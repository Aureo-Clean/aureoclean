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
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
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
from .arduino import ArduinoCloud, ArduinoError
from .web import RateLimit as _RateLimit
from .insights import Insights
from .report_ai import ReportAI
from .sensor_alerts import SensorAlertError, SensorAlerts, suggest as suggest_sensor_alert
from .smart import SETTINGS as SMART_SETTINGS, Smart
from .advice import Advice
from .alert_ideas import AlertIdeas
from .llm import KeyRefused, LlmClient, LlmError, ModelGone
from .mailer import Mailer, MailError
from .sms import (
    LogSender,
    ModemSender,
    SavedSmsSettings,
    SendTracker,
    SimPromo,
    SmsError,
    SmsSender,
    TrackedSender,
    detect_modem,
    mask_phone,
    normalize_phone,
)

PH_TZ = timezone(timedelta(hours=8))  # promo dates are counted in Philippine time
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


def invite_email(inviter: str, code: str, until: str, email: str, web_url: str = "") -> tuple[str, str, str]:
    """The admin invite email: subject, plain text and HTML (inline styles: email apps strip the rest)."""
    import html as _html

    first = (f"Go to the AureoClean screen, or open {web_url} on a phone or laptop connected to the school Wi-Fi."
             if web_url else "Go to the AureoClean screen (the Raspberry Pi touchscreen), or the AureoClean website "
                             "on the school Wi-Fi if an administrator has turned it on.")
    steps = [
        first,
        "On the sign-in screen, choose \u201cAdmin invite code\u201d.",
        f"Enter the code, your name, a username, this email address ({email}) and a password.",
        "We'll email you a verification link. Open it, then sign in.",
    ]
    subject = "You're invited to AureoClean as an administrator"
    text = (f"Hi,\n\n{inviter} invited you to help manage AureoClean, the restroom monitoring system at "
            f"STI College Caloocan.\n\nYour invite code: {code}\nIt works once, until {until}.\n\n"
            + "\n".join(f"{i}. {t}" for i, t in enumerate(steps, 1))
            + "\n\nIf you weren't expecting this, you can ignore this email.\n\n— AureoClean")
    e = _html.escape
    items = "".join(f'<li style="margin:0 0 8px">{e(t)}</li>' for t in steps)
    html = f"""<!doctype html><html><body style="margin:0;background:#F3F6FA;font-family:Inter,Segoe UI,Roboto,Arial,sans-serif;color:#0F1E36">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F3F6FA;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background:#FFFFFF;border:1px solid #E3E9F1;border-radius:16px">
<tr><td style="background:#14336E;border-radius:16px 16px 0 0;padding:18px 24px;color:#FFFFFF;font-size:20px;font-weight:700">
<span style="color:#F2C14E">Aureo</span>Clean</td></tr>
<tr><td style="padding:24px">
<h1 style="margin:0 0 8px;font-size:20px">You're invited as an administrator</h1>
<p style="margin:0 0 18px;color:#5B6B80;font-size:14px;line-height:1.5">{e(inviter)} invited you to help manage AureoClean,
the restroom monitoring system at STI College Caloocan.</p>
<div style="background:#F0F4FA;border-radius:12px;padding:16px;text-align:center;margin:0 0 6px">
<div style="font-size:12px;color:#5B6B80;margin-bottom:6px">Your invite code</div>
<div style="font-family:Menlo,Consolas,monospace;font-size:24px;font-weight:700;letter-spacing:3px;color:#14336E">{e(code)}</div></div>
<p style="margin:0 0 18px;font-size:12px;color:#5B6B80;text-align:center">Works once, until {e(until)}.</p>
<ol style="margin:0 0 16px;padding-left:20px;font-size:14px;line-height:1.5">{items}</ol>
<p style="margin:0;font-size:12px;color:#5B6B80">Not expecting this? You can ignore this email.</p>
</td></tr></table></td></tr></table></body></html>"""
    return subject, text, html


def public_locations() -> list[dict]:
    """The restroom list from docs/assets/config.js (the file the QR codes and report page use)."""
    path = Path(__file__).resolve().parent.parent / "docs" / "assets" / "config.js"
    try:
        text = path.read_text()
    except OSError:
        return []
    return [{"code": c, "name": n} for c, n in
            re.findall(r'\{\s*code:\s*"([^"]+)",\s*name:\s*"([^"]+)"\s*\}', text)]


def idle_lock_minutes() -> int:
    """Minutes without a touch before the desktop screen locks (0 = never)."""
    try:
        return max(0, min(int(os.environ.get("AUREOCLEAN_IDLE_LOCK_MINUTES", "5")), 240))
    except ValueError:
        return 5


def _public_profile(profile: dict) -> dict:
    keys = ("uid", "username", "fullName", "email", "role", "status", "createdAt", "lastLoginAt",
            "smsPhone", "smsAlerts", "smsVerifiedAt")
    out = {k: profile.get(k) for k in keys}
    out["smsAlerts"] = bool(out["smsAlerts"])
    out["hasLockPin"] = bool(profile.get("lockPinHash"))
    out["smsOnly"] = bool(profile.get("smsOnly"))
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
    # Every sender is wrapped so System health can show whether texts are going out.
    @property
    def sms(self):
        return self._sms

    @sms.setter
    def sms(self, sender):
        self._sms = sender if isinstance(sender, TrackedSender) else TrackedSender(sender, self.send_tracker)

    def __init__(self, settings: Settings, store: DocStore, identity: Identity, sms: Optional[SmsSender] = None):
        self.settings = settings
        self.send_tracker = SendTracker()
        self.sms = sms or LogSender(settings.log_dir / "sms_outbox.log")
        self.sim_promo = SimPromo(settings.sms_settings_file.with_name("sim_promo.json"))
        self.mailer = Mailer(settings.sms_settings_file.with_name("mail.json"))
        self.arduino = ArduinoCloud(settings.sms_settings_file.with_name("arduino.json"))
        self.llm = LlmClient(settings.sms_settings_file.with_name("llm.json"))
        self.insights = Insights(self)
        self.report_ai = ReportAI(self)
        self.sensor_alerts = SensorAlerts(self, settings.sms_settings_file.with_name("sensor_alerts.json"))
        self.smart = Smart(self, settings.sms_settings_file.parent)
        from .sensor_feed import SensorFeed

        def firebase_app():
            store = self.store
            if not hasattr(store, "remote") or not store.online:
                return None
            return getattr(store.remote(), "_app", None)
        self.sensor_feed = SensorFeed(firebase_app, settings.sensor_path)
        if settings.mode == "firebase" and settings.firebase_database_url:
            self.arduino.feed = self.sensor_feed      # faster values from the Realtime Database
        self.advice = Advice(self)
        self.alert_ideas = AlertIdeas(self)
        self._seen_at: dict[str, float] = {}   # uid → when lastSeenAt was last saved (for "active today")
        self._control_limit = _RateLimit(30, 60)   # dashboard controls: 30 changes a minute per person
        self._unlock_cache: dict[str, tuple[float, dict]] = {}  # uid -> (time, profile) for a fast lock screen
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
        self.C_DELETED_REPORTS = f"{prefix}DeletedReports"   # the deleted bin (30 days, restorable)
        self.C_SESSIONS = f"{prefix}Sessions"
        self.sessions = SessionManager(settings.session_hours, store, self.C_SESSIONS)
        self.login_failures = FailureTracker(settings.max_login_failures, settings.login_lockout_minutes)
        # Guessing activation / invite codes: 10 wrong tries per key per 15 min.
        self.code_failures = FailureTracker(10, 15)
        from .two_way import TwoWaySms
        self.two_way = TwoWaySms(self)
        from .health import HealthMonitor
        self.health = HealthMonitor(self)
        self.job_status: dict[str, dict] = {}  # background jobs: last run + error (for the health panel)
        # SMS verification codes: at most 3 texts per user per 15 minutes.
        self.sms_sends = FailureTracker(3, 15)
        self.unlock_failures = FailureTracker(5, 15)  # idle-lock screen: 5 wrong tries ends the session

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
        return {"token": session.token, "expiresAt": iso(session.expires_at),
                "user": {**_public_profile(profile), "idleLockMinutes": idle_lock_minutes()}}

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
        self.mark_seen(session.uid)
        return session

    def mark_seen(self, uid: str) -> None:
        """Remember that this person used AureoClean (app or SMS reply) — emergencies text everyone active today."""
        now = time.monotonic()
        if now - self._seen_at.get(uid, -1e9) < 600:
            return
        self._seen_at[uid] = now

        def seen(current):
            if not current:
                return None
            current["lastSeenAt"] = iso(utcnow())
            return current
        try:
            self.store.transact(self.C_USERS, uid, seen)
        except Exception:  # noqa: BLE001 - never block a request for this
            self._seen_at.pop(uid, None)

    def verify_admin(self, identifier: str, password: str) -> dict:
        """Check an admin's username/email and password without starting a session
        (used to leave kiosk mode from the sign-in screen)."""
        ident = (identifier or "").strip()
        key = f"login:{ident.lower()}"
        wait = self.login_failures.locked_for(key)
        if wait:
            raise TooManyAttempts(f"Too many failed attempts. Try again in {max(1, wait // 60)} minute(s).")
        if not ident or not password:
            raise BadRequest("Enter an administrator's username and password.")
        _, uid = self._check_credentials(ident, password, key)
        profile = self.store.get(self.C_USERS, uid) or {}
        if profile.get("role") != ROLE_ADMIN or profile.get("status") != "active":
            raise Forbidden("Only an administrator can do this.")
        self.login_failures.reset(key)
        self.audit("kiosk_exit", {"type": "admin", "uid": uid, "username": profile.get("username")}, uid)
        return {"ok": True, "username": profile.get("username")}

    def me(self, session: Session) -> dict:
        profile = self.store.get(self.C_USERS, session.uid)
        if not profile or profile.get("status") != "active":
            self.sessions.revoke(session.token)
            raise Unauthorized("Your account is no longer active.")
        # Tell the screen whether texts are really sent (modem) or only logged (test mode).
        return {**_public_profile(profile), "smsMode": self.sms.mode, "idleLockMinutes": idle_lock_minutes()}

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

    # ============================================================ screen lock (kiosk)
    def set_lock_pin(self, session: Session, password: str, pin: str) -> dict:
        """A 4-6 digit PIN unlocks the idle screen; the account password is needed to set or remove it."""
        pin = (pin or "").strip()
        if pin and not re.fullmatch(r"\d{4,6}", pin):
            raise BadRequest("The PIN must be 4 to 6 digits.")
        if pin and (len(set(pin)) == 1 or pin in ("1234", "12345", "123456", "4321", "654321")):
            raise BadRequest("That PIN is too easy to guess. Pick another one.")
        try:
            ok = self.identity.verify_password(session.email, password or "")
        except AccountDisabled:
            raise Forbidden("This account has been disabled.")
        if ok != session.uid:
            raise Unauthorized("Your password is incorrect.")
        self.store.transact(self.C_USERS, session.uid,
                            lambda u: {**u, "lockPinHash": hash_secret(pin) if pin else None})
        self._unlock_cache.pop(session.uid, None)
        self.audit("lock_pin_set" if pin else "lock_pin_removed", session.actor, session.uid)
        return {"hasLockPin": bool(pin)}

    def unlock(self, session: Session, secret: str) -> dict:
        """Unlock the idle screen with the PIN (or the password when no PIN is set).
        Too many wrong tries signs the session out, so a full sign-in is needed."""
        key = f"unlock:{session.uid}"
        hit = self._unlock_cache.get(session.uid)
        if hit and time.monotonic() - hit[0] < 600:
            profile = hit[1]
        else:
            profile = self.store.get(self.C_USERS, session.uid) or {}
            self._unlock_cache[session.uid] = (time.monotonic(), profile)
        if profile.get("status") != "active":
            self.sessions.revoke(session.token)
            raise Unauthorized("Your account is no longer active.")
        secret = secret or ""
        pin_hash = profile.get("lockPinHash")
        if pin_hash:
            ok = verify_secret(secret, pin_hash)
        else:
            try:
                ok = bool(secret) and self.identity.verify_password(session.email, secret) == session.uid
            except AccountDisabled:
                ok = False
        if ok:
            self.unlock_failures.reset(key)
            return {"ok": True}
        self.unlock_failures.record_failure(key)
        if self.unlock_failures.locked_for(key):
            self.unlock_failures.reset(key)
            self.sessions.revoke(session.token)
            self.audit("unlock_locked_out", session.actor, session.uid)
            raise Unauthorized("Too many wrong tries. You've been signed out — sign in with your password.")
        raise BadRequest("Wrong PIN." if pin_hash else "Wrong password.")

    # ============================================================ admin invites
    def create_admin_invite(self, admin: Session, email: str = "", hours: Optional[int] = None, phone: str = "",
                            web_url: str = "") -> dict:
        """Make a one-time invite code for a new administrator, and send it to them:
        by email (when email is set up) and/or by SMS (when a mobile number is given)."""
        email = _check(email, validate_email) if (email or "").strip() else ""
        if (phone or "").strip():
            try:
                phone = normalize_phone(phone)
            except ValueError as exc:
                raise BadRequest(str(exc))
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
                "phone": mask_phone(phone) if phone else None,
                "status": "active",
                "createdBy": {"uid": admin.uid, "username": admin.username},
                "createdAt": iso(utcnow()),
                "expiresAt": expires,
            },
        )
        self.audit("admin_invite_created", admin.actor, invite_id[:12], {"email": email or None, "expiresAt": expires})
        result = {"inviteId": invite_id, "code": code, "email": email or None, "expiresAt": expires,
                  "emailed": False, "emailError": None, "texted": False, "smsError": None}
        inviter = admin.full_name or admin.username
        when = parse_iso(expires).astimezone(PH_TZ).strftime("%b %d, %I:%M %p").replace(" 0", " ")
        if email:
            if not self.mailer.configured:
                result["emailError"] = "Email isn't set up yet, so nothing was sent. Give them the code yourself."
            else:
                try:
                    subject, text, html = invite_email(inviter, code, when, email, web_url)
                    self.mailer.send(email, subject, text, html)
                    result["emailed"] = True
                    self.audit("admin_invite_emailed", admin.actor, invite_id[:12], {"email": email})
                except MailError as exc:
                    result["emailError"] = str(exc)
        if phone:
            try:
                self.sms.send(phone, f"AureoClean: {inviter.split()[0]} invited you as an admin. Code {code} "
                                     f"(until {when}). On AureoClean choose Admin invite code."[:160])
                result["texted"] = True
            except SmsError as exc:
                result["smsError"] = exc.message
        return result

    # ------------------------------------------------------------------ email set-up
    # ------------------------------------------------------------------ Arduino Cloud (live sensors)
    def can_control(self, session: Optional[Session]) -> bool:
        """Admins may use dashboard controls; janitors only if an admin allowed it."""
        if session is None:
            return False
        return session.role == ROLE_ADMIN or (session.role == ROLE_JANITOR and self.arduino.settings()["janitorsControl"])

    def sensors(self, session: Optional[Session] = None) -> dict:
        if not self.arduino.configured:
            return {"configured": False, "things": []}
        try:
            return {"configured": True, **self.arduino.readings(), "canControl": self.can_control(session),
                    "janitorsControl": self.arduino.settings()["janitorsControl"],
                    "alerts": self.sensor_alerts.for_screens(), "smart": self.smart.problems_for_screens(),
                    "isAdmin": bool(session and session.role == ROLE_ADMIN)}
        except ArduinoError as exc:
            raise ServiceUnavailable(str(exc))

    NUMBER_TYPES = ("INT", "FLOAT", "PERCENTAGE", "TEMPERATURE", "HUMIDITY", "LENGTH", "VOLUME", "LUMINANCE",
                    "ILLUMINANCE", "PRESSURE", "DISTANCE", "VOLTAGE", "CURRENT", "POWER", "ENERGY", "FLOW",
                    "CONCENTRATION", "GAS", "SOUND", "ANGLE", "FREQUENCY", "SPEED", "MASS", "TIME", "HOME_TEMPERATURE")

    def sensor_control(self, session: Session, thing_id: str, prop_id: str, value) -> dict:
        """Change a writable Arduino variable from a dashboard control (switch, slider, button …)."""
        if not self.can_control(session):
            raise Forbidden("Only administrators can use the controls (an admin can allow janitors in Arduino settings).")
        if not self._control_limit.allow(session.uid):
            raise BadRequest("Too many changes in a short time — wait a few seconds.")
        try:
            data = self.arduino.readings()
        except ArduinoError as exc:
            raise ServiceUnavailable(str(exc))
        prop = next((v for t in data["things"] if t["id"] == thing_id for v in t["properties"] if v["id"] == prop_id), None)
        if prop is None:
            raise NotFound("That sensor value isn't on Arduino Cloud any more.")
        if not prop.get("writable"):
            raise BadRequest(f"“{prop['label']}” is read-only in Arduino Cloud.")
        typ = (prop.get("type") or "").upper()
        limits = [w["options"] for p in data.get("pages", []) for w in p["widgets"]
                  if any(v.get("id") == prop_id for v in w["values"])]
        if typ in ("BOOL", "STATUS", "HOME_SWITCH", "HOME_CONTACT_SENSOR", "HOME_MOTION_SENSOR") or isinstance(prop.get("value"), bool):
            if not isinstance(value, bool):
                raise BadRequest("This one is on/off.")
            shown = "On" if value else "Off"
        elif typ.startswith(self.NUMBER_TYPES) or (isinstance(prop.get("value"), (int, float)) and not isinstance(prop.get("value"), bool)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) > 1e9:
                raise BadRequest("Type a number.")
            if typ == "INT" or (isinstance(prop.get("value"), int) and float(value).is_integer()):
                value = int(round(value))
            for o in limits:
                lo, hi = o.get("min"), o.get("max")
                if isinstance(lo, (int, float)) and value < lo or isinstance(hi, (int, float)) and value > hi:
                    raise BadRequest(f"Pick a value between {lo} and {hi}.")
            shown = f"{value}{prop.get('unit') or ''}"
        elif typ in ("STRING", "CHARSTRING") or isinstance(prop.get("value"), str):
            if not isinstance(value, str):
                raise BadRequest("Type a message.")
            value = " ".join(value.split())[:256]
            shown = f"“{value[:40]}”"
        else:
            raise BadRequest("This kind of control (colour, map, schedule …) isn't supported in AureoClean yet — use Arduino Cloud.")
        try:
            self.arduino.publish(thing_id, prop_id, value)
        except ArduinoError as exc:
            raise ServiceUnavailable(str(exc))
        self.audit("sensor_control", session.actor, prop_id, {"thing": thing_id, "variable": prop.get("name"), "value": str(value)[:60]})
        return {"value": value, "shown": shown, "message": f"{prop['label']} → {shown}"}

    def sensor_series(self, thing_id: str, prop_id: str, hours: int) -> dict:
        try:
            return {"points": self.arduino.series(thing_id, prop_id, hours), "hours": hours}
        except ArduinoError as exc:
            raise ServiceUnavailable(str(exc))

    def set_arduino_options(self, admin: Session, janitors_control: bool) -> dict:
        try:
            self.arduino.set_options(janitors_control)
        except ArduinoError as exc:
            raise BadRequest(str(exc))
        self.audit("arduino_options", admin.actor, None, {"janitorsControl": janitors_control})
        return {"janitorsControl": janitors_control}

    def sensor_history(self) -> dict:
        if not self.arduino.configured:
            return {"series": {}, "hours": 0}
        try:
            return self.arduino.history()
        except ArduinoError as exc:
            raise ServiceUnavailable(str(exc))

    def set_arduino(self, admin: Session, client_id: str, client_secret: str, org: str = "") -> dict:
        try:
            self.arduino.save(client_id, client_secret, org)
            data = self.arduino.readings()  # check the key works straight away
        except ArduinoError as exc:
            raise BadRequest(str(exc))
        self.audit("arduino_connected", admin.actor, None)
        count = sum(len(t["properties"]) for t in data["things"])
        names = [p["name"] for p in data.get("pages", [])]
        shown = ", ".join(names[:4]) + ("…" if len(names) > 4 else "")
        pages = f"{len(names)} page{'s' if len(names) != 1 else ''} ({shown})" if names else "no dashboards yet"
        return {"configured": True, "message": f"Connected: {pages}, {count} sensor value{'s' if count != 1 else ''}."}

    # ------------------------------------------------------------------ AI insights (advice only)
    def insights_status(self) -> dict:
        return self.insights.status()

    def set_llm(self, admin: Session, provider: str, api_key: str = "", model: str = "", url: str = "") -> dict:
        """Save the AI settings, then check them with a tiny question; the old settings stay if it fails."""
        old = self.llm.file.read_text() if self.llm.file.exists() else None

        def put_back():
            if old is None:
                self.llm.clear()
            else:
                self.llm.file.write_text(old)
        try:
            self.llm.save(provider, api_key, model, url)
        except LlmError as exc:
            raise BadRequest(str(exc))
        try:
            reply = self.llm.ask("Reply with JSON only.", 'Reply exactly with {"ok": true}', max_tokens=256, fast=True)
        except (KeyRefused, ModelGone) as exc:
            put_back()            # the key or model really is wrong: don't keep it
            raise BadRequest(str(exc))
        except LlmError as exc:
            # The key itself wasn't refused (busy, free limit for now, slow internet …): keep the
            # settings — set up once — and say what happened.
            self.audit("ai_set_up", admin.actor, None, {"provider": provider, "tested": False})
            self.insights.last_error = f"Saved, but the first test didn't finish: {exc}"
            st = self.llm.public_status()
            return {**st, "warning": True,
                    "message": f"Saved {st['providerLabel']}. The test didn't finish: {exc} Insights will work once it answers."}
        self.audit("ai_set_up", admin.actor, None, {"provider": provider, "model": self.llm.settings()["model"]})
        st = self.llm.public_status()
        note = "" if '"ok"' in reply.replace(" ", "") else " (it answered, but not quite as asked — results may vary)"
        sw = getattr(self.llm, "switched", None)
        if sw and sw[0]:
            note += f" — “{sw[0]}” is no longer offered, so the newest model was picked"
        elif sw:
            note += " — picked automatically"
        return {**st, "message": f"Connected to {st['providerLabel']} · {st['model']}{note}."}

    def clear_llm(self, admin: Session) -> dict:
        self.llm.clear()
        self.audit("ai_removed", admin.actor, None)
        return self.llm.public_status()

    def set_insights_daily(self, admin: Session, daily: Optional[bool] = None, hour: Optional[int] = None,
                           auto_tips: Optional[bool] = None) -> dict:
        self.llm.set_daily(daily, hour, auto_tips)
        self.audit("insights_daily", admin.actor, None, {"daily": daily, "hour": hour, "autoTips": auto_tips})
        return self.llm.public_status()

    def report_tip(self, session: Session, report_id: str) -> dict:
        try:
            return self.report_ai.tip(report_id, session.actor)
        except LlmError as exc:
            raise BadRequest(str(exc))

    def report_fill(self, session: Session, text: str) -> dict:
        try:
            return self.report_ai.fill(text, public_locations(), admin_form=session.role == ROLE_ADMIN)
        except LlmError as exc:
            raise BadRequest(str(exc))

    def period_summary(self, admin: Session, days: int, force: bool = False) -> dict:
        try:
            return self.report_ai.period(days, admin.actor, force)
        except LlmError as exc:
            raise BadRequest(str(exc))

    def period_summary_saved(self, days: int) -> dict:
        return {"summary": self.report_ai.period_saved(days), "aiReady": self.llm.configured}

    def start_insights(self, admin: Session) -> dict:
        try:
            return self.insights.start(admin.actor)
        except LlmError as exc:
            raise BadRequest(str(exc))

    def ask_insights(self, admin: Session, question: str) -> dict:
        try:
            return self.insights.ask(admin.actor, question)
        except LlmError as exc:
            raise BadRequest(str(exc))

    def insights_today(self) -> dict:
        return self.insights.today()

    # ------------------------------------------------------------------ sensor alerts (fixed rules, no AI)
    def sensor_alerts_overview(self) -> dict:
        return {**self.sensor_alerts.overview(), "locations": public_locations(), "configured": self.arduino.configured}

    def save_sensor_alert(self, admin: Session, data: dict) -> dict:
        try:
            values = self.arduino.readings()
        except ArduinoError as exc:
            raise ServiceUnavailable(str(exc))
        prop = next((v for t in values["things"] if t["id"] == data.get("thingId") for v in t["properties"]
                     if v["id"] == data.get("propertyId")), None)
        if prop is None:
            raise NotFound("That sensor value isn't on Arduino Cloud any more.")
        known = {loc["name"]: loc["code"] for loc in public_locations()}
        location = data.get("location") or ""
        if location and location not in known:
            raise BadRequest("Pick the restroom from the list.")
        try:
            rule = self.sensor_alerts.save_rule({**data, "variable": prop["name"], "label": data.get("label") or prop["label"],
                                                 "thing": prop.get("thing", ""), "unit": prop.get("unit", ""),
                                                 "locationCode": known.get(location)}, by=admin.username)
        except SensorAlertError as exc:
            raise BadRequest(str(exc))
        self.audit("sensor_alert_saved", admin.actor, rule["id"], {"rule": self.sensor_alerts.describe(rule),
                                                                   "level": rule["level"], "on": rule["on"]})
        if not getattr(self, "sync_notifications", False):   # check at once, so "right away" really is
            threading.Thread(target=lambda: self._check_sensor_alerts_quietly(), daemon=True, name="sensor-check").start()
        return {"rule": rule, "message": f"Alert saved: {self.sensor_alerts.describe(rule)}."}

    def _check_sensor_alerts_quietly(self) -> None:
        try:
            self.run_job("sensor_alerts", self.sensor_alerts.check)
        except Exception:  # noqa: BLE001 - shown in the alert list and System health
            pass

    def test_sensor_alert(self, admin: Session, rule_id: str) -> dict:
        """A TEST report and texts for one alert, to check reports and SMS end to end."""
        try:
            report = self.sensor_alerts.send_test(rule_id, admin.username)
        except SensorAlertError as exc:
            raise BadRequest(str(exc))
        self.audit("sensor_alert_tested", admin.actor, report["id"], {"title": report["title"]})
        n = report.get("texted", 0)
        if n == 0:
            texts = ("No text: this alert is “Report only”." if report["priority"] == "normal" else
                     "Nobody was texted: no one has SMS alerts on with a verified number (My profile → SMS alerts).")
        elif self.sms.mode != "modem":
            texts = f"SMS is in TEST MODE, so the {n} text(s) were only written to the log — System → SMS → Find & use modem."
        else:
            texts = f"Texting {n} people now."
        return {"id": report["id"], "ref": report["smsRef"], "texted": n,
                "message": f"Test report #{report['smsRef']} made. {texts}"}

    def delete_sensor_alert(self, admin: Session, rule_id: str) -> dict:
        try:
            rule = self.sensor_alerts.delete_rule(rule_id)
        except SensorAlertError as exc:
            raise NotFound(str(exc))
        self.audit("sensor_alert_deleted", admin.actor, rule_id, {"rule": self.sensor_alerts.describe(rule)})
        return {"message": "Alert deleted."}

    def sensor_alert_suggestion(self, thing_id: str, prop_id: str) -> dict:
        try:
            values = self.arduino.readings()
        except ArduinoError as exc:
            raise ServiceUnavailable(str(exc))
        prop = next((v for t in values["things"] if t["id"] == thing_id for v in t["properties"] if v["id"] == prop_id), None)
        if prop is None:
            raise NotFound("That sensor value isn't on Arduino Cloud any more.")
        history = []
        if isinstance(prop.get("value"), (int, float)) and not isinstance(prop.get("value"), bool):
            try:
                history = self.arduino.series(thing_id, prop_id, 24)
            except ArduinoError:
                history = []
        # The restroom used by another alert on the same thing is a good guess.
        same = next((r for r in self.sensor_alerts.rules() if r["thingId"] == thing_id and r.get("location")), None)
        unit_loc = (self.smart.config()["units"].get(thing_id) or {}).get("location")
        return {**suggest_sensor_alert(prop, history), "location": unit_loc or (same["location"] if same else ""),
                "label": prop["label"], "unit": prop.get("unit", "")}

    # ------------------------------------------------------------------ smart monitoring + daily advice
    def smart_overview(self) -> dict:
        return {**self.smart.overview(), "configured": self.arduino.configured}

    def smart_cleaned(self, session: Session, thing_id: str) -> dict:
        try:
            c = self.smart.cleaned(thing_id)
        except ValueError as exc:
            raise BadRequest(str(exc))
        name = (self.smart.units_seen.get(thing_id) or {}).get("name", thing_id)
        self.audit("smart_cleaned", session.actor, thing_id, {"unit": name, "uses": c.get("uses")})
        # Close an open "cleaning due" job for this unit.
        p = self.smart.state["problems"].get(f"{thing_id}:clean")
        if p and p.get("reportId") and self.smart._status(p["reportId"]) not in (None, "resolved"):
            self._add_report_note(p["reportId"], session.actor, f"Cleaned ({session.username}, via Cleaned button)", "resolved")
        return {"message": f"{name}: counting uses from zero again. Thank you!"}

    def smart_settings(self) -> dict:
        cfg = self.smart.config()
        units = []
        try:
            data = self.arduino.readings() if self.arduino.configured else {"things": []}
            units = [{"id": u["id"], "name": u["name"], "location": u["location"], "kind": u["kind"], "on": u["on"],
                      "variables": sorted(u["vars"])} for u in self.smart._units(data, cfg)]
        except ArduinoError:
            pass
        return {"config": cfg, "settings": [{"key": k, "min": v[0], "max": v[1], "label": v[4], "value": cfg[k]}
                                            for k, v in SMART_SETTINGS.items()],
                "units": units, "locations": public_locations(), "quiet": self.smart.quiet(cfg=cfg)}

    def save_smart_settings(self, admin: Session, changes: dict) -> dict:
        units = changes.get("units")
        if isinstance(units, dict):
            known = {loc["name"]: loc["code"] for loc in public_locations()}
            for u in units.values():
                if isinstance(u, dict) and u.get("location") and u["location"] not in known:
                    raise BadRequest("Pick the restroom from the list.")
                if isinstance(u, dict) and u.get("location"):
                    u["locationCode"] = known[u["location"]]
        try:
            cfg = self.smart.save_config(changes)
        except ValueError as exc:
            raise BadRequest(str(exc))
        self.audit("smart_settings", admin.actor, None, {k: (v if k != "units" else len(v)) for k, v in changes.items()})
        return {"config": cfg, "message": "Smart monitoring settings saved."}

    # ------------------------------------------------------------------ the Alerts page
    def alerts_page(self) -> dict:
        cfg = self.smart.config()
        ov = self.smart.overview()
        custom = self.sensor_alerts.overview()
        now = []
        for u in ov["units"]:
            for p in u["problems"]:
                if p["tier"] >= 1 or p["active"]:
                    now.append({"unit": u["name"], "where": u["location"] or u["name"], "title": p["title"], "tier": p["tier"],
                                "tierText": p["tierText"], "ref": p["ref"], "reportId": p["reportId"], "active": p["active"],
                                "for": p["for"], "source": "automatic"})
        for r in custom["rules"]:
            if r["on"] and r["state"] in ("alert", "over"):
                now.append({"unit": r.get("thing") or "", "where": r.get("location") or r.get("thing") or "", "title": r["text"],
                            "tier": {"report": 1, "urgent": 2, "emergency": 3}[r["level"]] if r["state"] == "alert" else 0,
                            "tierText": "Alert sent" if r["state"] == "alert" else "Over the limit", "ref": (r.get("active") or {}).get("ref"),
                            "reportId": (r.get("active") or {}).get("reportId"), "active": True, "for": "", "source": "yours",
                            "why": r.get("why")})
        now.sort(key=lambda x: (-x["tier"], not x["active"]))
        return {"configured": self.arduino.configured, "enabled": cfg["enabled"], "quiet": ov["quiet"], "quietHours": ov["quietHours"],
                "checker": ov["checker"], "customChecker": custom["checker"], "now": now, "units": ov["units"],
                "automatic": self.smart.automatic_rules(cfg), "custom": custom, "ideas": self.alert_ideas.view(),
                "smsMode": self.sms.mode}

    def active_alert_count(self) -> int:
        n = sum(1 for u in self.smart.units_seen.values() for p in u["problems"] if p["active"] and p["tier"] >= 1)
        try:
            n += sum(1 for r in self.sensor_alerts.overview()["rules"] if r["on"] and r["state"] == "alert")
        except Exception:  # noqa: BLE001
            pass
        return n

    def refresh_alert_ideas(self, admin: Session) -> dict:
        if not self.arduino.configured:
            raise BadRequest("Connect Arduino Cloud first.")
        try:
            return self.alert_ideas.run(admin.actor)
        except ArduinoError as exc:
            raise ServiceUnavailable(str(exc))

    def add_alert_idea(self, admin: Session, idea_id: str) -> dict:
        idea = self.alert_ideas.find(idea_id)
        if idea is None:
            raise NotFound("That suggestion is gone — find new suggestions.")
        rule = dict(idea["rule"])
        same = next((r for r in self.sensor_alerts.rules() if r["thingId"] == rule["thingId"] and r.get("location")), None)
        unit_loc = (self.smart.config()["units"].get(rule["thingId"]) or {}).get("location")
        rule["location"] = unit_loc or (same or {}).get("location") or ""
        result = self.save_sensor_alert(admin, rule)
        self.audit("alert_idea_added", admin.actor, result["rule"]["id"], {"idea": idea["title"], "source": idea["source"]})
        return result

    def dismiss_alert_idea(self, admin: Session, idea_id: str) -> dict:
        self.alert_ideas.dismiss(idea_id)
        return {"message": "Suggestion dismissed."}

    def advice_today(self, session: Session) -> dict:
        return self.advice.today(session.role)

    def refresh_advice(self, admin: Session) -> dict:
        saved = self.advice.saved() or {}
        if saved.get("createdAt") and not saved.get("live"):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(saved["createdAt"])).total_seconds()
            if age < 120:
                raise BadRequest("The advice was just made — wait a minute.")
        if self.advice.running:
            return {"running": True}
        self.advice.start(admin.actor, "manual")
        return {"running": True, "message": "Making today's advice…"}

    def apply_advice(self, admin: Session, setting: str, value) -> dict:
        from .advice import TUNABLE
        if setting not in TUNABLE:
            raise BadRequest("That setting can't be changed from the advice.")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise BadRequest("Bad value.")
        try:
            self.smart.save_config({setting: value})
        except ValueError as exc:
            raise BadRequest(str(exc))
        saved = self.advice.saved()
        if saved:
            saved["suggestions"] = [s for s in saved.get("suggestions") or [] if s.get("setting") != setting]
            self.store.set(self.advice.collection, "advice", saved)
        label = SMART_SETTINGS[setting][4]
        self.audit("smart_settings", admin.actor, None, {setting: value, "from": "advice"})
        return {"message": f"Changed: {label} → {value:g}."}

    def clear_arduino(self, admin: Session) -> dict:
        self.arduino.clear()
        self.audit("arduino_disconnected", admin.actor, None)
        return {"configured": False}

    def mail_status(self) -> dict:
        return self.mailer.public_status()

    def set_mail(self, admin: Session, user: str, password: str) -> dict:
        try:
            self.mailer.save(user, password)
        except MailError as exc:
            raise BadRequest(str(exc))
        self.audit("mail_set_up", admin.actor, None, {"user": user.strip()})
        return self.mailer.public_status()

    def clear_mail(self, admin: Session) -> dict:
        self.mailer.clear()
        self.audit("mail_removed", admin.actor, None)
        return self.mailer.public_status()

    def send_test_email(self, admin: Session) -> dict:
        if not admin.email:
            raise BadRequest("Your account has no email address.")
        try:
            self.mailer.send(admin.email, "AureoClean test email",
                             f"Hi {admin.full_name or admin.username},\n\nEmail from AureoClean is working. "
                             "Admin invites will be sent from this address.\n\n— AureoClean")
        except MailError as exc:
            raise ServiceUnavailable(str(exc))
        return {"message": f"Test email sent to {admin.email}. Check the inbox (and Spam)."}

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

    def emergency_recipients(self, exclude_uid: Optional[str] = None) -> list[dict]:
        """Real emergencies text everyone active today: people who used the app or replied by SMS today,
        plus every SMS-only contact (they never sign in). If nobody was active today, everyone with SMS on."""
        from .analysis import LOCAL_TZ
        today = datetime.now(LOCAL_TZ).date()

        def active(u):
            if u.get("smsOnly"):
                return True
            for k in ("lastSeenAt", "lastLoginAt"):
                try:
                    if u.get(k) and datetime.fromisoformat(u[k]).astimezone(LOCAL_TZ).date() == today:
                        return True
                except ValueError:
                    pass
            return False
        everyone = [u for _, u in self.store.query(self.C_USERS, status="active")
                    if u.get("smsAlerts") and u.get("smsPhone") and u["uid"] != exclude_uid]
        chosen = [u for u in everyone if active(u)] or everyone
        return [{"uid": u["uid"], "username": u["username"], "role": u["role"], "phone": u["smsPhone"]} for u in chosen]

    def alert_people(self, report_id: str, audience: str, text: str, exclude_uid: Optional[str] = None,
                     force: bool = False) -> int:
        """Text an alert. "emergency" → everyone active today, at any hour. "janitors" → the janitors, but
        during quiet hours (7 PM–5 AM) the text waits until morning (sent then if the job is still open).
        Returns how many were texted (0 when held)."""
        if audience == "emergency":
            recipients = self.emergency_recipients(exclude_uid)
        else:
            if not force and self.smart.quiet():
                def hold(current):
                    if current is None:
                        return None
                    current["smsDeferred"] = {"audience": audience, "text": text, "at": iso(utcnow())}
                    return current
                self.store.transact(self.C_REPORTS, report_id, hold)
                self._add_report_note(report_id, {"type": "system", "username": "sms"},
                                      f"Quiet hours ({self.smart.quiet_text()}): the text to the janitors goes out "
                                      "in the morning if this is still open.")
                return 0
            recipients = [r for r in self.sms_recipients(ROLE_JANITOR) if r["uid"] != exclude_uid]
        self._notify_async(report_id, recipients, text)
        return len(recipients)

    def send_deferred_texts(self) -> int:
        """After quiet hours: send the texts that were held overnight (only for jobs still open)."""
        if self.smart.quiet():
            return 0
        sent = 0
        for status in ("open", "in_progress"):
            for rid, r in self.store.query(self.C_REPORTS, status=status):
                held = r.get("smsDeferred")
                if not held:
                    continue

                def clear(current):
                    if current is None or not current.get("smsDeferred"):
                        return None
                    current.pop("smsDeferred", None)
                    return current
                if self.store.transact(self.C_REPORTS, rid, clear) is None:
                    continue
                if status == "in_progress":   # someone already has it
                    continue
                sent += self.alert_people(rid, held.get("audience", "janitors"), held["text"], force=True)
        return sent

    # ------------------------------------------------ admin SMS tools
    def sms_settings(self) -> dict:
        port = getattr(self.sms, "port", "")
        baud = getattr(self.sms, "baud", 0)
        label = f"GSM modem on {port} @ {baud} baud" if self.sms.mode == "modem" else "Test mode (texts are not sent)"
        return {"mode": self.sms.mode, "port": port, "baud": baud, "label": label}

    # ---------------------------------------------------------------- SIM promo
    def get_sim_promo(self) -> dict:
        promo = self.sim_promo.load()
        if not promo.get("expires"):
            return {"set": False}
        try:
            ends = date.fromisoformat(promo["expires"])
        except ValueError:
            return {"set": False}
        days_left = (ends - datetime.now(PH_TZ).date()).days
        return {"set": True, "name": promo.get("name") or "Promo", "expires": ends.isoformat(),
                "daysLeft": days_left, "registered": promo.get("registered")}

    def set_sim_promo(self, admin: Session, name: str, days: int, registered: str = "") -> dict:
        """Save the promo: it ends ``days`` days after ``registered`` (default today)."""
        name = (name or "").strip()[:60] or "Promo"
        try:
            days = int(days)
            start = date.fromisoformat(registered) if registered else datetime.now(PH_TZ).date()
        except (TypeError, ValueError):
            raise BadRequest("Check the promo days and start date.")
        if not 1 <= days <= 400:
            raise BadRequest("The promo should last 1 to 400 days.")
        ends = start + timedelta(days=days)
        self.sim_promo.save({"name": name, "registered": start.isoformat(), "days": days,
                             "expires": ends.isoformat(), "setBy": admin.username, "setAt": iso(utcnow())})
        self.audit("sim_promo_set", admin.actor, None, {"name": name, "expires": ends.isoformat()})
        return self.get_sim_promo()

    def clear_sim_promo(self, admin: Session) -> dict:
        self.sim_promo.save({})
        self.audit("sim_promo_cleared", admin.actor, None)
        return {"set": False}

    def promo_reminder(self) -> bool:
        """Text the admins once when the promo has 1 day left (and once when it has ended)."""
        promo = self.get_sim_promo()
        if not promo["set"] or promo["daysLeft"] > 1 or self.sms.mode != "modem":
            return False
        days = promo["daysLeft"]
        stage = "ended" if days < 0 else "ending"
        saved = self.sim_promo.load()
        if saved.get("remindedFor") == f"{promo['expires']}:{stage}":
            return False
        when = "has ended" if days < 0 else "ends today" if days == 0 else "ends tomorrow"
        text = (f"AureoClean: the SIM promo ({promo['name']}) {when}. Register again so alerts keep going out, "
                "then update it in System > SMS.")
        sent = 0
        for admin in self.sms_recipients(ROLE_ADMIN):
            try:
                self.sms.send(admin["phone"], text[:160])
                sent += 1
            except SmsError:
                pass
        saved["remindedFor"] = f"{promo['expires']}:{stage}"
        self.sim_promo.save(saved)
        return sent > 0

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

    def run_job(self, key: str, fn):
        """Run a background job and remember how it went (shown in System health)."""
        try:
            result = fn()
            self.job_status[key] = {"at": iso(utcnow()), "error": None}
            return result
        except Exception as exc:
            self.job_status[key] = {"at": iso(utcnow()), "error": f"{type(exc).__name__}: {exc}"}
            raise

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
                notify = "emergency" if category == "emergency" else ("janitors" if urgency == "high" else "none")
                if notify != "none":
                    label = "EMERGENCY" if category == "emergency" else "URGENT"
                    text = alert_text(label, report["smsRef"], report["title"], report["location"], report["details"])
                    self.alert_people(report["id"], notify, text)
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
        try:
            self.report_ai.queue_new(report_id)   # an AI tip for the janitors, in the background (advice only)
        except Exception:  # noqa: BLE001 - never let the AI get in the way of a report
            pass
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

        report = self.store.transact(self.C_REPORTS, report_id, update)
        if status == "resolved":
            try:
                self.smart.report_done(report)   # a cleaning done → count uses from zero again
            except Exception:  # noqa: BLE001 - never block marking a job done
                pass
        return report

    # ------------------------------------------------ the local data (admins only)
    DATA_COLLECTIONS = {   # what the Data screen may show → local collection suffix
        "reports": "Reports", "deletedReports": "DeletedReports", "users": "Users", "janitorRequests": "RegistrationRequests",
        "adminInvites": "AdminInvites", "auditLog": "AuditLogs", "localCredentials": "LocalCredentials", "counters": "Counters",
    }
    DATA_MINUTES = 5

    def data_status(self) -> dict:
        prefix = self.settings.collection_prefix
        counts = self.store.collections() if hasattr(self.store, "collections") else {}
        local = getattr(self.store, "local", self.store)
        st = self.store.status() if hasattr(self.store, "status") else {"mode": "local", "online": False, "pending": 0}
        return {**st, "mode": "Firebase + local copy" if self.settings.mode == "firebase" else "Local only",
                "encrypted": bool(getattr(local, "encrypted", False)), "file": getattr(local, "path", ""),
                "collections": [{"key": k, "count": counts.get(prefix + s, 0)} for k, s in self.DATA_COLLECTIONS.items()],
                "minutes": self.DATA_MINUTES}

    def unlock_data(self, admin: Session, password: str) -> dict:
        """Re-enter the admin password to look at the local data for a few minutes (logged)."""
        key = f"data:{admin.uid}"
        if self.login_failures.locked_for(key):
            raise TooManyAttempts("Too many wrong passwords. Try again in 15 minutes.")
        try:
            uid = self.identity.verify_password(admin.email, password or "")
        except AccountDisabled:
            uid = None
        if uid != admin.uid:
            self.login_failures.record_failure(key)
            self.audit("local_data_refused", admin.actor, None, {})
            raise Forbidden("Wrong password.")
        self.login_failures.reset(key)
        token = secrets.token_urlsafe(24)
        self._data_tokens = {t: v for t, v in getattr(self, "_data_tokens", {}).items() if v[1] > time.monotonic()}
        self._data_tokens[token] = (admin.uid, time.monotonic() + self.DATA_MINUTES * 60)
        self.audit("local_data_opened", admin.actor, None, {})
        return {"token": token, "minutes": self.DATA_MINUTES}

    def browse_data(self, admin: Session, token: str, collection: str, limit: int = 200) -> dict:
        hit = getattr(self, "_data_tokens", {}).get(token or "")
        if not hit or hit[0] != admin.uid or hit[1] < time.monotonic():
            raise Forbidden("Enter your password again to see the local data.")
        suffix = self.DATA_COLLECTIONS.get(collection)
        if suffix is None:
            raise BadRequest("Unknown collection.")
        rows = self.store.query(self.settings.collection_prefix + suffix)

        def redact(v, k=""):
            if isinstance(v, dict):
                return {kk: redact(vv, kk) for kk, vv in v.items()}
            if isinstance(v, list):
                return [redact(x) for x in v]
            if re.search(r"hash|token|secret|password|code$", k, re.I) and v:
                return "•••• (hidden)"
            return v
        rows.sort(key=lambda r: ((r[1].get("_meta") or {}).get("updatedAt") or r[1].get("createdAt") or ""), reverse=True)
        self.audit("local_data_viewed", admin.actor, None, {"collection": collection})
        return {"collection": collection, "total": len(rows),
                "rows": [{"id": i, "data": redact(d)} for i, d in rows[:max(1, min(limit, 500))]]}

    def sync_now(self, admin: Session) -> dict:
        if not hasattr(self.store, "sync_once"):
            raise BadRequest("Local-only mode: there is no Firebase to sync with.")
        st = self.store.sync_once(force_pull=True)
        return {**st, "message": "In sync with Firebase." if st["online"] and not st["pending"] else
                ("Offline — changes will sync when the Pi is online." if not st["online"] else f"{st['pending']} change(s) still sending…")}

    # ------------------------------------------------ the deleted bin
    BIN_DAYS = 30

    def delete_report(self, admin: Session, report_id: str, reason: str = "") -> dict:
        """Move a report to the deleted bin (restorable for 30 days, then removed everywhere)."""
        report = self.store.get(self.C_REPORTS, report_id)
        if report is None:
            raise NotFound("Report not found (maybe it was already deleted).")
        reason = " ".join((reason or "").split())[:200]
        if len(reason) < 3:
            raise BadRequest("Say why it's being deleted (e.g. duplicate, test, prank).")
        now = utcnow()
        doc = {**report, "deletedAt": iso(now), "deletedBy": {"uid": admin.uid, "username": admin.username},
               "deleteReason": reason, "purgeAt": iso(now + timedelta(days=self.BIN_DAYS))}
        self.store.set(self.C_DELETED_REPORTS, report_id, doc)
        self.store.delete(self.C_REPORTS, report_id)
        self.audit("report_deleted", admin.actor, report_id, {"title": report.get("title", "")[:80], "ref": report.get("smsRef"),
                                                              "reason": reason})
        return {"message": f"Report #{report.get('smsRef')} moved to the deleted bin (restorable for {self.BIN_DAYS} days)."}

    def deleted_reports(self) -> list[dict]:
        rows = [dict(r, id=i) for i, r in self.store.query(self.C_DELETED_REPORTS)]
        now = utcnow()
        for r in rows:
            try:
                r["daysLeft"] = max(0, (parse_iso(r["purgeAt"]) - now).days)
            except (KeyError, ValueError):
                r["daysLeft"] = None
        return sorted(rows, key=lambda r: r.get("deletedAt", ""), reverse=True)

    def restore_report(self, admin: Session, report_id: str) -> dict:
        doc = self.store.get(self.C_DELETED_REPORTS, report_id)
        if doc is None:
            raise NotFound("That report isn't in the deleted bin any more.")
        for k in ("deletedAt", "deletedBy", "deleteReason", "purgeAt", "daysLeft"):
            doc.pop(k, None)
        doc.setdefault("notes", []).append({"at": iso(utcnow()), "by": admin.username, "text": "Restored from the deleted bin"})
        self.store.set(self.C_REPORTS, report_id, doc)
        self.store.delete(self.C_DELETED_REPORTS, report_id)
        self.audit("report_restored", admin.actor, report_id, {"ref": doc.get("smsRef")})
        return {"message": f"Report #{doc.get('smsRef')} restored."}

    def purge_report(self, admin: Optional[Session], report_id: str) -> dict:
        doc = self.store.get(self.C_DELETED_REPORTS, report_id)
        if doc is None:
            raise NotFound("That report isn't in the deleted bin.")
        self.store.delete(self.C_DELETED_REPORTS, report_id)
        self.audit("report_purged", admin.actor if admin else {"type": "system", "username": "bin"}, report_id,
                   {"ref": doc.get("smsRef"), "title": doc.get("title", "")[:80]})
        return {"message": f"Report #{doc.get('smsRef')} deleted for good."}

    def purge_old_deleted(self) -> int:
        now = iso(utcnow())
        old = [i for i, r in self.store.query(self.C_DELETED_REPORTS) if r.get("purgeAt", "9999") <= now]
        for i in old:
            self.purge_report(None, i)
        return len(old)

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
    def admin_overview(self, cached: bool = False) -> dict:
        """Counts for the tab names. The screen's 30 s timer passes cached=True, so an
        answer from the last 15 s is reused instead of re-reading every collection."""
        hit = getattr(self, "_overview_cache", None)
        if cached and hit and time.monotonic() - hit[0] < 15:
            return hit[1]
        result = self._admin_overview()
        self._overview_cache = (time.monotonic(), result)
        return result

    def _admin_overview(self) -> dict:
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
            "activeAlerts": self.active_alert_count(),
        }

    # ============================================================ janitor jobs
    def _job(self, report_id: str, r: dict, me: str) -> dict:
        holder = r.get("assignedTo") or {}
        done_by = r.get("resolvedBy") or {}
        return {
            "id": report_id, "ref": r.get("smsRef"), "title": r.get("title", ""), "location": r.get("location", ""),
            "category": r.get("category"), "priority": r.get("priority"), "status": r.get("status"), "source": r.get("source"),
            "details": (r.get("details") or "")[:300], "createdAt": r.get("reportedAt") or r.get("createdAt"),
            "takenBy": holder.get("name") or holder.get("username"), "mine": holder.get("uid") == me,
            "doneBy": done_by.get("name") or done_by.get("username"), "resolvedAt": r.get("resolvedAt"),
            "ai": {k: (r.get("ai") or {}).get(k) for k in ("summary", "steps", "bring", "minutes", "safety", "helper")}
            if r.get("ai") else None,
        }

    def janitor_jobs(self, session: Session) -> dict:
        """The janitor's simple report view: open jobs, my jobs, and what was fixed today."""
        start_of_day = datetime.now(PH_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        today = iso(start_of_day.astimezone(timezone.utc))
        order = {"critical": 0, "high": 1, "normal": 2, "low": 3}
        open_, mine, taken, done = [], [], [], []
        for report_id, r in self.store.query(self.C_REPORTS):
            job = self._job(report_id, r, session.uid)
            if r.get("status") == "resolved":
                if (r.get("resolvedAt") or "") >= today:
                    done.append(job)
            elif job["mine"]:
                mine.append(job)
            elif r.get("assignedTo"):
                taken.append(job)
            else:
                open_.append(job)
        def urgent_first(rows):
            rows.sort(key=lambda j: j["createdAt"] or "", reverse=True)
            rows.sort(key=lambda j: order.get(j["priority"], 9))
            return rows
        done.sort(key=lambda j: j["resolvedAt"] or "", reverse=True)
        return {"open": urgent_first(open_), "mine": urgent_first(mine), "taken": urgent_first(taken), "done": done}

    def _janitor_name(self, session: Session) -> str:
        return (session.full_name or session.username).split()[0]

    def janitor_take(self, session: Session, report_id: str) -> dict:
        """'I'll take it' in the app: the same as texting OK <number>."""
        name = self._janitor_name(session)

        def take(current):
            if current is None:
                raise NotFound("That report no longer exists.")
            if current.get("status") == "resolved":
                raise Conflict("This one is already done. Thank you!")
            holder = current.get("assignedTo") or {}
            if holder and holder.get("uid") != session.uid:
                raise Conflict(f"Already taken by {holder.get('name') or holder.get('username')}.")
            current.update(status="in_progress", acceptedAt=current.get("acceptedAt") or iso(utcnow()),
                           assignedTo={"uid": session.uid, "username": session.username, "name": name})
            return current

        self.store.transact(self.C_REPORTS, report_id, take)
        report = self._add_report_note(report_id, session.actor, f"Taken by {name} in the app", "in_progress")
        self.audit("report_updated", session.actor, report_id, {"status": "in_progress", "via": "app"})
        return self._job(report_id, report, session.uid)

    def janitor_done(self, session: Session, report_id: str, note: str = "") -> dict:
        """'Done' in the app: the same as texting DONE <number>."""
        name = self._janitor_name(session)

        def finish(current):
            if current is None:
                raise NotFound("That report no longer exists.")
            if current.get("status") == "resolved":
                raise Conflict("This one is already marked done.")
            holder = current.get("assignedTo") or {}
            if holder and holder.get("uid") != session.uid:
                raise Conflict(f"{holder.get('name') or holder.get('username')} is working on this one.")
            me = {"uid": session.uid, "username": session.username, "name": name}
            current.setdefault("assignedTo", me)
            current.setdefault("acceptedAt", iso(utcnow()))
            current["resolvedBy"] = me
            return current

        self.store.transact(self.C_REPORTS, report_id, finish)
        text = f"Marked done by {name} in the app" + (f": {note.strip()[:300]}" if (note or "").strip() else "")
        report = self._add_report_note(report_id, session.actor, text, "resolved")
        self.audit("report_updated", session.actor, report_id, {"status": "resolved", "via": "app"})
        return self._job(report_id, report, session.uid)

    def report_problem(self, session: Session, location: str, issues: list[str], details: str = "",
                       location_code: str = "") -> dict:
        """'Report a problem' (janitors and admins): pick the restroom, tap what's wrong.
        Same fixed rules as the public page: emergencies text everyone."""
        from .report_schema import category_for, issue_map, priority_for, title_for

        known = issue_map()
        issues = list(dict.fromkeys(c for c in (issues or []) if c in known))
        if not issues:
            raise BadRequest("Tap at least one problem.")
        location = " ".join((location or "").split())[:60]
        if not location:
            raise BadRequest("Pick the restroom.")
        category = category_for(issues)
        urgency = "high" if "emergency" in issues else "medium"
        report = self._save_report(
            category=category, title=title_for(issues), location=location, details=(details or "").strip()[:500],
            priority=priority_for(issues, urgency),
            created_by={"uid": session.uid, "username": session.username},
            extra={"source": session.role, "channel": "app", "issues": issues, "urgency": urgency,
                   "locationCode": (location_code or "")[:12] or None})
        report_id = report.pop("id")
        self.audit("report_created", session.actor, report_id, {"category": category, "title": report["title"]})
        notified = 0
        if category == "emergency":
            notified = self.alert_people(report_id, "emergency",
                                         alert_text("EMERGENCY", report["smsRef"], report["title"], location, report["details"]),
                                         exclude_uid=session.uid)
        return {"id": report_id, "ref": report["smsRef"], "title": report["title"], "notified": notified,
                "message": f"Report #{report['smsRef']} sent." + (f" Texting {notified} people." if notified else "")}

    def report_options(self) -> dict:
        """Restrooms and problem types for the report forms (same lists as the public page)."""
        from .report_schema import schema
        return {"locations": public_locations(), "issues": schema()["issues"], "categories": schema()["categories"]}

    def janitor_overview(self, session: Session) -> dict:
        return {
            "profile": self.me(session),
            # Filled in once the Realtime Database sensor feed is connected.
            "notifications": [],
            "message": "Restroom alerts will appear here once the sensor feed is connected.",
        }
