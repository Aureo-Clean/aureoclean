"""SQLite implementation: the Pi's local copy of the data (and the whole store in local mode).

Documents can be encrypted at rest (AES-256-GCM, see backend/localcrypt.py). The same file also
keeps what the Firebase sync needs: an outbox of changes still to send, sync cursors, and
account changes made while offline (passwords in it are encrypted too and removed once sent)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Optional

from ..localcrypt import Cipher
from ..security import generate_id, hash_secret, verify_secret
from .base import AccountDisabled, AccountExists, AlreadyExists


class LocalBackend:
    """Implements both DocStore and Identity on one SQLite file.

    Local mode can't send email, so email verification is switched off
    (tests switch it on and use ``mark_email_verified``)."""

    supports_email_verification = False

    def __init__(self, path: str | Path, key: Optional[bytes] = None):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(Path(path).parent, 0o700)
            except OSError:
                pass
        self._cipher = Cipher(key)
        self.encrypted = key is not None
        self.path = str(path)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        if str(path) != ":memory:":
            for f in (str(path), f"{path}-wal", f"{path}-shm"):
                try:
                    os.chmod(f, 0o600)
                except OSError:
                    pass
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
            CREATE TABLE IF NOT EXISTS outbox (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                collection TEXT NOT NULL, id TEXT NOT NULL, at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auth_ops (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                uid TEXT NOT NULL, op TEXT NOT NULL, payload TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(accounts)")}
        if "origin" not in cols:
            self._db.execute("ALTER TABLE accounts ADD COLUMN origin TEXT NOT NULL DEFAULT ''")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------ DocStore
    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM docs WHERE collection=? AND id=?", (collection, doc_id)
            ).fetchone()
        return self._load(collection, doc_id, row[0]) if row else None

    def _dump(self, collection: str, doc_id: str, data: dict) -> str:
        return self._cipher.seal(json.dumps(data), f"{collection}/{doc_id}")

    def _load(self, collection: str, doc_id: str, raw: str) -> dict:
        return json.loads(self._cipher.open(raw, f"{collection}/{doc_id}"))

    def create(self, collection: str, doc_id: str, data: dict) -> None:
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO docs (collection, id, data) VALUES (?, ?, ?)",
                    (collection, doc_id, self._dump(collection, doc_id, data)),
                )
            except sqlite3.IntegrityError as exc:
                raise AlreadyExists(f"{collection}/{doc_id}") from exc

    def set(self, collection: str, doc_id: str, data: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO docs (collection, id, data) VALUES (?, ?, ?)",
                (collection, doc_id, self._dump(collection, doc_id, data)),
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
            data = self._load(collection, doc_id, raw)
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
    def create_account(self, email: str, password: str, display_name: str, uid: Optional[str] = None,
                       origin: str = "") -> str:
        uid = uid or generate_id()
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO accounts (uid, email, display_name, password_hash, origin) VALUES (?, ?, ?, ?, ?)",
                    (uid, email, display_name, hash_secret(password), origin),
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

    # ------------------------------------------------------------ for the Firebase sync
    def remember_account(self, uid: str, email: str, display_name: str, password: str) -> None:
        """Keep a local (hashed) copy of a Firebase account after a successful online sign-in,
        so the same person can sign in while the Wi-Fi is down."""
        with self._lock:
            row = self._db.execute("SELECT uid, origin FROM accounts WHERE email=?", (email,)).fetchone()
            if row and row[0] != uid:
                self._db.execute("DELETE FROM accounts WHERE email=?", (email,))
                row = None
            if row:
                self._db.execute("UPDATE accounts SET password_hash=?, display_name=? WHERE uid=?",
                                 (hash_secret(password), display_name, uid))
            else:
                self._db.execute("INSERT INTO accounts (uid, email, display_name, password_hash, origin) VALUES (?, ?, ?, ?, ?)",
                                 (uid, email, display_name, hash_secret(password), "firebase"))

    def account(self, uid: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT uid, email, display_name, password_hash, disabled, origin FROM accounts WHERE uid=?",
                                   (uid,)).fetchone()
        return dict(zip(("uid", "email", "displayName", "passwordHash", "disabled", "origin"), row)) if row else None

    def accounts(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT uid, email, display_name, password_hash, disabled, origin FROM accounts").fetchall()
        return [dict(zip(("uid", "email", "displayName", "passwordHash", "disabled", "origin"), r)) for r in rows]

    def put_account_hash(self, uid: str, email: str, display_name: str, password_hash: str, disabled: bool, origin: str) -> None:
        """Restore an account from its synced (hashed) credential record."""
        with self._lock:
            self._db.execute("DELETE FROM accounts WHERE email=? AND uid<>?", (email, uid))
            self._db.execute("INSERT OR REPLACE INTO accounts (uid, email, display_name, password_hash, disabled, origin) "
                             "VALUES (?, ?, ?, ?, ?, ?)", (uid, email, display_name, password_hash, int(disabled), origin))

    # outbox: documents changed here that Firebase hasn't got yet
    def enqueue(self, collection: str, doc_id: str, at: str) -> None:
        with self._lock:
            self._db.execute("INSERT INTO outbox (collection, id, at) VALUES (?, ?, ?)", (collection, doc_id, at))

    def outbox(self, limit: int = 200) -> list[tuple[int, str, str]]:
        with self._lock:
            return self._db.execute("SELECT seq, collection, id FROM outbox ORDER BY seq LIMIT ?", (limit,)).fetchall()

    def outbox_done(self, seqs: list[int]) -> None:
        with self._lock:
            self._db.executemany("DELETE FROM outbox WHERE seq=?", [(s,) for s in seqs])

    def pending(self) -> int:
        with self._lock:
            return (self._db.execute("SELECT COUNT(DISTINCT collection || '/' || id) FROM outbox").fetchone()[0]
                    + self._db.execute("SELECT COUNT(*) FROM auth_ops").fetchone()[0])

    def is_pending(self, collection: str, doc_id: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM outbox WHERE collection=? AND id=? LIMIT 1", (collection, doc_id)).fetchone() is not None

    # account changes made offline (passwords encrypted; removed once Firebase has them)
    def add_auth_op(self, uid: str, op: str, payload: dict) -> None:
        with self._lock:
            self._db.execute("INSERT INTO auth_ops (uid, op, payload) VALUES (?, ?, ?)",
                             (uid, op, self._cipher.seal(json.dumps(payload), f"auth/{uid}")))

    def auth_ops(self) -> list[tuple[int, str, str, dict]]:
        with self._lock:
            rows = self._db.execute("SELECT seq, uid, op, payload FROM auth_ops ORDER BY seq").fetchall()
        return [(s, u, o, json.loads(self._cipher.open(p, f"auth/{u}"))) for s, u, o, p in rows]

    def auth_op_done(self, seq: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM auth_ops WHERE seq=?", (seq,))

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def collections(self) -> dict[str, int]:
        with self._lock:
            return dict(self._db.execute("SELECT collection, COUNT(*) FROM docs GROUP BY collection").fetchall())
