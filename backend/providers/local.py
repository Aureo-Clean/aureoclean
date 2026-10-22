"""SQLite implementation for development, demos and offline use."""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from ..security import generate_id, hash_secret, verify_secret
from .base import AccountDisabled, AccountExists, AlreadyExists


class LocalBackend:
    """Implements both DocStore and Identity on one SQLite file.

    Local mode can't send email, so email verification is switched off
    (tests switch it on and use ``mark_email_verified``)."""

    supports_email_verification = False

    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS docs (
                collection TEXT NOT NULL,
                id TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (collection, id)
            );
            CREATE TABLE IF NOT EXISTS accounts (
                uid TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                disabled INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS verified_emails (uid TEXT PRIMARY KEY);
            """
        )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ DocStore
    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM docs WHERE collection=? AND id=?", (collection, doc_id)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def create(self, collection: str, doc_id: str, data: dict) -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO docs (collection, id, data) VALUES (?, ?, ?)",
                    (collection, doc_id, json.dumps(data)),
                )
            except sqlite3.IntegrityError as exc:
                raise AlreadyExists(f"{collection}/{doc_id}") from exc

    def set(self, collection: str, doc_id: str, data: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO docs (collection, id, data) VALUES (?, ?, ?)",
                (collection, doc_id, json.dumps(data)),
            )

    def delete(self, collection: str, doc_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM docs WHERE collection=? AND id=?", (collection, doc_id))

    def query(self, collection: str, **equals) -> list[tuple[str, dict]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, data FROM docs WHERE collection=?", (collection,)
            ).fetchall()
        out = []
        for doc_id, raw in rows:
            data = json.loads(raw)
            if all(data.get(k) == v for k, v in equals.items()):
                out.append((doc_id, data))
        return out

    def latest(self, collection: str, field: str, limit: int) -> list[tuple[str, dict]]:
        docs = self.query(collection)
        docs.sort(key=lambda item: item[1].get(field) or "", reverse=True)
        return docs[:limit]

    def transact(self, collection: str, doc_id: str, fn: Callable[[Optional[dict]], Optional[dict]]):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                current = self.get(collection, doc_id)
                result = fn(current)
                if result is not None:
                    self.set(collection, doc_id, result)
                self._db.execute("COMMIT")
                return result
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------ Identity
    def create_account(self, email: str, password: str, display_name: str) -> str:
        uid = generate_id()
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO accounts (uid, email, display_name, password_hash) VALUES (?, ?, ?, ?)",
                    (uid, email, display_name, hash_secret(password)),
                )
            except sqlite3.IntegrityError as exc:
                raise AccountExists(email) from exc
        return uid

    def verify_password(self, email: str, password: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute(
                "SELECT uid, password_hash, disabled FROM accounts WHERE email=?", (email,)
            ).fetchone()
        if not row:
            # Spend comparable time so response time doesn't reveal which emails exist.
            verify_secret(password, hash_secret("timing-equaliser"))
            return None
        uid, pw_hash, disabled = row
        if not verify_secret(password, pw_hash):
            return None
        if disabled:
            raise AccountDisabled(email)
        return uid

    def find_uid(self, email: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT uid FROM accounts WHERE email=?", (email,)).fetchone()
        return row[0] if row else None

    def set_password(self, uid: str, password: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE accounts SET password_hash=? WHERE uid=?", (hash_secret(password), uid)
            )

    def set_disabled(self, uid: str, disabled: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE accounts SET disabled=? WHERE uid=?", (int(disabled), uid))

    def delete_account(self, uid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM accounts WHERE uid=?", (uid,))

    def send_verification_email(self, email: str, password: str) -> None:
        self.sent_verifications = getattr(self, "sent_verifications", []) + [email]

    def is_email_verified(self, uid: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM verified_emails WHERE uid=?", (uid,)).fetchone() is not None

    def mark_email_verified(self, uid: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO verified_emails (uid) VALUES (?)", (uid,))
