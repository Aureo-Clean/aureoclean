"""Firebase always, with a secure local copy that keeps working offline.

* Every read and write goes to the Pi's local copy first (encrypted SQLite) — fast, and it keeps
  working when the Wi-Fi drops.
* A background sync sends each change to Firebase (an outbox survives restarts) and pulls changes
  made in Firebase back, so both hold the same data. After a Wi-Fi outage it catches up by itself.
* Accounts: when online, sign-in and new accounts go through Firebase Authentication, and a
  hashed copy is kept locally so the same people can sign in offline. Accounts made while
  offline are created locally, marked "createdLocally", their hashed credentials kept in the
  separate "localCredentials" collection, and created in Firebase Authentication (same UID) as
  soon as the Pi is back online. Passwords are never stored in plain text.

In Firebase the collections get plain names (users, reports, janitorRequests …); data under the
old names (aureoUsers, aureoReports …) is copied over on the first sync and left untouched.
Every synced document carries a small "_meta" map: when it last changed, on which device, and
whether that happened offline.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Callable, Optional

from ..security import generate_id, iso, utcnow
from .base import AccountDisabled, AccountExists, ProviderError

log = logging.getLogger("aureoclean.sync")

# local collection suffix → readable name in Firebase
REMOTE_NAMES = {
    "Users": "users", "Usernames": "usernames", "RegistrationRequests": "janitorRequests",
    "AdminInvites": "adminInvites", "AuditLogs": "auditLog", "Reports": "reports", "Counters": "counters",
    "Insights": "insights", "LocalCredentials": "localCredentials", "DeletedReports": "deletedReports",
}
LOCAL_ONLY = {"Sessions"}                      # sign-in sessions belong to this Pi
REMOTE_ONLY = {"PublicReports", "WebSignups"}   # written by the public web pages; read straight from Firebase
SYNC_SECONDS = 10
PULL_SECONDS = 60


def _now() -> str:
    return iso(utcnow())


class SyncedBackend:
    supports_email_verification = True

    def __init__(self, local, remote_factory: Callable[[], object], prefix: str = "aureo",
                 probe: Optional[Callable[[], bool]] = None, device: str = ""):
        self.local = local
        self._factory = remote_factory
        self._remote = None
        self.prefix = prefix
        self.device = device or socket.gethostname()
        self._probe = probe or _internet_up
        self.online = False
        self.remote_error: Optional[str] = None
        self.last_push: Optional[str] = local.get_meta("lastPush") or None
        self.last_pull: Optional[str] = local.get_meta("lastPull") or None
        self.last_error: Optional[str] = None
        self._created_offline: set[str] = set()
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._pulled_at = 0.0

    # ------------------------------------------------------------------ plumbing
    def _suffix(self, collection: str) -> str:
        return collection[len(self.prefix):] if collection.startswith(self.prefix) else collection

    def kind(self, collection: str) -> str:
        s = self._suffix(collection)
        return "remote" if s in REMOTE_ONLY else "local" if s in LOCAL_ONLY else "synced"

    def remote_name(self, collection: str) -> str:
        return REMOTE_NAMES.get(self._suffix(collection), collection)

    def remote(self):
        if self._remote is None:
            try:
                self._remote = self._factory()
                self.remote_error = None
            except Exception as exc:  # noqa: BLE001 - e.g. no service-account key yet
                self.remote_error = str(exc)[:300]
                return None
        return self._remote

    def check_online(self) -> bool:
        up = False
        try:
            up = bool(self._probe()) and self.remote() is not None
        except Exception:  # noqa: BLE001
            up = False
        if up != self.online:
            log.info("Firebase is %s", "reachable — syncing" if up else "NOT reachable — working offline")
        self.online = up
        return up

    def _need_remote(self):
        r = self.remote() if self.online else None
        if r is None:
            raise ProviderError("No internet connection to Firebase right now (working offline).")
        return r

    def _stamp(self, data: dict, collection: str, doc_id: str) -> dict:
        meta = dict(data.get("_meta") or {})
        meta.update(updatedAt=_now(), device=self.device)
        if not self.online:
            meta["offline"] = True
        else:
            meta.pop("offline", None)
        data["_meta"] = meta
        if self._suffix(collection) == "Users" and doc_id in self._created_offline:
            data["createdLocally"] = True
        return data

    def _changed(self, collection: str, doc_id: str) -> None:
        if self.kind(collection) == "synced":
            self.local.enqueue(collection, doc_id, _now())

    # ------------------------------------------------------------------ DocStore
    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        if self.kind(collection) == "remote":
            return self._need_remote().get(collection, doc_id)
        return self.local.get(collection, doc_id)

    def create(self, collection: str, doc_id: str, data: dict) -> None:
        if self.kind(collection) == "remote":
            return self._need_remote().create(collection, doc_id, data)
        self.local.create(collection, doc_id, self._stamp(dict(data), collection, doc_id))
        self._changed(collection, doc_id)

    def set(self, collection: str, doc_id: str, data: dict) -> None:
        if self.kind(collection) == "remote":
            return self._need_remote().set(collection, doc_id, data)
        self.local.set(collection, doc_id, self._stamp(dict(data), collection, doc_id))
        self._changed(collection, doc_id)

    def delete(self, collection: str, doc_id: str) -> None:
        if self.kind(collection) == "remote":
            return self._need_remote().delete(collection, doc_id)
        self.local.delete(collection, doc_id)
        self._changed(collection, doc_id)

    def query(self, collection: str, **equals) -> list[tuple[str, dict]]:
        if self.kind(collection) == "remote":
            return self._need_remote().query(collection, **equals)
        return self.local.query(collection, **equals)

    def latest(self, collection: str, field: str, limit: int) -> list[tuple[str, dict]]:
        if self.kind(collection) == "remote":
            return self._need_remote().latest(collection, field, limit)
        return self.local.latest(collection, field, limit)

    def transact(self, collection: str, doc_id: str, fn):
        if self.kind(collection) == "remote":
            return self._need_remote().transact(collection, doc_id, fn)
        changed = []

        def wrapped(current):
            result = fn(current)
            if result is not None:
                self._stamp(result, collection, doc_id)
                changed.append(True)
            return result
        result = self.local.transact(collection, doc_id, wrapped)
        if changed:
            self._changed(collection, doc_id)
        return result

    # ------------------------------------------------------------------ Identity
    def create_account(self, email: str, password: str, display_name: str) -> str:
        if self.online:
            try:
                uid = self.remote().create_account(email, password, display_name)
                self.local.remember_account(uid, email, display_name, password)
                return uid
            except AccountExists:
                raise
            except ProviderError:
                self.online = False            # fall through to offline
        if self.local.find_uid(email):
            raise AccountExists(email)
        uid = generate_id()
        self.local.create_account(email, password, display_name, uid=uid, origin="local")
        self.local.add_auth_op(uid, "create", {"email": email, "password": password, "displayName": display_name})
        self._created_offline.add(uid)
        acc = self.local.account(uid)
        self.set(f"{self.prefix}LocalCredentials", uid, {
            "uid": uid, "email": email, "displayName": display_name, "passwordHash": acc["passwordHash"],
            "createdLocallyAt": _now(), "createdOnDevice": self.device, "firebaseAuth": "pending"})
        log.info("Account %s created offline (locally); it goes to Firebase when the Pi is online", email)
        return uid

    def verify_password(self, email: str, password: str) -> Optional[str]:
        local_acc = self.local.find_uid(email)
        acc = self.local.account(local_acc) if local_acc else None
        if acc and acc["origin"] == "local" and self._has_auth_op(acc["uid"], "create"):
            return self.local.verify_password(email, password)       # not in Firebase yet
        if self.online:
            try:
                uid = self.remote().verify_password(email, password)
                if uid:
                    self.local.remember_account(uid, email, acc["displayName"] if acc else "", password)
                    return uid
                if acc and acc["origin"] in ("", "local") and self.local.verify_password(email, password):
                    return self._promote(acc, email, password)      # made on this Pi before Firebase was switched on
                return None
            except AccountDisabled:
                raise
            except ProviderError:
                self.online = False
        return self.local.verify_password(email, password)

    def _promote(self, acc: dict, email: str, password: str) -> Optional[str]:
        """An account that only exists on this Pi (made in local mode): create it in Firebase Authentication
        with the same UID now that we have its password, and mark it as created locally."""
        uid = acc["uid"]
        try:
            self.remote().create_account(email, password, acc["displayName"], uid=uid)
        except AccountExists:
            log.warning("%s already has a different Firebase account; sign in with that password", email)
            return None
        self.local.remember_account(uid, email, acc["displayName"], password)
        self.set(f"{self.prefix}LocalCredentials", uid, {
            "uid": uid, "email": email, "displayName": acc["displayName"], "passwordHash": self.local.account(uid)["passwordHash"],
            "createdLocallyAt": _now(), "createdOnDevice": self.device, "firebaseAuth": "created", "firebaseAuthAt": _now()})
        users = f"{self.prefix}Users"
        if self.local.get(users, uid) is not None:
            self.transact(users, uid, lambda u: {**u, "createdLocally": True})
        log.info("Account %s (made on this Pi) is now in Firebase Authentication too", email)
        return uid

    def _has_auth_op(self, uid: str, op: str) -> bool:
        return any(u == uid and o == op for _, u, o, _p in self.local.auth_ops())

    def find_uid(self, email: str) -> Optional[str]:
        uid = self.local.find_uid(email)
        if uid or not self.online:
            return uid
        try:
            return self.remote().find_uid(email)
        except ProviderError:
            return None

    def set_password(self, uid: str, password: str) -> None:
        if self.local.account(uid):
            self.local.set_password(uid, password)
        self._auth_change(uid, "password", {"password": password}, lambda r: r.set_password(uid, password))

    def set_disabled(self, uid: str, disabled: bool) -> None:
        if self.local.account(uid):
            self.local.set_disabled(uid, disabled)
        self._auth_change(uid, "disable", {"disabled": disabled}, lambda r: r.set_disabled(uid, disabled))

    def delete_account(self, uid: str) -> None:
        self.local.delete_account(uid)
        self._auth_change(uid, "delete", {}, lambda r: r.delete_account(uid))

    def _auth_change(self, uid: str, op: str, payload: dict, apply) -> None:
        if self._has_auth_op(uid, "create"):           # created offline: the create carries the newest state
            self.local.add_auth_op(uid, op, payload)
            return
        if self.online:
            try:
                apply(self.remote())
                return
            except ProviderError:
                self.online = False
            except Exception as exc:  # noqa: BLE001 - e.g. the user doesn't exist in Firebase Auth
                if "not found" in str(exc).lower() or "NotFound" in type(exc).__name__:
                    return
                raise
        self.local.add_auth_op(uid, op, payload)

    def send_verification_email(self, email: str, password: str) -> None:
        self._need_remote().send_verification_email(email, password)

    def is_email_verified(self, uid: str) -> bool:
        acc = self.local.account(uid)
        if acc and acc["origin"] == "local":
            return True                                  # made offline by an admin: no email involved
        if self.online:
            try:
                ok = self.remote().is_email_verified(uid)
                if ok:
                    self.local.mark_email_verified(uid)
                return ok
            except ProviderError:
                self.online = False
            except Exception:  # noqa: BLE001
                pass
        return self.local.is_email_verified(uid)

    # ------------------------------------------------------------------ the sync
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while True:
                try:
                    self.sync_once()
                except Exception:  # noqa: BLE001
                    log.warning("Sync failed; will retry", exc_info=True)
                time.sleep(SYNC_SECONDS)
        self._thread = threading.Thread(target=loop, daemon=True, name="firebase-sync")
        self._thread.start()

    def sync_once(self, force_pull: bool = False) -> dict:
        with self._lock:
            was_online = self.online
            if not self.check_online():
                return self.status()
            try:
                self._push_auth()
                pushed = self._push_docs()
                if force_pull or not was_online or time.monotonic() - self._pulled_at >= PULL_SECONDS:
                    self._pull()
                    self._pulled_at = time.monotonic()
                if pushed:
                    self.last_push = _now()
                    self.local.set_meta("lastPush", self.last_push)
                self.last_error = None
            except ProviderError as exc:
                self.last_error = str(exc)[:300]
                self.online = False
            return self.status()

    def _push_auth(self) -> None:
        r = self.remote()
        for seq, uid, op, p in self.local.auth_ops():
            if op == "create":
                try:
                    r.create_account(p["email"], p["password"], p.get("displayName", ""), uid=uid)
                except AccountExists:
                    if r.find_uid(p["email"]) != uid:
                        log.warning("Couldn't create %s in Firebase: that email already has another account", p["email"])
                cred = self.local.get(f"{self.prefix}LocalCredentials", uid)
                if cred:
                    cred.update(firebaseAuth="created", firebaseAuthAt=_now())
                    self.set(f"{self.prefix}LocalCredentials", uid, cred)
            elif op == "password":
                r.set_password(uid, p["password"])
            elif op == "disable":
                r.set_disabled(uid, p["disabled"])
            elif op == "delete":
                try:
                    r.delete_account(uid)
                except Exception as exc:  # noqa: BLE001
                    if "not found" not in str(exc).lower():
                        raise
            self.local.auth_op_done(seq)
            self._created_offline.discard(uid)

    def _push_docs(self) -> int:
        r = self.remote()
        sent = 0
        while True:
            batch = self.local.outbox()
            if not batch:
                return sent
            seen: dict[tuple, list[int]] = {}
            for seq, coll, doc_id in batch:
                seen.setdefault((coll, doc_id), []).append(seq)
            for (coll, doc_id), seqs in seen.items():
                doc = self.local.get(coll, doc_id)
                name = self.remote_name(coll)
                if doc is None:
                    r.delete(name, doc_id)
                else:
                    r.set(name, doc_id, doc)
                self.local.outbox_done(seqs)
                sent += 1

    def _pull(self) -> None:
        r = self.remote()
        for suffix, name in REMOTE_NAMES.items():
            local_coll = f"{self.prefix}{suffix}"
            cursor = self.local.get_meta(f"pull:{suffix}")
            if cursor:
                docs = r.changed_since(name, cursor)
            else:
                docs = r.query(name)
                if not docs and local_coll != name:
                    legacy = r.query(local_coll)            # old collection names (aureoUsers …): copy them over
                    for doc_id, doc in legacy:
                        if self.local.get(local_coll, doc_id) is None:
                            self.local.set(local_coll, doc_id, doc)
                            self.local.enqueue(local_coll, doc_id, _now())
                    if legacy:
                        log.info("Copied %d document(s) from %s to %s", len(legacy), local_coll, name)
            newest = cursor
            for doc_id, doc in docs:
                meta = doc.get("_meta") or {}
                stamp = meta.get("updatedAt") or ""
                newest = max(newest or "", stamp)
                if self.local.is_pending(local_coll, doc_id):
                    continue                                # our newer change goes up first
                mine = self.local.get(local_coll, doc_id)
                if mine is not None and (mine.get("_meta") or {}).get("updatedAt", "") > stamp:
                    continue
                self.local.set(local_coll, doc_id, doc)
                if suffix == "LocalCredentials" and doc.get("passwordHash"):
                    self.local.put_account_hash(doc["uid"], doc["email"], doc.get("displayName", ""), doc["passwordHash"],
                                                bool(doc.get("disabled")), "local")
            self.local.set_meta(f"pull:{suffix}", newest or _now())
        self.last_pull = _now()
        self.local.set_meta("lastPull", self.last_pull)

    def status(self) -> dict:
        return {"mode": "firebase+local", "online": self.online, "pending": self.local.pending(),
                "lastPush": self.last_push, "lastPull": self.last_pull, "error": self.last_error or self.remote_error,
                "encrypted": getattr(self.local, "encrypted", False), "running": bool(self._thread and self._thread.is_alive())}

    # passthroughs used by the admin Data screen
    def collections(self) -> dict[str, int]:
        return self.local.collections()

    def close(self) -> None:
        self.local.close()


def _internet_up(host: str = "firestore.googleapis.com", port: int = 443, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False

