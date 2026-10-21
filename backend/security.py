"""Hashing, code generation, validation and in-memory throttling helpers."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Codes avoid look-alike characters (0/O, 1/I/L) so they can be read aloud
# or copied from a phone screen without mistakes.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,24}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
PHONE_RE = re.compile(r"^\+?[0-9 ]{7,16}$")


class ValidationError(ValueError):
    """Raised when user input is invalid. The message is safe to show."""


# ---------------------------------------------------------------- time
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def iso_in(hours: float) -> str:
    return iso(utcnow() + timedelta(hours=hours))


# ---------------------------------------------------------------- input
def normalize_username(username: str) -> str:
    return (username or "").strip().lower()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_username(username: str) -> str:
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValidationError(
            "Username must be 3-24 characters: letters, numbers, dots or underscores."
        )
    return username


def validate_email(email: str) -> str:
    email = normalize_email(email)
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ValidationError("Enter a valid email address.")
    return email


def validate_full_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not 2 <= len(name) <= 80:
        raise ValidationError("Full name must be 2-80 characters.")
    return name


def validate_phone(phone: str) -> str:
    phone = (phone or "").strip()
    if phone and not PHONE_RE.fullmatch(phone):
        raise ValidationError("Phone number should contain only digits (optionally starting with +).")
    return phone


def validate_password(password: str) -> str:
    password = password or ""
    if len(password) < 8:
        raise ValidationError("Password must be at least 8 characters.")
    if len(password) > 128:
        raise ValidationError("Password must be at most 128 characters.")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"[0-9]", password):
        raise ValidationError("Password must contain at least one letter and one number.")
    return password


# ---------------------------------------------------------------- secrets
def hash_secret(secret: str) -> str:
    """Slow, salted hash (scrypt) for passwords and short activation codes."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_secret(secret: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, TypeError):
        return False
    digest = hashlib.scrypt(secret.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(digest, expected)


def lookup_hash(secret: str) -> str:
    """Fast, unsalted hash used as a lookup key for long random codes (invites).

    Only safe because invite codes carry ~70 bits of randomness."""
    return hashlib.sha256(normalize_code(secret).encode()).hexdigest()


def generate_code(groups: int = 2, size: int = 4) -> str:
    """e.g. ``K7QM-3XRB`` (2x4 chars, ~40 bits)."""
    return "-".join(
        "".join(secrets.choice(CODE_ALPHABET) for _ in range(size)) for _ in range(groups)
    )


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def generate_id() -> str:
    return secrets.token_hex(10)


# ---------------------------------------------------------------- throttling
class FailureTracker:
    """Counts failures per key in a sliding window and locks the key out."""

    def __init__(self, max_failures: int, window_minutes: int):
        self.max_failures = max_failures
        self.window = window_minutes * 60
        self._failures: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        items = [t for t in self._failures.get(key, []) if now - t < self.window]
        self._failures[key] = items
        return items

    def locked_for(self, key: str) -> int:
        """Seconds remaining in the lockout, or 0."""
        with self._lock:
            now = time.monotonic()
            items = self._prune(key, now)
            if len(items) >= self.max_failures:
                return max(1, int(self.window - (now - items[-self.max_failures])))
            return 0

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
