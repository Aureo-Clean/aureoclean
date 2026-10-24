"""Kiosk idle lock: PIN set-up and unlocking."""
import os
from unittest import mock

from backend.service import BadRequest, Unauthorized, idle_lock_minutes
from tests.test_service import PW, Base


class IdleLock(Base):
    def test_unlock_with_password_when_no_pin(self):
        self.assertFalse(self.svc.me(self.admin)["hasLockPin"])
        self.assertEqual(self.svc.unlock(self.admin, PW), {"ok": True})
        with self.assertRaises(BadRequest):
            self.svc.unlock(self.admin, "wrong")

    def test_set_pin_and_unlock(self):
        with self.assertRaises(Unauthorized):
            self.svc.set_lock_pin(self.admin, "not my password", "4826")
        for bad in ("12", "abcd", "1111", "1234", "1234567"):
            with self.assertRaises(BadRequest):
                self.svc.set_lock_pin(self.admin, PW, bad)
        self.assertEqual(self.svc.set_lock_pin(self.admin, PW, "4826"), {"hasLockPin": True})
        self.assertTrue(self.svc.me(self.admin)["hasLockPin"])
        profile = self.svc.store.get(self.svc.C_USERS, self.admin.uid)
        self.assertNotIn("4826", str(profile))  # stored hashed only
        self.assertEqual(self.svc.unlock(self.admin, "4826"), {"ok": True})
        with self.assertRaises(BadRequest):
            self.svc.unlock(self.admin, PW)  # once a PIN is set, the PIN is what unlocks
        self.svc.set_lock_pin(self.admin, PW, "")
        self.assertFalse(self.svc.me(self.admin)["hasLockPin"])

    def test_too_many_wrong_tries_signs_out(self):
        self.svc.set_lock_pin(self.admin, PW, "4826")
        for _ in range(4):
            with self.assertRaises(BadRequest):
                self.svc.unlock(self.admin, "0000")
        with self.assertRaises(Unauthorized):
            self.svc.unlock(self.admin, "0000")
        with self.assertRaises(Unauthorized):  # the session is gone
            self.svc.require_session(self.admin.token, "admin")

    def test_idle_minutes_setting(self):
        with mock.patch.dict(os.environ, {"AUREOCLEAN_IDLE_LOCK_MINUTES": "0"}):
            self.assertEqual(idle_lock_minutes(), 0)
        with mock.patch.dict(os.environ, {"AUREOCLEAN_IDLE_LOCK_MINUTES": "junk"}):
            self.assertEqual(idle_lock_minutes(), 5)
        self.assertIn("idleLockMinutes", self.svc.login("headadmin", PW)["user"])


class KioskExit(Base):
    def test_only_an_admin_password_works(self):
        from backend.service import Forbidden
        self.assertEqual(self.svc.verify_admin("headadmin", PW)["username"], "headadmin")
        with self.assertRaises(Unauthorized):
            self.svc.verify_admin("headadmin", "wrong")
        _, code = self.approved()
        self.svc.activate_janitor("MarkAgot", code, PW)
        self.svc.login("MarkAgot", PW)  # a real, working janitor account…
        with self.assertRaises(Forbidden):  # …still can't take the kiosk out of kiosk mode
            self.svc.verify_admin("MarkAgot", PW)

    def test_overview_cache_only_when_asked(self):
        first = self.svc.admin_overview(cached=True)
        self.request()
        self.assertEqual(self.svc.admin_overview(cached=True), first)  # reused
        self.assertEqual(self.svc.admin_overview()["pendingRequests"], first["pendingRequests"] + 1)
