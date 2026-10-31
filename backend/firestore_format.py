"""How AureoClean's documents look in Firebase.

Inside the app, times are ISO text ("2026-10-31T02:15:00+00:00"). In Firestore they are stored as
real timestamps, so the console shows them as dates and they sort and filter properly. Every
field whose name ends in "At" or "Until" (createdAt, updatedAt, expiresAt, resolvedAt …) is
converted on the way in, and back to ISO text on the way out. Older documents that still hold
text dates keep working.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)?$")


def _is_time_key(key) -> bool:
    return isinstance(key, str) and (key.endswith("At") or key.endswith("Until"))


def to_firestore(value, key=None):
    if isinstance(value, dict):
        return {k: to_firestore(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [to_firestore(v) for v in value]
    if isinstance(value, str) and _is_time_key(key) and _ISO.match(value):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return value
    return value


def from_firestore(value):
    if isinstance(value, dict):
        return {k: from_firestore(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_firestore(v) for v in value]
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return value
