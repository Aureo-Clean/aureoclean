"""Sessions survive a backend restart; invited admins must verify their email."""
import dataclasses
import unittest

from backend.config import load_settings
from backend.providers.local import LocalBackend
from backend.service import AureoService, Forbidden, TooManyAttempts, Unauthorized
from tests.test_service import PW


def service_on(backend) -> AureoService:
    return AureoService(dataclasses.replace(load_settings(), mode="local"), backend, backend)


class Sessions(unittest.TestCase):
    def setUp(self):
        self.backend = LocalBackend(":memory:")
        self.addCleanup(self.backend.close)
        self.svc = service_on(self.backend)
        self.svc.bootstrap_admin("Head Admin", "headadmin", "admin@example.com", PW)

    def test_survives_restart_and_logout_revokes(self):
        token = self.svc.login("headadmin", PW)["token"]
        restarted = service_on(self.backend)  # fresh process: empty cache
        session = restarted.require_session(token, "admin")
        self.assertEqual(session.username, "headadmin")
        restarted.logout(session)
        with self.assertRaises(Unauthorized):
            service_on(self.backend).require_session(token)

    def test_token_not_stored_in_plain_text(self):
        token = self.svc.login("headadmin", PW)["token"]
        ids = [doc_id for doc_id, _ in self.backend.query(self.svc.C_SESSIONS)]
        self.assertNotIn(token, ids)
        self.assertEqual(len(ids), 1)

    def test_disable_user_revokes_saved_sessions(self):
        self.svc.bootstrap_admin("Second", "second", "second@example.com", PW)
        token = self.svc.login("second", PW)["token"]
        admin = self.svc.require_session(self.svc.login("headadmin", PW)["token"])
        uid = self.svc.require_session(token).uid
        self.svc.set_user_status(admin, uid, False)
        with self.assertRaises(Unauthorized):
            service_on(self.backend).require_session(token)

    def test_expired_session_rejected_after_restart(self):
        token = self.svc.login("headadmin", PW)["token"]
        key = self.svc.sessions._key(token)
        doc = self.backend.get(self.svc.C_SESSIONS, key)
        doc["expiresAt"] = "2000-01-01T00:00:00+00:00"
        self.backend.set(self.svc.C_SESSIONS, key, doc)
        with self.assertRaises(Unauthorized):
            service_on(self.backend).require_session(token)


class AdminEmailVerification(unittest.TestCase):
    def setUp(self):
        self.backend = LocalBackend(":memory:")
        self.backend.supports_email_verification = True  # pretend we're on Firebase
        self.addCleanup(self.backend.close)
        self.svc = service_on(self.backend)
        self.svc.bootstrap_admin("Head Admin", "headadmin", "admin@example.com", PW)
        self.admin = self.svc.require_session(self.svc.login("headadmin", PW)["token"])

    def test_invited_admin_must_verify(self):
        invite = self.svc.create_admin_invite(self.admin, "new@example.com")
        result = self.svc.register_admin(invite["code"], "New Admin", "newadmin", "new@example.com", PW)
        self.assertIn("verification link", result["message"])
        self.assertEqual(self.backend.sent_verifications, ["new@example.com"])

        with self.assertRaises(Forbidden) as ctx:
            self.svc.login("newadmin", PW)
        self.assertIn("verify your email", ctx.exception.message)
        # Resend works, but at most 3 emails per 15 minutes in total.
        self.svc.resend_verification("newadmin", PW)
        self.svc.resend_verification("newadmin", PW)
        with self.assertRaises(TooManyAttempts):
            self.svc.resend_verification("newadmin", PW)
        self.assertEqual(len(self.backend.sent_verifications), 3)

        uid = self.backend.find_uid("new@example.com")
        self.backend.mark_email_verified(uid)
        self.assertEqual(self.svc.login("newadmin", PW)["user"]["role"], "admin")
        self.assertIn("admin_email_verified", [a["action"] for a in self.svc.list_audit()])

    def test_resend_needs_the_right_password(self):
        invite = self.svc.create_admin_invite(self.admin)
        self.svc.register_admin(invite["code"], "New Admin", "newadmin", "new@example.com", PW)
        with self.assertRaises(Unauthorized):
            self.svc.resend_verification("newadmin", "WrongPass1")

    def test_bootstrap_admin_not_locked_out(self):
        # The first admin (created/linked on the Pi) doesn't need the email step.
        self.assertEqual(self.svc.login("headadmin", PW)["user"]["role"], "admin")


if __name__ == "__main__":
    unittest.main()
