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

import hashlib
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .config import Settings
from .providers.base import (
    AccountDisabled,
    AccountExists,
    AlreadyExists,
    DocStore,
    Identity,
    ProviderError,
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
from .two_way import alert_text
from .sms import (
    LogSender,
    ModemSender,
    SavedSmsSettings,
    SmsError,
    SmsSender,
    detect_modem,
    mask_phone,
    normalize_phone,
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
    """Sign-in sessions, saved in the database so restarting the backend
    (or the Pi) doesn't sign anyone out.

    Only a SHA-256 hash of each token is stored, so someone reading the
    database can't use it to sign in. A small in-memory cache avoids a
    database read on every request."""

    def __init__(self, hours: int, store: Optional[DocStore] = None, collection: str = "aureoSessions"):
        self.hours = hours
        self.store = store
        self.collection = collection
        self._cache: dict[str, Session] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

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
        if self.store is not None:
            self.store.set(self.collection, self._key(session.token), {
                "uid": session.uid, "role": session.role, "username": session.username,
                "fullName": session.full_name, "email": session.email,
                "expiresAt": iso(session.expires_at), "createdAt": iso(utcnow()),
            })
            self._drop_expired(session.uid)
        with self._lock:
            self._cache[session.token] = session
        return session

    def get(self, token: str) -> Optional[Session]:
        if not token:
            return None
        with self._lock:
            session = self._cache.get(token)
        if session is None and self.store is not None:
            doc = self.store.get(self.collection, self._key(token))
            if doc:
                session = Session(token=token, uid=doc["uid"], role=doc["role"], username=doc["username"],
                                  full_name=doc.get("fullName", ""), email=doc["email"],
                                  expires_at=parse_iso(doc["expiresAt"]))
                with self._lock:
                    self._cache[token] = session
        if session and session.expires_at <= utcnow():
            self.revoke(token)
            return None
        return session

    def revoke(self, token: str) -> None:
        with self._lock:
            self._cache.pop(token, None)
        if self.store is not None and token:
            self.store.delete(self.collection, self._key(token))

    def revoke_user(self, uid: str) -> None:
        with self._lock:
            for token in [t for t, s in self._cache.items() if s.uid == uid]:
                del self._cache[token]
        if self.store is not None:
            for doc_id, _ in self.store.query(self.collection, uid=uid):
                self.store.delete(self.collection, doc_id)

    def _drop_expired(self, uid: str) -> None:
        """Tidy up this user's old, expired sessions."""
        for doc_id, doc in self.store.query(self.collection, uid=uid):
            if parse_iso(doc["expiresAt"]) <= utcnow():
                self.store.delete(self.collection, doc_id)


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


def _as_iso(value) -> str:
    """Firestore returns datetimes; the local store returns strings."""
    if isinstance(value, datetime):
        return iso(value)
    return str(value) if value else iso(utcnow())


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
        self.C_WEB_SIGNUPS = f"{prefix}WebSignups"   # written by the public sign-up website (create-only)
        self.C_REPORTS = f"{prefix}Reports"
        self.C_PUBLIC_REPORTS = f"{prefix}PublicReports"  # written by the public report page (create-only)
        self.C_COUNTERS = f"{prefix}Counters"
        self.C_SESSIONS = f"{prefix}Sessions"
        self.sessions = SessionManager(settings.session_hours, store, self.C_SESSIONS)
        self.login_failures = FailureTracker(settings.max_login_failures, settings.login_lockout_minutes)
        # Guessing activation / invite codes: 10 wrong tries per key per 15 min.
        self.code_failures = FailureTracker(10, 15)
        from .two_way import TwoWaySms
        self.two_way = TwoWaySms(self)
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
        req = self.store.get(self.C_REQUESTS, request_id)
        if req and req.get("source") == "web":
            return self._approve_sms_contact(admin, request_id)
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

        if not ident or not password:
            raise BadRequest("Enter your username (or email) and password.")
        email, uid = self._check_credentials(ident, password, key)

        profile = self.store.get(self.C_USERS, uid)
        if not profile:
            raise Forbidden(
                "This sign-in account is not set up in AureoClean yet. "
                "An administrator can link it with tools/create_admin.py --link-existing."
            )
        if profile.get("status") != "active":
            raise Forbidden("This account has been disabled. Contact an administrator.")
        if profile.get("emailVerifyRequired"):
            if not self.identity.is_email_verified(uid):
                self.login_failures.reset(key)
                raise Forbidden(
                    f"Please verify your email first: open the link we sent to {email}, then sign in again. "
                    "(Didn't get it? Use “Resend verification email”.)")
            profile.pop("emailVerifyRequired", None)
            profile["emailVerifiedAt"] = iso(utcnow())
            self.audit("admin_email_verified", {"type": profile["role"], "uid": uid, "username": profile["username"]}, uid)

        self.login_failures.reset(key)
        profile["lastLoginAt"] = iso(utcnow())
        self.store.set(self.C_USERS, uid, profile)
        session = self.sessions.create(profile)
        self.audit("login", session.actor, uid)
        return {"token": session.token, "expiresAt": iso(session.expires_at), "user": _public_profile(profile)}

    def _check_credentials(self, ident: str, password: str, key: str) -> tuple[str, str]:
        """Resolve a username or email and check the password. Returns (email, uid)."""
        generic = Unauthorized("Incorrect username/email or password.")
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
        if not email:  # SMS-only janitor contacts can't sign in to the app
            self.login_failures.record_failure(key)
            raise generic

        try:
            uid = self.identity.verify_password(email, password)
        except AccountDisabled:
            raise Forbidden("This account has been disabled. Contact an administrator.")
        if not uid:
            self.login_failures.record_failure(key)
            raise generic
        return email, uid

    def resend_verification(self, identifier: str, password: str) -> dict:
        ident = (identifier or "").strip()
        key = f"login:{ident.lower()}"
        if self.login_failures.locked_for(key):
            raise TooManyAttempts("Too many failed attempts. Try again later.")
        if not ident or not password:
            raise BadRequest("Enter your username (or email) and password first.")
        email, uid = self._check_credentials(ident, password, key)
        profile = self.store.get(self.C_USERS, uid) or {}
        if not profile.get("emailVerifyRequired") or self.identity.is_email_verified(uid):
            return {"message": "Your email is already verified. You can sign in."}
        send_key = f"verify:{uid}"
        if self.sms_sends.locked_for(send_key):
            raise TooManyAttempts("A verification email was sent recently. Check your inbox (and Spam) first.")
        try:
            self.identity.send_verification_email(email, password)
        except ProviderError as exc:
            raise ServiceUnavailable(str(exc))
        self.sms_sends.record_failure(send_key)
        self.audit("verification_email_sent", {"type": profile.get("role", ""), "uid": uid}, uid)
        return {"message": f"Verification email sent to {email}. Open the link, then sign in."}

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
        # Tell the screen whether texts are really sent (modem) or only logged (test mode).
        return {**_public_profile(profile), "smsMode": self.sms.mode}

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
            extra = {"inviteId": invite_id[:12]}
            if self.identity.supports_email_verification:
                extra["emailVerifyRequired"] = True
            profile = self._create_user(full_name, username, email, password, ROLE_ADMIN, extra)
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
        if not profile.get("emailVerifyRequired"):
            return {"username": username, "message": "Admin account created. You can now sign in."}
        try:
            self.identity.send_verification_email(email, password)
            self.sms_sends.record_failure(f"verify:{profile['uid']}")
            note = f"We sent a verification link to {email}. Open it, then sign in."
        except ProviderError:
            note = "We couldn't send the verification email just now: sign in and choose “Resend verification email”."
        return {"username": username, "message": f"Admin account created. {note}"}

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
        if not profile.get("smsOnly"):
            self.identity.set_disabled(uid, not active)
        profile["status"] = "active" if active else "disabled"
        self.store.set(self.C_USERS, uid, profile)
        if not active:
            self.sessions.revoke_user(uid)
        self.audit("user_enabled" if active else "user_disabled", admin.actor, uid, {"username": profile["username"]})
        return _public_profile(profile)

    def delete_user(self, admin: Session, uid: str, confirm_username: str) -> dict:
        """Permanently delete an account: sign-in account, profile, username and sessions.

        The audit log keeps a record that it happened (who, when, which username),
        and past reports keep the name they were filed under."""
        if uid == admin.uid:
            raise BadRequest("You cannot delete your own account.")
        profile = self.store.get(self.C_USERS, uid)
        if not profile:
            raise NotFound("User not found.")
        if normalize_username(confirm_username) != profile.get("usernameNormalized", normalize_username(profile["username"])):
            raise BadRequest(f"To confirm, type the username exactly: {profile['username']}")
        if profile.get("role") == ROLE_ADMIN:
            others = [u for _, u in self.store.query(self.C_USERS, role=ROLE_ADMIN, status="active") if u["uid"] != uid]
            if not others:
                raise BadRequest("You can't delete the last active administrator.")

        if not profile.get("smsOnly"):
            try:
                self.identity.delete_account(uid)
            except Exception as exc:  # already gone in Firebase is fine; anything else stops here
                if "not found" not in str(exc).lower() and type(exc).__name__ != "UserNotFoundError":
                    raise ServiceUnavailable(f"Couldn't delete the sign-in account: {exc}")
        self.sessions.revoke_user(uid)
        norm = profile.get("usernameNormalized") or normalize_username(profile["username"])
        entry = self.store.get(self.C_USERNAMES, norm)
        if entry and entry.get("uid") == uid:
            self.store.delete(self.C_USERNAMES, norm)
        self.store.delete(self.C_USERS, uid)
        self.audit("user_deleted", admin.actor, uid, {"username": profile["username"], "role": profile.get("role")})
        return {"message": f"{profile.get('fullName') or profile['username']}'s account was deleted."}

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
    def sms_settings(self) -> dict:
        port = getattr(self.sms, "port", "")
        baud = getattr(self.sms, "baud", 0)
        label = f"GSM modem on {port} @ {baud} baud" if self.sms.mode == "modem" else "Test mode (texts are not sent)"
        return {"mode": self.sms.mode, "port": port, "baud": baud, "label": label}

    def use_detected_modem(self, admin: Session, detector=None) -> dict:
        """Find the modem on the serial ports and switch SMS to it right away."""
        try:
            found = (detector or detect_modem)()
        except SmsError as exc:
            raise ServiceUnavailable(str(exc))
        self.sms = ModemSender(found["port"], found["baud"])
        SavedSmsSettings(self.settings.sms_settings_file).save("modem", found["port"], found["baud"])
        self.audit("sms_modem_enabled", admin.actor, None, {"port": found["port"], "baud": found["baud"],
                                                            "model": found["model"][:60]})
        return {**self.sms_settings(), "model": found["model"],
                "message": f"Found {found['model'] or 'the modem'} on {found['port']} @ {found['baud']} baud. "
                           "Texts are now sent for real."}

    def use_test_mode(self, admin: Session) -> dict:
        self.sms = LogSender(self.settings.log_dir / "sms_outbox.log")
        SavedSmsSettings(self.settings.sms_settings_file).save("log")
        self.audit("sms_test_mode", admin.actor)
        return {**self.sms_settings(), "message": "Test mode: texts are written to logs/sms_outbox.log, not sent."}

    def send_test_sms_to(self, admin: Session, phone: str) -> dict:
        try:
            phone = normalize_phone(phone)
        except ValueError as exc:
            raise BadRequest(str(exc))
        key = f"testsms:{admin.uid}"
        if self.sms_sends.locked_for(key):
            raise TooManyAttempts("Too many test texts. Wait a few minutes.")
        try:
            self.sms.send(phone, f"AureoClean test message from {admin.username}. SMS is working.")
        except SmsError as exc:
            raise ServiceUnavailable(str(exc))
        self.sms_sends.record_failure(key)
        self.audit("sms_test_sent", admin.actor, None, {"phone": mask_phone(phone)})
        where = "written to logs/sms_outbox.log (test mode)" if self.sms.mode == "log" else f"sent to {phone}"
        return {"message": f"Test SMS {where}."}

    def simulate_sms_reply(self, admin: Session, phone: str, text: str) -> dict:
        """Handle a reply as if it had arrived by SMS (for testing and demos)."""
        result = self.two_way.handle(phone, text)
        self.audit("sms_reply_simulated", admin.actor, None, {"text": (text or "")[:40], "result": result["action"]})
        return result

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

    # ============================================================ web sign-up (janitors)
    def _username_from_name(self, full_name: str) -> str:
        parts = [re.sub(r"[^a-z0-9]", "", w) for w in full_name.lower().split()]
        parts = [w for w in parts if w]
        base = ".".join([parts[0], parts[-1]] if len(parts) > 1 else parts)[:20] or "janitor"
        base = base if len(base) >= 3 else (base + "jan")[:3]
        candidate, n = base, 1
        while self.store.get(self.C_USERNAMES, candidate):
            n += 1
            candidate = f"{base}{n}"
        return candidate

    def _phone_in_use(self, phone: str) -> bool:
        if self.store.query(self.C_USERS, smsPhone=phone):
            return True
        return any(r.get("status") in (REQUEST_PENDING, REQUEST_APPROVED)
                   for _, r in self.store.query(self.C_REQUESTS, phone=phone))

    def submit_web_signup(self, full_name: str, phone: str, email: str = "") -> dict:
        """A janitor signed up on the website: add it to the admin's Requests list."""
        full_name = _check(full_name, validate_full_name)
        try:
            phone = normalize_phone(phone)
        except ValueError as exc:
            raise BadRequest(str(exc))
        email = _check(email, validate_email) if (email or "").strip() else ""
        if self._phone_in_use(phone):
            raise Conflict("This mobile number is already registered or waiting for approval.")
        if email and self._email_in_use(email):
            raise Conflict("This email is already registered or waiting for approval.")

        request_id = generate_id()
        now = iso(utcnow())
        for _ in range(5):  # another sign-up may grab the same username at the same moment
            username = self._username_from_name(full_name)
            try:
                self.store.create(self.C_USERNAMES, username,
                                  {"kind": "request", "requestId": request_id, "email": email, "createdAt": now})
                break
            except AlreadyExists:
                continue
        else:
            raise Conflict("Couldn't reserve a username. Try again.")
        self.store.create(self.C_REQUESTS, request_id, {
            "username": username, "usernameNormalized": username, "fullName": full_name, "email": email,
            "phone": phone, "role": ROLE_JANITOR, "status": REQUEST_PENDING, "createdAt": now,
            "activationAttempts": 0, "source": "web",
        })
        self.audit("web_signup_received", {"type": "applicant", "username": username}, request_id,
                   {"phone": mask_phone(phone)})
        return {"requestId": request_id, "username": username}

    def import_web_signups(self) -> int:
        """Move sign-ups from the website into Requests. Run every few seconds by the backend.

        The website's document ID is the device's ID. After importing, the document is
        replaced by an anonymous marker (no name/number), so the database keeps refusing
        another sign-up from that device."""
        count = 0
        for doc_id, data in self.store.query(self.C_WEB_SIGNUPS):
            if data.get("processed"):
                continue
            outcome = "imported"
            try:
                self.submit_web_signup(data.get("fullName", ""), data.get("phone", ""), data.get("email", ""))
                count += 1
            except ServiceError as exc:
                outcome = "rejected"
                self.audit("web_signup_rejected", {"type": "system", "username": "web"}, doc_id[:8],
                           {"reason": exc.message, "phone": mask_phone(str(data.get("phone", "")))})
                try:
                    self.sms.send(normalize_phone(data.get("phone", "")),
                                  f"AureoClean: we couldn't accept your sign-up. {exc.message}")
                except (SmsError, ValueError):
                    pass
            finally:
                self.store.set(self.C_WEB_SIGNUPS, doc_id,
                               {"processed": True, "processedAt": iso(utcnow()), "outcome": outcome})
        return count

    # ------------------------------------------------ public reports (report page / QR codes)
    PUBLIC_REPORTS_PER_DEVICE_PER_HOUR = 6
    PUBLIC_REPORTS_PER_HOUR = 120

    def import_public_reports(self) -> int:
        """Turn reports from the public page into admin Reports. Run every few seconds."""
        from .report_schema import category_for, issue_map, priority_for, title_for, urgency_codes

        known = issue_map()
        hour_ago = iso(utcnow() - timedelta(hours=1))
        recent = [r for _, r in self.store.query(self.C_REPORTS, source="public") if r.get("createdAt", "") >= hour_ago]
        count = 0
        for doc_id, data in self.store.query(self.C_PUBLIC_REPORTS):
            try:
                issues = [c for c in data.get("issues") or [] if c in known][:len(known)]
                device = hashlib.sha256(str(data.get("deviceId", "")).encode()).hexdigest()[:16]
                from_device = sum(r.get("deviceHash") == device for r in recent)
                if not issues:
                    continue
                if "emergency" not in issues and (from_device >= self.PUBLIC_REPORTS_PER_DEVICE_PER_HOUR
                                                  or len(recent) >= self.PUBLIC_REPORTS_PER_HOUR):
                    self.audit("public_report_dropped", {"type": "system", "username": "report-page"}, doc_id[:8],
                               {"reason": "too many reports", "device": device[:8]})
                    continue
                urgency = data.get("urgency") if data.get("urgency") in urgency_codes() else "medium"
                rating = data.get("rating")
                rating = rating if isinstance(rating, int) and 1 <= rating <= 5 else None
                category = category_for(issues)
                report = self._save_report(
                    category=category, title=title_for(issues),
                    location=str(data.get("location", ""))[:60], details=str(data.get("details", ""))[:500],
                    priority=priority_for(issues, urgency),
                    created_by={"type": "public", "username": "report page"},
                    extra={
                        "source": "public", "channel": data.get("source", "web"), "issues": issues,
                        "urgency": urgency, "rating": rating, "locationCode": data.get("locationCode") or None,
                        "deviceHash": device, "publicRef": doc_id[:6].upper(),
                        "reportedAt": _as_iso(data.get("createdAt")),
                        "contact": ({"name": str(data.get("contactName", ""))[:60],
                                     "phone": data.get("contactPhone")}
                                    if data.get("contactName") or data.get("contactPhone") else None),
                    })
                recent.append(report)
                count += 1
                # Deterministic alerting (never left to the AI): emergencies text everyone,
                # urgent problems text the janitors.
                notify = "everyone" if category == "emergency" else ("janitors" if urgency == "high" else "none")
                if notify != "none":
                    label = "EMERGENCY" if category == "emergency" else "URGENT"
                    text = alert_text(label, report["smsRef"], report["title"], report["location"], report["details"])
                    self._notify_async(report["id"], self.sms_recipients(None if notify == "everyone" else ROLE_JANITOR), text)
            finally:
                self.store.delete(self.C_PUBLIC_REPORTS, doc_id)
        return count

    def _approve_sms_contact(self, admin: Session, request_id: str) -> dict:
        """Approve a website sign-up: the janitor becomes an SMS contact (no app sign-in)."""
        now = iso(utcnow())

        def claim(current):
            if current is None:
                raise NotFound("Request not found.")
            if current.get("status") != REQUEST_PENDING:
                raise Conflict(f"This request is already {current.get('status')}.")
            current.update(status=REQUEST_ACTIVATED, reviewedBy={"uid": admin.uid, "username": admin.username},
                           reviewedAt=now, activatedAt=now)
            return current

        req = self.store.transact(self.C_REQUESTS, request_id, claim)
        uid = f"sms-{request_id}"
        self.store.set(self.C_USERS, uid, {
            "uid": uid, "username": req["username"], "usernameNormalized": req["usernameNormalized"],
            "fullName": req["fullName"], "email": req.get("email", ""), "role": ROLE_JANITOR, "status": "active",
            "smsOnly": True, "smsPhone": req["phone"], "smsAlerts": True, "smsVerifiedAt": now,
            "createdAt": now, "requestId": request_id, "approvedBy": {"uid": admin.uid, "username": admin.username},
        })
        self.store.set(self.C_USERNAMES, req["usernameNormalized"],
                       {"kind": "user", "uid": uid, "email": req.get("email", ""), "createdAt": now})
        self.audit("janitor_contact_approved", admin.actor, uid, {"username": req["username"],
                                                                  "phone": mask_phone(req["phone"])})
        try:
            self.sms.send(req["phone"], f"Welcome to AureoClean, {req['fullName'].split()[0]}! You'll now get "
                                        "restroom alerts by SMS from this number.")
            note = f"A welcome text was sent to {req['phone']}."
        except SmsError as exc:
            note = f"Couldn't send the welcome text ({exc.message})."
        return {"requestId": request_id, "username": req["username"], "contact": True,
                "message": f"{req['fullName']} is now an SMS contact. {note}"}

    # ============================================================ reports
    REPORT_CATEGORIES = {"emergency": "Emergency", "facility": "Broken facility", "supplies": "Supplies",
                         "cleanliness": "Cleanliness", "system": "System problem", "other": "Other"}
    REPORT_PRIORITIES = ("low", "normal", "high", "critical")
    REPORT_STATUSES = ("open", "in_progress", "resolved")

    def create_report(self, admin: Session, category: str, title: str, location: str = "", details: str = "",
                      priority: str = "", notify: str = "none") -> dict:
        if category not in self.REPORT_CATEGORIES:
            raise BadRequest("Choose a report type.")
        title = " ".join((title or "").split())[:80]
        if len(title) < 3:
            raise BadRequest("Give the report a short title (at least 3 characters).")
        location = " ".join((location or "").split())[:60]
        details = (details or "").strip()[:1000]
        priority = priority or ("critical" if category == "emergency" else "normal")
        if priority not in self.REPORT_PRIORITIES:
            raise BadRequest("Unknown priority.")
        if notify not in ("none", "janitors", "everyone"):
            raise BadRequest("Unknown notify option.")
        report = self._save_report(category, title, location, details, priority,
                                   {"uid": admin.uid, "username": admin.username}, {"source": "admin"})
        report_id = report.pop("id")
        self.audit("report_created", admin.actor, report_id, {"category": category, "title": title})
        sent = 0
        if notify != "none":
            recipients = self.sms_recipients(None if notify == "everyone" else ROLE_JANITOR)
            label = "EMERGENCY" if category == "emergency" else self.REPORT_CATEGORIES[category].upper()
            text = alert_text(label, report["smsRef"], title, location, details)
            self._notify_async(report_id, recipients, text)
            sent = len(recipients)
        return {"id": report_id, **report, "notifying": sent,
                "message": "Report saved." + (f" Texting {sent} people…" if sent else "")}

    def _next_report_ref(self) -> int:
        """Short, increasing report number (#1, #2, …) used in SMS replies."""
        def bump(current):
            current = current or {"next": 1}
            current["next"] = int(current.get("next", 1)) + 1
            return current
        return int(self.store.transact(self.C_COUNTERS, "reportRef", bump)["next"]) - 1

    def _save_report(self, category, title, location, details, priority, created_by, extra=None) -> dict:
        report_id = generate_id()
        now = iso(utcnow())
        report = {
            "category": category, "title": title, "location": location, "details": details, "priority": priority,
            "status": "open", "createdAt": now, "updatedAt": now, "createdBy": created_by, "notes": [],
            "smsRef": self._next_report_ref(),
            **(extra or {}),
        }
        self.store.create(self.C_REPORTS, report_id, report)
        return {"id": report_id, **report}

    def _notify_async(self, report_id: str, recipients: list[dict], text: str) -> None:
        if recipients:
            # Remember who was texted and when: needed for replies without a number and for escalation.
            def mark(current):
                if current is None:
                    return None
                current["notifiedAt"] = current.get("notifiedAt") or iso(utcnow())
                current["notifiedTo"] = sorted(set(current.get("notifiedTo") or []) | {r["uid"] for r in recipients})
                return current
            self.store.transact(self.C_REPORTS, report_id, mark)

        def work():
            ok, failed = 0, []
            for r in recipients:
                try:
                    self.sms.send(r["phone"], text)
                    ok += 1
                except SmsError as exc:
                    failed.append(f"{r['username']}: {exc.message}")
            note = f"SMS sent to {ok} of {len(recipients)}." + (f" Failed: {'; '.join(failed)[:300]}" if failed else "")
            self._add_report_note(report_id, {"type": "system", "username": "sms"}, note)

        if not recipients:
            return
        if getattr(self, "sync_notifications", False):
            work()
        else:
            threading.Thread(target=work, daemon=True, name="report-sms").start()

    def _add_report_note(self, report_id: str, by: dict, text: str, status: Optional[str] = None) -> dict:
        def update(current):
            if current is None:
                raise NotFound("Report not found.")
            now = iso(utcnow())
            note = {"at": now, "by": by.get("username", ""), "text": text}
            if status and status != current.get("status"):
                note["status"] = status
                current["status"] = status
                current["resolvedAt"] = now if status == "resolved" else None
            current.setdefault("notes", []).append(note)
            current["notes"] = current["notes"][-50:]
            current["updatedAt"] = now
            return current

        return self.store.transact(self.C_REPORTS, report_id, update)

    def update_report(self, admin: Session, report_id: str, status: str = "", note: str = "") -> dict:
        if status and status not in self.REPORT_STATUSES:
            raise BadRequest("Unknown status.")
        note = (note or "").strip()[:500]
        if not status and not note:
            raise BadRequest("Change the status or write a note.")
        labels = {"open": "Reopened", "in_progress": "Marked in progress", "resolved": "Marked resolved"}
        text = note or labels.get(status, "")
        report = self._add_report_note(report_id, admin.actor, text, status or None)
        self.audit("report_updated", admin.actor, report_id, {"status": status or None})
        return {"id": report_id, **report}

    def list_reports(self, status: str = "", category: str = "") -> list[dict]:
        filters = {}
        if status:
            filters["status"] = status
        if category:
            filters["category"] = category
        rows = [dict(data, id=doc_id) for doc_id, data in self.store.query(self.C_REPORTS, **filters)]
        order = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        # Open first, most urgent first, newest first within the same priority.
        rows.sort(key=lambda r: r.get("createdAt", ""), reverse=True)
        rows.sort(key=lambda r: (r["status"] == "resolved", order.get(r.get("priority"), 9)))
        return rows

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
            "openReports": sum(r.get("status") != "resolved" for _, r in self.store.query(self.C_REPORTS)),
        }

    def janitor_overview(self, session: Session) -> dict:
        return {
            "profile": self.me(session),
            # Filled in once the Realtime Database sensor feed is connected.
            "notifications": [],
            "message": "Restroom alerts will appear here once the sensor feed is connected.",
        }
