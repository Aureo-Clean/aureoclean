"""End-to-end tests of the account rules using the local SQLite provider.

Run from the project folder:  python3 -m unittest discover -s tests -v
"""
import dataclasses
from pathlib import Path
import unittest

from backend.config import load_settings
from backend.providers.local import LocalBackend
from backend.service import (
    AureoService,
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    TooManyAttempts,
    Unauthorized,
)

PW = "CleanPass123"


def make_service(**overrides) -> AureoService:
    # Never touch the real ~/.config/aureoclean (SMS settings, SIM promo) from tests.
    import tempfile
    overrides.setdefault("sms_settings_file", Path(tempfile.mkdtemp(prefix="aureo-test-")) / "sms.json")
    settings = dataclasses.replace(load_settings(), mode="local", **overrides)
    backend = LocalBackend(":memory:")
    return AureoService(settings, backend, backend)


class Base(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        self.svc.bootstrap_admin("Head Admin", "headadmin", "admin@example.com", PW)
        self.admin = self.svc.require_session(self.svc.login("headadmin", PW)["token"], "admin")
        self.addCleanup(lambda: self.svc.store.close())

    def request(self, username="MarkAgot", email="mark@example.com"):
        return self.svc.submit_janitor_request("Mark Agot", username, email, "+639171234567")

    def approved(self, username="MarkAgot", email="mark@example.com"):
        req = self.request(username, email)
        return req["requestId"], self.svc.approve_request(self.admin, req["requestId"])["code"]


class JanitorFlow(Base):
    def test_full_flow_request_approve_activate_login(self):
        req = self.request()
        self.assertEqual(req["status"], "pending")
        self.assertEqual(len(self.svc.list_requests("pending")), 1)
        # No sign-in account exists before approval + activation.
        self.assertIsNone(self.svc.identity.find_uid("mark@example.com"))
        with self.assertRaises(Forbidden):
            self.svc.login("MarkAgot", PW)

        code = self.svc.approve_request(self.admin, req["requestId"])["code"]
        listed = self.svc.list_requests("approved")[0]
        self.assertNotIn("activationCodeHash", listed)

        self.svc.activate_janitor("markagot", code.lower(), "Janitor123")
        result = self.svc.login("MarkAgot", "Janitor123")
        self.assertEqual(result["user"]["role"], "janitor")
        # Email login works too.
        self.assertEqual(self.svc.login("MARK@example.com", "Janitor123")["user"]["username"], "MarkAgot")

        janitor = self.svc.require_session(result["token"])
        with self.assertRaises(Forbidden):
            self.svc.require_session(result["token"], "admin")
        self.assertEqual(self.svc.janitor_overview(janitor)["profile"]["role"], "janitor")

        actions = [a["action"] for a in self.svc.list_audit()]
        for expected in ("janitor_request_submitted", "janitor_request_approved", "janitor_activated"):
            self.assertIn(expected, actions)

    def test_code_cannot_be_reused(self):
        _, code = self.approved()
        self.svc.activate_janitor("MarkAgot", code, "Janitor123")
        with self.assertRaises(Conflict):
            self.svc.activate_janitor("MarkAgot", code, "Another123")

    def test_wrong_codes_lock_the_request_until_reissued(self):
        rid, code = self.approved()
        for _ in range(4):
            with self.assertRaises(BadRequest):
                self.svc.activate_janitor("MarkAgot", "AAAA-AAAA", "Janitor123")
        with self.assertRaises(Forbidden):
            self.svc.activate_janitor("MarkAgot", "AAAA-AAAA", "Janitor123")
        # Even the right code is refused now.
        with self.assertRaises(Forbidden):
            self.svc.activate_janitor("MarkAgot", code, "Janitor123")
        new_code = self.svc.reissue_code(self.admin, rid)["code"]
        self.svc.activate_janitor("MarkAgot", new_code, "Janitor123")

    def test_expired_code_rejected(self):
        self.svc.store.close()
        self.svc = make_service(activation_code_hours=0)
        self.svc.bootstrap_admin("Head Admin", "headadmin", "admin@example.com", PW)
        self.admin = self.svc.require_session(self.svc.login("headadmin", PW)["token"])
        _, code = self.approved()
        with self.assertRaises(Forbidden):
            self.svc.activate_janitor("MarkAgot", code, "Janitor123")

    def test_cannot_activate_before_approval(self):
        self.request()
        with self.assertRaises(Forbidden):
            self.svc.activate_janitor("MarkAgot", "ABCD-EFGH", "Janitor123")

    def test_reject_frees_username(self):
        req = self.request()
        self.svc.reject_request(self.admin, req["requestId"], "Not on staff list")
        # Same username (and email) may apply again.
        self.request()
        with self.assertRaises(Conflict):
            self.svc.approve_request(self.admin, req["requestId"])

    def test_duplicates_rejected(self):
        self.request()
        with self.assertRaises(Conflict):
            self.request(username="markagot", email="other@example.com")
        with self.assertRaises(Conflict):
            self.request(username="SomeoneElse", email="mark@example.com")
        with self.assertRaises(Conflict):
            self.request(username="HeadAdmin", email="new@example.com")

    def test_validation(self):
        for bad in [("x", "a@b.co"), ("good_name", "not-an-email"), ("bad name!", "a@b.co")]:
            with self.assertRaises(BadRequest):
                self.svc.submit_janitor_request("Mark Agot", *bad)
        _, code = self.approved()
        with self.assertRaises(BadRequest):
            self.svc.activate_janitor("MarkAgot", code, "short")
        with self.assertRaises(BadRequest):  # 17 characters: over the 16 limit
            self.svc.activate_janitor("MarkAgot", code, "Abcdefgh123456789")
        self.svc.activate_janitor("MarkAgot", code, "Abcdefgh12345678")  # exactly 16 is fine

    def test_status_check(self):
        req = self.request()
        self.assertEqual(self.svc.check_request_status("markagot", "mark@example.com")["status"], "pending")
        with self.assertRaises(NotFound):
            self.svc.check_request_status("markagot", "wrong@example.com")
        self.svc.reject_request(self.admin, req["requestId"], "duplicate")
        with self.assertRaises(NotFound):  # username was released
            self.svc.check_request_status("markagot", "mark@example.com")


class AdminFlow(Base):
    def test_invite_registration(self):
        invite = self.svc.create_admin_invite(self.admin, "second@example.com")
        with self.assertRaises(BadRequest):  # wrong email for this invite
            self.svc.register_admin(invite["code"], "Second Admin", "second", "other@example.com", PW)
        self.svc.register_admin(invite["code"].lower(), "Second Admin", "second", "second@example.com", PW)
        self.assertEqual(self.svc.login("second", PW)["user"]["role"], "admin")
        with self.assertRaises(BadRequest):  # single use
            self.svc.register_admin(invite["code"], "Third", "third", "second@example.com", PW)

    def test_invalid_and_revoked_invites(self):
        with self.assertRaises(BadRequest):
            self.svc.register_admin("ZZZZZ-ZZZZZ-ZZZZZ", "Bad Actor", "badactor", "x@example.com", PW)
        invite = self.svc.create_admin_invite(self.admin)
        self.svc.revoke_invite(self.admin, invite["inviteId"])
        with self.assertRaises(BadRequest):
            self.svc.register_admin(invite["code"], "Late Admin", "late", "late@example.com", PW)

    def test_failed_registration_returns_invite(self):
        invite = self.svc.create_admin_invite(self.admin)
        with self.assertRaises(Conflict):  # username taken
            self.svc.register_admin(invite["code"], "Dup", "headadmin", "dup@example.com", PW)
        # Invite still usable after the failure.
        self.svc.register_admin(invite["code"], "Dup", "dupadmin", "dup@example.com", PW)

    def test_link_existing_account_without_touching_it(self):
        # Simulate an auth account that exists without an AureoClean profile.
        uid = self.svc.identity.create_account("old@example.com", "OldPass123", "Old")
        with self.assertRaises(Forbidden):
            self.svc.login("old@example.com", "OldPass123")
        with self.assertRaises(Conflict):
            self.svc.bootstrap_admin("Old Admin", "oldadmin", "old@example.com", PW)
        profile = self.svc.bootstrap_admin("Old Admin", "oldadmin", "old@example.com", link_existing=True)
        self.assertEqual(profile["uid"], uid)
        # Original password still works.
        self.assertEqual(self.svc.login("oldadmin", "OldPass123")["user"]["role"], "admin")
        with self.assertRaises(Conflict):
            self.svc.bootstrap_admin("Old Admin", "oldadmin2", "old@example.com", link_existing=True)

    def test_disable_user_and_last_admin_protection(self):
        rid, code = self.approved()
        self.svc.activate_janitor("MarkAgot", code, "Janitor123")
        token = self.svc.login("MarkAgot", "Janitor123")["token"]
        uid = self.svc.require_session(token).uid

        self.svc.set_user_status(self.admin, uid, False)
        with self.assertRaises(Unauthorized):  # existing session revoked
            self.svc.require_session(token)
        with self.assertRaises(Forbidden):
            self.svc.login("MarkAgot", "Janitor123")
        self.svc.set_user_status(self.admin, uid, True)
        self.svc.login("MarkAgot", "Janitor123")

        with self.assertRaises(BadRequest):
            self.svc.set_user_status(self.admin, self.admin.uid, False)

    def test_change_password(self):
        with self.assertRaises(Unauthorized):
            self.svc.change_password(self.admin, "WrongPass1", "NewPass456")
        self.svc.change_password(self.admin, PW, "NewPass456")
        with self.assertRaises(Unauthorized):
            self.svc.login("headadmin", PW)
        self.svc.login("headadmin", "NewPass456")

    def test_login_lockout(self):
        for _ in range(5):
            with self.assertRaises(Unauthorized):
                self.svc.login("headadmin", "WrongPass1")
        with self.assertRaises(TooManyAttempts):
            self.svc.login("headadmin", PW)

    def test_logout(self):
        token = self.svc.login("headadmin", PW)["token"]
        session = self.svc.require_session(token)
        self.svc.logout(session)
        with self.assertRaises(Unauthorized):
            self.svc.require_session(token)

    def test_overview(self):
        self.request()
        ov = self.svc.admin_overview()
        self.assertEqual((ov["pendingRequests"], ov["activeAdmins"]), (1, 1))


if __name__ == "__main__":
    unittest.main()


class DeleteAccount(Base):
    def test_delete_janitor_and_guards(self):
        _, code = self.approved()
        self.svc.activate_janitor("MarkAgot", code, "Janitor123")
        token = self.svc.login("MarkAgot", "Janitor123")["token"]
        uid = self.svc.require_session(token).uid

        with self.assertRaises(BadRequest):  # must type the username
            self.svc.delete_user(self.admin, uid, "someoneelse")
        with self.assertRaises(BadRequest):  # not yourself
            self.svc.delete_user(self.admin, self.admin.uid, "headadmin")

        self.svc.delete_user(self.admin, uid, "markagot")
        with self.assertRaises(Unauthorized):  # signed out at once
            self.svc.require_session(token)
        with self.assertRaises(Unauthorized):  # can't sign in
            self.svc.login("MarkAgot", "Janitor123")
        self.assertIsNone(self.svc.identity.find_uid("mark@example.com"))
        self.assertNotIn("MarkAgot", [u["username"] for u in self.svc.list_users()])
        self.assertIn("user_deleted", [a["action"] for a in self.svc.list_audit()])
        # username is free again
        self.svc.submit_janitor_request("Mark Agot", "MarkAgot", "mark2@example.com")

    def test_admin_can_delete_another_admin(self):
        self.svc.bootstrap_admin("Second", "second", "second@example.com", PW)
        second = self.svc.require_session(self.svc.login("second", PW)["token"])
        self.svc.delete_user(second, self.admin.uid, "HeadAdmin")  # case doesn't matter
        with self.assertRaises(Unauthorized):
            self.svc.login("headadmin", PW)
        self.assertEqual([u["username"] for u in self.svc.list_users()], ["second"])
