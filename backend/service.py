"""AureoClean account rules: registration, approval, activation, login.

Every rule lives here, independent of FastAPI and of the storage provider, so
it can be tested directly and behaves the same in local and Firebase modes.

Accounts and roles
------------------
* Janitors request access (no password yet). An admin approves the request,
  which produces a one-time activation code (stored only as a hash, expires,
  limited attempts). The janitor activates with that code and chooses a
  password; only then is an authentication account created.
* Admins cannot self-register freely. They need a one-time admin invite code
  created by an existing admin (optionally locked to one email address).
  The very first admin is created on the Raspberry Pi itself with
  ``tools/create_admin.py``, which also links an already-existing Firebase
  account without deleting or changing it.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import Settings
from .sms import LogSender, SmsError, SmsSender, mask_phone, normalize_phone
from .providers.base import (
    AccountDisabled,
    AccountExists,
    AlreadyExists,
    DocStore,
    Identity,
)
from .security import (
    FailureTracker,
    ValidationError,
    generate_code,
    generate_id,
    generate_token,
    hash_secret,
    iso,
    iso_in,
    lookup_hash,
    normalize_code,
    normalize_email,
    normalize_username,
    parse_iso,
    utcnow,
    validate_email,
    validate_full_name,
    validate_password,
    validate_phone,
    validate_username,
    verify_secret,
)

ROLE_ADMIN = "admin"
ROLE_JANITOR = "janitor"

REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_ACTIVATING = "activating"
REQUEST_ACTIVATED = "activated"
REQUEST_REJECTED = "rejected"


# ---------------------------------------------------------------- errors
class ServiceError(Exception):
    status_code = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BadRequest(ServiceError):
    status_code = 400


class Unauthorized(ServiceError):
    status_code = 401


class Forbidden(ServiceError):
    status_code = 403


class NotFound(ServiceError):
    status_code = 404


class Conflict(ServiceError):
    status_code = 409


class TooManyAttempts(ServiceError):
    status_code = 429


class ServiceUnavailable(ServiceError):
    status_code = 503


# ---------------------------------------------------------------- sessions
@dataclass
class Session:
    token: str
    uid: str
    role: str
    username: str
    full_name: str
    email: str
    expires_at: datetime

    @property
    def actor(self) -> dict:
        return {"type": self.role, "uid": self.uid, "username": self.username}


class SessionManager:
    """In-memory sessions. Restarting the backend signs everyone out."""

    def __init__(self, hours: int):
        self.hours = hours
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, profile: dict) -> Session:
        session = Session(
            token=generate_token(),
            uid=profile["uid"],
            role=profile["role"],
            username=profile["username"],
            full_name=profile.get("fullName", ""),
            email=profile["email"],
            expires_at=parse_iso(iso_in(self.hours)),
        )
        with self._lock:
            self._sessions[session.token] = session
        return session

    def get(self, token: str) -> Optional[Session]:
        with self._lock:
            session = self._sessions.get(token or "")
            if session and session.expires_at <= utcnow():
                del self._sessions[session.token]
                return None
            return session

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_user(self, uid: str) -> None:
        with self._lock:
            for token in [t for t, s in self._sessions.items() if s.uid == uid]:
                del self._sessions[token]


# ---------------------------------------------------------------- helpers
def _check(value, validator):
    try:
        return validator(value)
    except ValidationError as exc:
        raise BadRequest(str(exc)) from exc


def _public_profile(profile: dict) -> dict:
    keys = ("uid", "username", "fullName", "email", "role", "status", "createdAt", "lastLoginAt",
            "smsPhone", "smsAlerts", "smsVerifiedAt")
    out = {k: profile.get(k) for k in keys}
    out["smsAlerts"] = bool(out["smsAlerts"])
    pending = profile.get("smsPending") or {}
    out["smsPendingPhone"] = pending.get("phone") if not _expired(pending.get("expiresAt")) else None
    return out


def _expired(value: Optional[str]) -> bool:
    return not value or parse_iso(value) <= utcnow()


class AureoService:
    def __init__(self, settings: Settings, store: DocStore, identity: Identity, sms: Optional[SmsSender] = None):
        self.settings = settings
        self.sms = sms or LogSender(settings.log_dir / "sms_outbox.log")
        self.store = store
        self.identity = identity
        prefix = settings.collection_prefix
        self.C_USERS = f"{prefix}Users"
        self.C_USERNAMES = f"{prefix}Usernames"
        self.C_REQUESTS = f"{prefix}RegistrationRequests"
        self.C_INVITES = f"{prefix}AdminInvites"
        self.C_AUDIT = f"{prefix}AuditLogs"
        self.sessions = SessionManager(settings.session_hours)
        self.login_failures = FailureTracker(settings.max_login_failures, settings.login_lockout_minutes)
        # Guessing activation / invite codes: 10 wrong tries per key per 15 min.
        self.code_failures = FailureTracker(10, 15)
        # SMS verification codes: at most 3 texts per user per 15 minutes.
        self.sms_sends = FailureTracker(3, 15)

    # ============================================================ audit
    def audit(self, action: str, actor: dict, target: Optional[str] = None, details: Optional[dict] = None):
        self.store.set(
            self.C_AUDIT,
            generate_id(),
            {"action": action, "actor": actor, "target": target, "details": details or {}, "at": iso(utcnow())},
        )

    def list_audit(self, limit: int = 200) -> list[dict]:
        limit = max(1, min(limit, 500))
        return [dict(data, id=doc_id) for doc_id, data in self.store.latest(self.C_AUDIT, "at", limit)]

    # ============================================================ janitor requests
    def _email_in_use(self, email: str) -> bool:
        if self.identity.find_uid(email) or self.store.query(self.C_USERS, email=email):
            return True
        return any(
            data.get("status") in (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_ACTIVATING)
            for _, data in self.store.query(self.C_REQUESTS, email=email)
        )

    def submit_janitor_request(self, full_name: str, username: str, email: str, phone: str = "") -> dict:
        full_name = _check(full_name, validate_full_name)
        username = _check(username, validate_username)
        email = _check(email, validate_email)
        phone = _check(phone, validate_phone)
        norm = normalize_username(username)

        if self._email_in_use(email):
            raise Conflict("This email already has an account or a request waiting for review.")

        request_id = generate_id()
        now = iso(utcnow())
        try:
            # Reserving the username document is atomic, so two people
            # can never end up with the same username.
            self.store.create(
                self.C_USERNAMES, norm, {"kind": "request", "requestId": request_id, "email": email, "createdAt": now}
            )
        except AlreadyExists:
            raise Conflict("That username is already taken.")

        try:
            self.store.create(
                self.C_REQUESTS,
                request_id,
                {
                    "username": username,
                    "usernameNormalized": norm,
                    "fullName": full_name,
                    "email": email,
                    "phone": phone,
                    "role": ROLE_JANITOR,
                    "status": REQUEST_PENDING,
                    "createdAt": now,
                    "activationAttempts": 0,
                },
            )
        except Exception:
            self.store.delete(self.C_USERNAMES, norm)
            raise

        self.audit("janitor_request_submitted", {"type": "applicant", "username": username}, request_id)
        return {
            "requestId": request_id,
            "status": REQUEST_PENDING,
            "message": "Request sent. An administrator will review it and give you an activation code.",
        }

    def check_request_status(self, username: str, email: str) -> dict:
        norm = normalize_username(username)
        email = normalize_email(email)
        not_found = NotFound("No request matches that username and email.")
        entry = self.store.get(self.C_USERNAMES, norm) if norm else None
        if not entry or entry.get("email") != email:
            raise not_found
        if entry.get("kind") == "user":
            return {"status": REQUEST_ACTIVATED, "message": "Your account is active. You can sign in."}
        req = self.store.get(self.C_REQUESTS, entry.get("requestId", ""))
        if not req:
            raise not_found
        status = req["status"]
        messages = {
            REQUEST_PENDING: "Your request is waiting for an administrator to review it.",
            REQUEST_APPROVED: "Approved. Get your activation code from the administrator, then choose 'Activate account'.",
            REQUEST_ACTIVATING: "Your account is being activated. Try signing in shortly.",
            REQUEST_REJECTED: "Your request was not approved." + (f" Reason: {req['rejectReason']}" if req.get("rejectReason") else ""),
        }
        if status == REQUEST_APPROVED and _expired(req.get("activationExpiresAt")):
            return {"status": status, "message": "Approved, but your activation code expired. Ask an administrator for a new one."}
        return {"status": status, "message": messages.get(status, status)}

    # ============================================================ admin review
    def list_requests(self, status: Optional[str] = None) -> list[dict]:
        filters = {"status": status} if status else {}
        out = []
        for doc_id, data in self.store.query(self.C_REQUESTS, **filters):
            item = {k: v for k, v in data.items() if k != "activationCodeHash"}
            item["id"] = doc_id
            if data.get("status") == REQUEST_APPROVED:
                item["codeExpired"] = _expired(data.get("activationExpiresAt"))
                item["codeLocked"] = data.get("activationAttempts", 0) >= self.settings.max_activation_attempts
            out.append(item)
        out.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
        return out

    def _issue_code(self, admin: Session, request_id: str, allowed: tuple[str, ...], action: str) -> dict:
        code = generate_code()
        expires = iso_in(self.settings.activation_code_hours)

        def update(current):
            if current is None:
                raise NotFound("Request not found.")
            if current.get("status") not in allowed:
                raise Conflict(f"This request is already {current.get('status')}.")
            current.update(
                status=REQUEST_APPROVED,
                activationCodeHash=hash_secret(normalize_code(code)),
                activationExpiresAt=expires,
                activationAttempts=0,
                reviewedBy={"uid": admin.uid, "username": admin.username},
                reviewedAt=iso(utcnow()),
            )
            return current

        req = self.store.transact(self.C_REQUESTS, request_id, update)
        self.audit(action, admin.actor, request_id, {"username": req["username"], "expiresAt": expires})
        return {"requestId": request_id, "username": req["username"], "code": code, "expiresAt": expires}

    def approve_request(self, admin: Session, request_id: str) -> dict:
        return self._issue_code(admin, request_id, (REQUEST_PENDING,), "janitor_request_approved")

    def reissue_code(self, admin: Session, request_id: str) -> dict:
        return self._issue_code(admin, request_id, (REQUEST_APPROVED,), "activation_code_reissued")

    def reject_request(self, admin: Session, request_id: str, reason: str = "") -> dict:
        reason = " ".join((reason or "").split())[:200]

        def update(current):
            if current is None:
                raise NotFound("Request not found.")
            if current.get("status") not in (REQUEST_PENDING, REQUEST_APPROVED):
                raise Conflict(f"This request is already {current.get('status')}.")
            current.pop("activationCodeHash", None)
            current.update(
                status=REQUEST_REJECTED,
                rejectReason=reason,
                reviewedBy={"uid": admin.uid, "username": admin.username},
                reviewedAt=iso(utcnow()),
            )
            return current

        req = self.store.transact(self.C_REQUESTS, request_id, update)
        # Free the username so it can be requested again.
        entry = self.store.get(self.C_USERNAMES, req["usernameNormalized"])
        if entry and entry.get("kind") == "request" and entry.get("requestId") == request_id:
            self.store.delete(self.C_USERNAMES, req["usernameNormalized"])
        self.audit("janitor_request_rejected", admin.actor, request_id, {"username": req["username"], "reason": reason})
        return {"requestId": request_id, "status": REQUEST_REJECTED}

    # ============================================================ activation
    def activate_janitor(self, username: str, code: str, password: str) -> dict:
        password = _check(password, validate_password)
        norm = normalize_username(username)
        key = f"activate:{norm}"
        wait = self.code_failures.locked_for(key)
        if wait:
            raise TooManyAttempts(f"Too many attempts. Try again in {max(1, wait // 60)} minute(s).")

        generic = BadRequest("Invalid username or activation code.")
        entry = self.store.get(self.C_USERNAMES, norm) if norm else None
        if entry and entry.get("kind") == "user":
            raise Conflict("This account is already active. Please sign in.")
        if not entry or entry.get("kind") != "request":
            self.code_failures.record_failure(key)
            raise generic

        request_id = entry["requestId"]
        req = self.store.get(self.C_REQUESTS, request_id)
        if not req:
            raise generic
        status = req.get("status")
        if status == REQUEST_PENDING:
            raise Forbidden("Your request has not been approved yet.")
        if status == REQUEST_REJECTED:
            raise Forbidden("Your request was not approved. Contact an administrator.")
        if status != REQUEST_APPROVED:
            raise Conflict("This request is already being activated.")
        if req.get("activationAttempts", 0) >= self.settings.max_activation_attempts:
            raise Forbidden("Too many wrong codes. Ask an administrator to issue a new activation code.")
        if _expired(req.get("activationExpiresAt")):
            raise Forbidden("This activation code has expired. Ask an administrator for a new one.")

        stored_hash = req.get("activationCodeHash", "")
        if not verify_secret(normalize_code(code), stored_hash):
            self.code_failures.record_failure(key)

            def bump(current):
                if current and current.get("status") == REQUEST_APPROVED:
                    current["activationAttempts"] = current.get("activationAttempts", 0) + 1
                    return current
                return None

            updated = self.store.transact(self.C_REQUESTS, request_id, bump) or req
            left = self.settings.max_activation_attempts - updated.get("activationAttempts", 0)
            if left <= 0:
                raise Forbidden("Too many wrong codes. Ask an administrator to issue a new activation code.")
            raise BadRequest(f"Invalid username or activation code. {left} attempt(s) left.")

        # Claim the request so the same code can never be used twice.
        def claim(current):
            if not current or current.get("status") != REQUEST_APPROVED or current.get("activationCodeHash") != stored_hash:
                raise Conflict("This activation code has already been used.")
            current.update(status=REQUEST_ACTIVATING, activationClaimedAt=iso(utcnow()))
            current.pop("activationCodeHash", None)
            return current

        self.store.transact(self.C_REQUESTS, request_id, claim)

        def release(current):
            if current and current.get("status") == REQUEST_ACTIVATING:
                current.update(status=REQUEST_APPROVED, activationCodeHash=stored_hash)
                return current
            return None

        try:
            uid = self.identity.create_account(req["email"], password, req["fullName"])
        except AccountExists:
            self.store.transact(self.C_REQUESTS, request_id, release)
            raise Conflict("An account already exists for this email. Ask an administrator for help.")
        except Exception:
            self.store.transact(self.C_REQUESTS, request_id, release)
            raise

        now = iso(utcnow())
        profile = {
            "uid": uid,
            "username": req["username"],
            "usernameNormalized": norm,
            "fullName": req["fullName"],
            "email": req["email"],
            "phone": req.get("phone", ""),
            "role": ROLE_JANITOR,
            "status": "active",
            "createdAt": now,
            "requestId": request_id,
            "approvedBy": req.get("reviewedBy"),
        }
        self.store.set(self.C_USERS, uid, profile)
        self.store.set(self.C_USERNAMES, norm, {"kind": "user", "uid": uid, "email": req["email"], "createdAt": now})

        def finish(current):
            current = current or {}
            current.update(status=REQUEST_ACTIVATED, activatedAt=now, uid=uid)
            return current

        self.store.transact(self.C_REQUESTS, request_id, finish)
        self.code_failures.reset(key)
        self.audit("janitor_activated", {"type": ROLE_JANITOR, "uid": uid, "username": req["username"]}, uid)
        return {"username": req["username"], "message": "Account activated. You can now sign in."}

    # ============================================================ login
    def login(self, identifier: str, password: str) -> dict:
        ident = (identifier or "").strip()
        key = f"login:{ident.lower()}"
        wait = self.login_failures.locked_for(key)
        if wait:
            raise TooManyAttempts(f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).")

        generic = Unauthorized("Incorrect username/email or password.")
        if not ident or not password:
            raise BadRequest("Enter your username (or email) and password.")

        if "@" in ident:
            email = normalize_email(ident)
        else:
            entry = self.store.get(self.C_USERNAMES, normalize_username(ident))
            if not entry:
                self.login_failures.record_failure(key)
                raise generic
            if entry.get("kind") == "request":
                req = self.store.get(self.C_REQUESTS, entry.get("requestId", "")) or {}
                status = req.get("status")
                if status == REQUEST_APPROVED:
                    raise Forbidden("Your request was approved. Choose 'Activate account' and enter your activation code first.")
                if status == REQUEST_PENDING:
                    raise Forbidden("Your request is still waiting for administrator approval.")
                self.login_failures.record_failure(key)
                raise generic
            email = entry.get("email", "")

        try:
            uid = self.identity.verify_password(email, password)
        except AccountDisabled:
            raise Forbidden("This account has been disabled. Contact an administrator.")
        if not uid:
            self.login_failures.record_failure(key)
            raise generic

        profile = self.store.get(self.C_USERS, uid)
        if not profile:
            raise Forbidden(
                "This sign-in account is not set up in AureoClean yet. "
                "An administrator can link it with tools/create_admin.py --link-existing."
            )
        if profile.get("status") != "active":
            raise Forbidden("This account has been disabled. Contact an administrator.")

        self.login_failures.reset(key)
        profile["lastLoginAt"] = iso(utcnow())
        self.store.set(self.C_USERS, uid, profile)
        session = self.sessions.create(profile)
        self.audit("login", session.actor, uid)
        return {"token": session.token, "expiresAt": iso(session.expires_at), "user": _public_profile(profile)}

    def logout(self, session: Session) -> None:
        self.sessions.revoke(session.token)

    def require_session(self, token: str, role: Optional[str] = None) -> Session:
        session = self.sessions.get(token)
        if not session:
            raise Unauthorized("Your session has ended. Please sign in again.")
        if role and session.role != role:
            raise Forbidden("You do not have permission to do that.")
        return session

    def me(self, session: Session) -> dict:
        profile = self.store.get(self.C_USERS, session.uid)
        if not profile or profile.get("status") != "active":
            self.sessions.revoke(session.token)
            raise Unauthorized("Your account is no longer active.")
        return _public_profile(profile)

    def change_password(self, session: Session, current: str, new: str) -> None:
        new = _check(new, validate_password)
        if new == current:
            raise BadRequest("The new password must be different from the current one.")
        try:
            ok = self.identity.verify_password(session.email, current or "")
        except AccountDisabled:
            raise Forbidden("This account has been disabled.")
        if ok != session.uid:
            raise Unauthorized("Your current password is incorrect.")
        self.identity.set_password(session.uid, new)
        self.audit("password_changed", session.actor, session.uid)

    # ============================================================ admin invites
    def create_admin_invite(self, admin: Session, email: str = "", hours: Optional[int] = None) -> dict:
        email = _check(email, validate_email) if (email or "").strip() else ""
        hours = hours or self.settings.admin_invite_hours
        hours = max(1, min(int(hours), 168))
        code = generate_code(groups=3, size=5)
        invite_id = lookup_hash(code)
        expires = iso_in(hours)
        self.store.create(
            self.C_INVITES,
            invite_id,
            {
                "email": email or None,
                "status": "active",
                "createdBy": {"uid": admin.uid, "username": admin.username},
                "createdAt": iso(utcnow()),
                "expiresAt": expires,
            },
        )
        self.audit("admin_invite_created", admin.actor, invite_id[:12], {"email": email or None, "expiresAt": expires})
        return {"inviteId": invite_id, "code": code, "email": email or None, "expiresAt": expires}

    def list_invites(self) -> list[dict]:
        out = []
        for doc_id, data in self.store.query(self.C_INVITES):
            status = data.get("status")
            if status == "active" and _expired(data.get("expiresAt")):
                status = "expired"
            out.append(dict(data, id=doc_id, status=status))
        out.sort(key=lambda r: r.get("createdAt") or "", reverse=True)
        return out

    def revoke_invite(self, admin: Session, invite_id: str) -> None:
        def update(current):
            if current is None:
                raise NotFound("Invite not found.")
            if current.get("status") != "active":
                raise Conflict(f"This invite is already {current.get('status')}.")
            current.update(status="revoked", revokedBy={"uid": admin.uid, "username": admin.username}, revokedAt=iso(utcnow()))
            return current

        self.store.transact(self.C_INVITES, invite_id, update)
        self.audit("admin_invite_revoked", admin.actor, invite_id[:12])

    def register_admin(self, invite_code: str, full_name: str, username: str, email: str, password: str) -> dict:
        full_name = _check(full_name, validate_full_name)
        username = _check(username, validate_username)
        email = _check(email, validate_email)
        password = _check(password, validate_password)

        key = "admin-invite"
        wait = self.code_failures.locked_for(key)
        if wait:
            raise TooManyAttempts(f"Too many invalid invite codes. Try again in {max(1, wait // 60)} minute(s).")

        invite_id = lookup_hash(invite_code)
        bad = BadRequest("Invalid or expired admin invite code.")

        def claim(current):
            if not current or current.get("status") != "active" or _expired(current.get("expiresAt")):
                raise bad
            if current.get("email") and current["email"] != email:
                raise BadRequest("This invite code was issued for a different email address.")
            current.update(status="claimed", claimedAt=iso(utcnow()))
            return current

        try:
            self.store.transact(self.C_INVITES, invite_id, claim)
        except BadRequest:
            self.code_failures.record_failure(key)
            raise

        def unclaim(current):
            if current and current.get("status") == "claimed":
                current["status"] = "active"
                current.pop("claimedAt", None)
                return current
            return None

        try:
            if self._email_in_use(email):
                raise Conflict(
                    "An account already exists for this email. Ask an administrator to link it "
                    "with tools/create_admin.py --link-existing instead."
                )
            profile = self._create_user(full_name, username, email, password, ROLE_ADMIN, {"inviteId": invite_id[:12]})
        except Exception:
            self.store.transact(self.C_INVITES, invite_id, unclaim)
            raise

        def used(current):
            current = current or {}
            current.update(status="used", usedBy={"uid": profile["uid"], "username": username}, usedAt=iso(utcnow()))
            return current

        self.store.transact(self.C_INVITES, invite_id, used)
        self.code_failures.reset(key)
        self.audit("admin_registered", {"type": ROLE_ADMIN, "uid": profile["uid"], "username": username}, profile["uid"], {"inviteId": invite_id[:12]})
        return {"username": username, "message": "Admin account created. You can now sign in."}

    def _create_user(
        self, full_name: str, username: str, email: str, password: Optional[str], role: str,
        extra: Optional[dict] = None, existing_uid: Optional[str] = None,
    ) -> dict:
        """Reserve the username, create (or link) the auth account and the profile."""
        norm = normalize_username(username)
        now = iso(utcnow())
        try:
            self.store.create(self.C_USERNAMES, norm, {"kind": "reserved", "email": email, "createdAt": now})
        except AlreadyExists:
            raise Conflict("That username is already taken.")

        created_uid = None
        try:
            if existing_uid:
                uid = existing_uid
            else:
                try:
                    uid = created_uid = self.identity.create_account(email, password, full_name)
                except AccountExists:
                    raise Conflict("An account already exists for this email.")
            profile = {
                "uid": uid,
                "username": username,
                "usernameNormalized": norm,
                "fullName": full_name,
                "email": email,
                "role": role,
                "status": "active",
                "createdAt": now,
                **(extra or {}),
            }
            self.store.set(self.C_USERS, uid, profile)
            self.store.set(self.C_USERNAMES, norm, {"kind": "user", "uid": uid, "email": email, "createdAt": now})
            return profile
        except Exception:
            self.store.delete(self.C_USERNAMES, norm)
            if created_uid:
                self.identity.delete_account(created_uid)
            raise

    # ============================================================ bootstrap (CLI only)
    def bootstrap_admin(
        self, full_name: str, username: str, email: str, password: Optional[str] = None, link_existing: bool = False
    ) -> dict:
        """Create the first admin, or link an existing auth account as an admin.

        Only reachable from tools/create_admin.py on the Pi itself, never over HTTP."""
        full_name = _check(full_name, validate_full_name)
        username = _check(username, validate_username)
        email = _check(email, validate_email)
        actor = {"type": "system", "username": "create_admin.py"}

        if link_existing:
            uid = self.identity.find_uid(email)
            if not uid:
                raise NotFound("No sign-in account exists for that email, so there is nothing to link.")
            if self.store.get(self.C_USERS, uid):
                raise Conflict("That account is already set up in AureoClean.")
            # The existing account and its password are left exactly as they are.
            profile = self._create_user(full_name, username, email, None, ROLE_ADMIN, {"linkedExisting": True}, existing_uid=uid)
            self.audit("admin_linked_existing", actor, uid, {"username": username, "email": email})
        else:
            password = _check(password, validate_password)
            if self._email_in_use(email):
                raise Conflict("An account already exists for this email. Use --link-existing to link it.")
            profile = self._create_user(full_name, username, email, password, ROLE_ADMIN, {"bootstrap": True})
            self.audit("admin_bootstrapped", actor, profile["uid"], {"username": username, "email": email})
        return _public_profile(profile)

    # ============================================================ user management
    def list_users(self) -> list[dict]:
        users = [_public_profile(data) for _, data in self.store.query(self.C_USERS)]
        users.sort(key=lambda u: (u.get("role") != ROLE_ADMIN, (u.get("username") or "").lower()))
        return users

    def set_user_status(self, admin: Session, uid: str, active: bool) -> dict:
        if uid == admin.uid:
            raise BadRequest("You cannot change the status of your own account.")
        profile = self.store.get(self.C_USERS, uid)
        if not profile:
            raise NotFound("User not found.")
        if not active and profile.get("role") == ROLE_ADMIN:
            others = [u for _, u in self.store.query(self.C_USERS, role=ROLE_ADMIN, status="active") if u["uid"] != uid]
            if not others:
                raise BadRequest("At least one active administrator must remain.")
        self.identity.set_disabled(uid, not active)
        profile["status"] = "active" if active else "disabled"
        self.store.set(self.C_USERS, uid, profile)
        if not active:
            self.sessions.revoke_user(uid)
        self.audit("user_enabled" if active else "user_disabled", admin.actor, uid, {"username": profile["username"]})
        return _public_profile(profile)

    # ============================================================ SMS (any signed-in user)
    def _update_profile(self, uid: str, fn) -> dict:
        def update(current):
            if not current or current.get("status") != "active":
                raise Unauthorized("Your account is no longer active.")
            fn(current)
            return current

        return self.store.transact(self.C_USERS, uid, update)

    def start_phone_verification(self, session: Session, phone: str) -> dict:
        try:
            phone = normalize_phone(phone)
        except ValueError as exc:
            raise BadRequest(str(exc))
        profile = self.store.get(self.C_USERS, session.uid) or {}
        if profile.get("smsPhone") == phone:
            raise BadRequest("This number is already verified on your account.")
        pending = profile.get("smsPending") or {}
        if pending.get("sentAt") and (utcnow() - parse_iso(pending["sentAt"])).total_seconds() < 60:
            raise TooManyAttempts("Please wait a minute before asking for another code.")
        key = f"sms:{session.uid}"
        wait = self.sms_sends.locked_for(key)
        if wait:
            raise TooManyAttempts(f"Too many codes sent. Try again in {max(1, wait // 60)} minute(s).")

        code = f"{secrets.randbelow(10**6):06d}"
        try:
            self.sms.send(phone, f"AureoClean verification code: {code}. It expires in 10 minutes. Do not share it.")
        except SmsError as exc:
            raise ServiceUnavailable(str(exc))
        self.sms_sends.record_failure(key)  # counts sends, not failures

        def save(p):
            p["smsPending"] = {
                "phone": phone,
                "codeHash": hash_secret(code),
                "expiresAt": iso_in(10 / 60),
                "attempts": 0,
                "sentAt": iso(utcnow()),
            }

        self._update_profile(session.uid, save)
        self.audit("sms_code_sent", session.actor, session.uid, {"phone": mask_phone(phone)})
        if self.sms.mode == "log":
            message = ("SMS is in test mode, so no text was actually sent. An administrator can find "
                       "the code in logs/sms_outbox.log on the Raspberry Pi.")
        else:
            message = f"We sent a 6-digit code to {phone}. It expires in 10 minutes."
        return {"phone": phone, "smsMode": self.sms.mode, "message": message}

    def confirm_phone(self, session: Session, code: str) -> dict:
        code = "".join(ch for ch in (code or "") if ch.isdigit())
        result = {}

        def check(p):
            pending = p.get("smsPending")
            if not pending:
                raise BadRequest("Ask for a verification code first.")
            if _expired(pending.get("expiresAt")):
                p.pop("smsPending", None)
                result["error"] = BadRequest("That code has expired. Ask for a new one.")
                return
            if pending.get("attempts", 0) >= 5:
                p.pop("smsPending", None)
                result["error"] = Forbidden("Too many wrong codes. Ask for a new one.")
                return
            if not verify_secret(code, pending.get("codeHash", "")):
                pending["attempts"] = pending.get("attempts", 0) + 1
                left = 5 - pending["attempts"]
                result["error"] = BadRequest(f"Wrong code. {left} attempt(s) left." if left else "Wrong code. Ask for a new one.")
                return
            p.update(smsPhone=pending["phone"], smsAlerts=True, smsVerifiedAt=iso(utcnow()))
            p.pop("smsPending", None)

        profile = self._update_profile(session.uid, check)
        if "error" in result:
            raise result["error"]
        self.audit("sms_phone_verified", session.actor, session.uid, {"phone": mask_phone(profile["smsPhone"])})
        return _public_profile(profile)

    def set_sms_alerts(self, session: Session, enabled: bool) -> dict:
        def change(p):
            if enabled and not p.get("smsPhone"):
                raise BadRequest("Add and verify a mobile number first.")
            p["smsAlerts"] = bool(enabled)

        profile = self._update_profile(session.uid, change)
        self.audit("sms_alerts_on" if enabled else "sms_alerts_off", session.actor, session.uid)
        return _public_profile(profile)

    def remove_phone(self, session: Session) -> dict:
        def change(p):
            for k in ("smsPhone", "smsVerifiedAt", "smsPending"):
                p.pop(k, None)
            p["smsAlerts"] = False

        profile = self._update_profile(session.uid, change)
        self.audit("sms_phone_removed", session.actor, session.uid)
        return _public_profile(profile)

    def sms_recipients(self, role: Optional[str] = None) -> list[dict]:
        """Active users who verified a number and want SMS alerts (for the alert service)."""
        filters = {"status": "active"}
        if role:
            filters["role"] = role
        return [
            {"uid": u["uid"], "username": u["username"], "role": u["role"], "phone": u["smsPhone"]}
            for _, u in self.store.query(self.C_USERS, **filters)
            if u.get("smsAlerts") and u.get("smsPhone")
        ]

    # ------------------------------------------------ admin SMS tools
    def sms_status(self) -> dict:
        try:
            return self.sms.check()
        except SmsError as exc:
            return {"mode": self.sms.mode, "ok": False, "detail": str(exc)}

    def send_test_sms(self, admin: Session, uid: str) -> dict:
        profile = self.store.get(self.C_USERS, uid)
        if not profile:
            raise NotFound("User not found.")
        if not profile.get("smsPhone"):
            raise BadRequest(f"{profile['username']} has not verified a mobile number yet.")
        try:
            self.sms.send(profile["smsPhone"], f"AureoClean test message sent by {admin.username}. SMS alerts are working.")
        except SmsError as exc:
            raise ServiceUnavailable(str(exc))
        self.audit("sms_test_sent", admin.actor, uid, {"username": profile["username"], "phone": mask_phone(profile["smsPhone"])})
        where = "written to the test outbox (test mode)" if self.sms.mode == "log" else f"sent to {profile['smsPhone']}"
        return {"message": f"Test SMS {where}."}

    # ============================================================ links
    def links(self, session: Session) -> list[dict]:
        """External tools each role may open from the app.

        These are only shortcuts: access to the websites themselves is
        controlled by the Arduino / Firebase accounts, not by this app."""
        out = []
        if self.settings.dashboard_url:
            out.append({"key": "dashboard", "title": "Restroom monitoring dashboard",
                        "description": "Live sensor readings on Arduino IoT Cloud.", "url": self.settings.dashboard_url})
        if session.role == ROLE_ADMIN:
            out.append({"key": "arduino", "title": "Arduino Cloud (things & code editor)",
                        "description": "Manage devices, variables and ESP32 sketches.", "url": self.settings.arduino_cloud_url})
            if self.settings.firebase_project_id:
                out.append({"key": "firebase", "title": "Firebase Console",
                            "description": "Authentication, Firestore and Realtime Database.",
                            "url": f"https://console.firebase.google.com/project/{self.settings.firebase_project_id}/overview"})
        return out

    # ============================================================ dashboards
    def admin_overview(self) -> dict:
        requests = self.list_requests()
        users = self.list_users()
        return {
            "pendingRequests": sum(r["status"] == REQUEST_PENDING for r in requests),
            "awaitingActivation": sum(r["status"] == REQUEST_APPROVED for r in requests),
            "activeJanitors": sum(u["role"] == ROLE_JANITOR and u["status"] == "active" for u in users),
            "activeAdmins": sum(u["role"] == ROLE_ADMIN and u["status"] == "active" for u in users),
            "storageMode": self.settings.mode,
            "smsMode": self.sms.mode,
            "smsSubscribers": len(self.sms_recipients()),
        }

    def janitor_overview(self, session: Session) -> dict:
        return {
            "profile": self.me(session),
            # Filled in once the Realtime Database sensor feed is connected.
            "notifications": [],
            "message": "Restroom alerts will appear here once the sensor feed is connected.",
        }
