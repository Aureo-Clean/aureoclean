"""Firebase always + a secure local copy: offline work, catching up, accounts made offline."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from backend.firestore_format import from_firestore, to_firestore
from backend.localcrypt import load_or_create_key
from backend.providers.base import AccountExists
from backend.providers.local import LocalBackend
from backend.providers.synced import SyncedBackend


class FakeFirebase(LocalBackend):
    """Firestore + Firebase Auth stand-in (an in-memory store with changed_since)."""

    def __init__(self):
        super().__init__(":memory:")
        self.calls = 0

    def changed_since(self, collection, since):
        return [(i, d) for i, d in self.query(collection) if (d.get("_meta") or {}).get("updatedAt", "") > since]


class Net:
    up = True

    def __call__(self):
        return self.up


class SyncBase(unittest.TestCase):
    def setUp(self):
        self.net = Net()
        self.fb = FakeFirebase()
        self.local = LocalBackend(":memory:", key=os.urandom(32))
        self.s = SyncedBackend(self.local, lambda: self.fb, prefix="aureo", probe=self.net, device="pi-test")
        self.s.check_online()


class Documents(SyncBase):
    def test_online_write_goes_to_firebase_with_readable_names_and_meta(self):
        self.s.set("aureoReports", "R1", {"title": "Clogged toilet", "createdAt": "2026-10-31T01:00:00+00:00"})
        self.s.sync_once()
        doc = self.fb.get("reports", "R1")
        self.assertEqual(doc["title"], "Clogged toilet")
        self.assertEqual(doc["_meta"]["device"], "pi-test")
        self.assertNotIn("offline", doc["_meta"])
        self.assertEqual(self.s.status()["pending"], 0)

    def test_offline_changes_wait_then_catch_up(self):
        self.net.up = False
        self.s.check_online()
        self.s.set("aureoReports", "R2", {"title": "Leak"})
        self.s.transact("aureoReports", "R2", lambda cur: {**cur, "status": "resolved"})
        self.s.set("aureoReports", "R3", {"title": "Temp"})
        self.s.delete("aureoReports", "R3")
        self.s.sync_once()
        self.assertEqual(self.s.status()["pending"], 2)
        self.assertIsNone(self.fb.get("reports", "R2"))
        self.assertTrue(self.local.get("aureoReports", "R2")["_meta"]["offline"])
        self.net.up = True
        self.s.sync_once()
        self.assertEqual(self.fb.get("reports", "R2")["status"], "resolved")
        self.assertIsNone(self.fb.get("reports", "R3"))
        self.assertEqual(self.s.status()["pending"], 0)

    def test_changes_made_in_firebase_come_down(self):
        self.s.sync_once(force_pull=True)
        self.fb.set("reports", "R9", {"title": "From the console", "_meta": {"updatedAt": "2099-01-01T00:00:00+00:00"}})
        self.s.sync_once(force_pull=True)
        self.assertEqual(self.local.get("aureoReports", "R9")["title"], "From the console")

    def test_a_local_change_not_yet_sent_wins(self):
        self.s.sync_once(force_pull=True)
        self.net.up = False
        self.s.check_online()
        self.s.set("aureoReports", "R5", {"title": "mine"})
        self.fb.set("reports", "R5", {"title": "theirs", "_meta": {"updatedAt": "2099-01-01T00:00:00+00:00"}})
        self.net.up = True
        self.s.sync_once(force_pull=True)
        self.assertEqual(self.fb.get("reports", "R5")["title"], "mine")

    def test_old_collection_names_are_copied_over(self):
        self.fb.set("aureoUsers", "u1", {"username": "headadmin"})
        self.s.sync_once(force_pull=True)
        self.assertEqual(self.local.get("aureoUsers", "u1")["username"], "headadmin")
        self.s.sync_once()
        self.assertEqual(self.fb.get("users", "u1")["username"], "headadmin")
        self.assertIsNotNone(self.fb.get("aureoUsers", "u1"))          # the old one is left alone

    def test_sessions_stay_on_the_pi_and_public_pages_read_firebase(self):
        self.s.set("aureoSessions", "x", {"uid": "u"})
        self.fb.set("aureoPublicReports", "p1", {"issues": ["leak"]})
        self.s.sync_once()
        self.assertIsNone(self.fb.get("aureoSessions", "x"))
        self.assertEqual([i for i, _ in self.s.query("aureoPublicReports")], ["p1"])


class Accounts(SyncBase):
    def test_online_sign_in_is_remembered_for_offline(self):
        uid = self.fb.create_account("ana@example.com", "Secret123", "Ana")
        self.assertEqual(self.s.verify_password("ana@example.com", "Secret123"), uid)
        self.net.up = False
        self.s.check_online()
        self.assertEqual(self.s.verify_password("ana@example.com", "Secret123"), uid)
        self.assertIsNone(self.s.verify_password("ana@example.com", "wrong"))

    def test_account_made_offline_is_marked_and_created_in_firebase_later(self):
        self.net.up = False
        self.s.check_online()
        uid = self.s.create_account("ben@example.com", "Secret123", "Ben")
        self.s.set("aureoUsers", uid, {"uid": uid, "username": "ben"})
        self.assertTrue(self.local.get("aureoUsers", uid)["createdLocally"])
        cred = self.local.get("aureoLocalCredentials", uid)
        self.assertEqual(cred["firebaseAuth"], "pending")
        self.assertTrue(cred["passwordHash"].startswith("scrypt$"))
        self.assertNotIn("Secret123", str(cred))
        self.assertEqual(self.s.verify_password("ben@example.com", "Secret123"), uid)   # signs in offline
        with self.assertRaises(AccountExists):
            self.s.create_account("ben@example.com", "x" * 10, "Ben 2")
        self.net.up = True
        self.s.sync_once(force_pull=True)
        self.assertEqual(self.fb.find_uid("ben@example.com"), uid)                    # same UID in Firebase Auth
        self.assertEqual(self.fb.verify_password("ben@example.com", "Secret123"), uid)
        self.assertTrue(self.fb.get("users", uid)["createdLocally"])
        self.assertEqual(self.fb.get("localCredentials", uid)["firebaseAuth"], "created")
        self.assertEqual(self.local.auth_ops(), [])                                   # the password is gone from the outbox

    def test_password_change_offline_reaches_firebase(self):
        uid = self.fb.create_account("cy@example.com", "OldPass123", "Cy")
        self.s.verify_password("cy@example.com", "OldPass123")
        self.net.up = False
        self.s.check_online()
        self.s.set_password(uid, "NewPass123")
        self.assertEqual(self.s.verify_password("cy@example.com", "NewPass123"), uid)
        self.net.up = True
        self.s.sync_once()
        self.assertEqual(self.fb.verify_password("cy@example.com", "NewPass123"), uid)


class AtRest(unittest.TestCase):
    def test_documents_are_encrypted_on_disk(self):
        d = Path(tempfile.mkdtemp())
        key = load_or_create_key(d / "local.key")
        self.assertEqual(oct((d / "local.key").stat().st_mode & 0o777), "0o600")
        db = LocalBackend(d / "data" / "a.db", key=key)
        db.set("aureoReports", "R1", {"title": "Secret restroom report"})
        db.close()
        raw = sqlite3.connect(str(d / "data" / "a.db")).execute("SELECT data FROM docs").fetchone()[0]
        self.assertTrue(raw.startswith("enc1:"))
        self.assertNotIn("Secret", raw)
        self.assertEqual(LocalBackend(d / "data" / "a.db", key=key).get("aureoReports", "R1")["title"], "Secret restroom report")
        from cryptography.exceptions import InvalidTag
        with self.assertRaises(InvalidTag):
            LocalBackend(d / "data" / "a.db", key=os.urandom(32)).get("aureoReports", "R1")


class Format(unittest.TestCase):
    def test_times_become_timestamps_and_back(self):
        doc = {"createdAt": "2026-10-31T01:02:03+00:00", "title": "2026-10-31T01:02:03+00:00",
               "notes": [{"at": "x", "editedAt": "2026-10-31T05:00:00+00:00"}], "_meta": {"updatedAt": "2026-10-31T06:00:00+00:00"}}
        fs = to_firestore(doc)
        self.assertEqual(type(fs["createdAt"]).__name__, "datetime")
        self.assertIsInstance(fs["title"], str)                         # only *At / *Until fields
        self.assertEqual(type(fs["notes"][0]["editedAt"]).__name__, "datetime")
        self.assertEqual(from_firestore(fs), doc)


class WholeApp(unittest.TestCase):
    def test_admin_and_report_made_offline_reach_firebase(self):
        import dataclasses
        from backend.config import load_settings
        from backend.service import AureoService
        net, fb = Net(), FakeFirebase()
        net.up = False
        local = LocalBackend(":memory:", key=os.urandom(32))
        store = SyncedBackend(local, lambda: fb, prefix="aureo", probe=net)
        store.check_online()
        settings = dataclasses.replace(load_settings(), mode="firebase",
                                       sms_settings_file=Path(tempfile.mkdtemp()) / "sms.json")
        svc = AureoService(settings, store, store)
        svc.sync_notifications = True
        svc.bootstrap_admin("Head Admin", "headadmin", "admin@example.com", "CleanPass123")
        session = svc.require_session(svc.login("headadmin", "CleanPass123")["token"], "admin")   # offline sign-in
        rep = svc.create_report(session, "facility", "Broken faucet", "2nd Floor · Male")
        net.up = True
        store.sync_once(force_pull=True)
        user = next(d for _, d in fb.query("users"))
        self.assertEqual(user["username"], "headadmin")
        self.assertTrue(user["createdLocally"])
        self.assertEqual(fb.get("reports", rep["id"])["title"], "Broken faucet")
        self.assertIsNotNone(fb.find_uid("admin@example.com"))
        self.assertEqual(fb.query("aureoSessions"), [])
        self.assertEqual(store.status()["pending"], 0)


class Feed(unittest.TestCase):
    def test_webhook_and_clean_layouts_and_overlay(self):
        from backend.sensor_feed import SensorFeed
        f = SensorFeed(lambda: None)
        f.apply("put", "/", {
            "-Nab1": {"thing_id": "t-uuid", "values": [{"name": "temperature", "value": 31.5, "updated_at": "2026-10-31T02:00:05Z"},
                                                         {"name": "iaq", "value": 80, "updated_at": "2026-10-31T02:00:05Z"}]},
            "-Nab2": {"thing_id": "t-uuid", "values": [{"name": "temperature", "value": 33.0, "updated_at": "2026-10-31T02:00:15Z"}]},
            "Urinal01": {"temperature": 29.0, "updatedAt": 1793152800000},
        })
        self.assertEqual(f.lookup("t-uuid", "Toilet01")["temperature"][0], 33.0)       # newest push wins
        self.assertEqual(f.lookup("x", "urinal01")["temperature"][0], 29.0)
        self.assertEqual(f.status()["format"], "clean+webhook")
        f.apply("patch", "/Urinal01", {"temperature": 30.0, "updatedAt": 1793152900000})
        self.assertEqual(f.lookup("x", "Urinal01")["temperature"][0], 30.0)
        data = {"things": [{"id": "t-uuid", "name": "Toilet01", "properties": [
            {"name": "temperature", "value": 30.0, "updatedAt": "2026-10-31T02:00:00+00:00"},
            {"name": "iaq", "value": 90, "updatedAt": "2026-10-31T02:01:00+00:00"}]}]}
        f.overlay(data)
        props = {p["name"]: p for p in data["things"][0]["properties"]}
        self.assertEqual((props["temperature"]["value"], props["temperature"]["via"]), (33.0, "firebase"))
        self.assertEqual(props["iaq"]["value"], 90)                                       # Arduino's is newer: kept
        f.apply("put", "/-Nab2", None)
        self.assertEqual(f.lookup("t-uuid", "")["temperature"][0], 31.5)


class LocalModeAccounts(unittest.TestCase):
    def test_account_from_local_mode_moves_into_firebase_on_first_online_sign_in(self):
        local = LocalBackend(":memory:", key=os.urandom(32))
        uid = local.create_account("head@example.com", "CleanPass123", "Head")       # made while in local mode
        local.set("aureoUsers", uid, {"uid": uid, "username": "headadmin"})
        fb, net = FakeFirebase(), Net()
        s = SyncedBackend(local, lambda: fb, prefix="aureo", probe=net)
        s.check_online()
        self.assertIsNone(s.verify_password("head@example.com", "wrong"))
        self.assertEqual(s.verify_password("head@example.com", "CleanPass123"), uid)
        self.assertEqual(fb.verify_password("head@example.com", "CleanPass123"), uid)
        s.sync_once(force_pull=True)
        self.assertTrue(fb.get("users", uid)["createdLocally"])
        self.assertEqual(fb.get("localCredentials", uid)["firebaseAuth"], "created")
