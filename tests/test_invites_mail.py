"""Admin invites are emailed (Gmail app password) and/or texted, and still work as before."""
import smtplib
from unittest import mock

from backend.mailer import Mailer
from backend.service import BadRequest, ServiceUnavailable
from tests.test_service import PW, Base
from tests.test_two_way import Recorder


class FakeSMTP:
    sent = []
    fail_login = False

    def __init__(self, host, port):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"bad")
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class InviteEmail(Base):
    def setUp(self):
        super().setUp()
        FakeSMTP.sent, FakeSMTP.fail_login = [], False
        self.svc.mailer._smtp_factory = FakeSMTP
        self.svc.sms = Recorder()

    def test_without_email_set_up_the_code_is_still_made(self):
        r = self.svc.create_admin_invite(self.admin, "new@gmail.com", 48)
        self.assertFalse(r["emailed"])
        self.assertIn("isn't set up", r["emailError"])
        self.assertTrue(r["code"])

    def test_emailed_invite_can_be_used(self):
        self.svc.set_mail(self.admin, "aureoclean.sti@gmail.com", "abcd efgh ijkl mnop")
        self.assertTrue(self.svc.mail_status()["configured"])
        self.assertNotIn("password", self.svc.mail_status())  # never sent back to the screen
        r = self.svc.create_admin_invite(self.admin, "new.admin@gmail.com", 48, web_url="http://192.168.1.5:8080")
        self.assertTrue(r["emailed"])
        msg = FakeSMTP.sent[-1]
        self.assertEqual(msg["To"], "new.admin@gmail.com")
        body = msg.get_body(("plain",)).get_content()
        self.assertIn(r["code"], body)
        self.assertIn("http://192.168.1.5:8080", body)
        self.assertIn(r["code"], msg.get_body(("html",)).get_content())
        # The invitee registers with the code from the email.
        res = self.svc.register_admin(r["code"], "New Admin", "newadmin", "new.admin@gmail.com", PW)
        self.assertIn("Admin account created", res["message"])
        self.svc.login("newadmin", PW)

    def test_wrong_app_password_is_explained(self):
        self.svc.set_mail(self.admin, "aureoclean.sti@gmail.com", "abcdefghijklmnop")
        FakeSMTP.fail_login = True
        r = self.svc.create_admin_invite(self.admin, "x@gmail.com")
        self.assertFalse(r["emailed"])
        self.assertIn("app password", r["emailError"])
        with self.assertRaises(ServiceUnavailable):
            self.svc.send_test_email(self.admin)

    def test_texted_invite(self):
        r = self.svc.create_admin_invite(self.admin, "", 24, phone="0917 123 4567")
        self.assertTrue(r["texted"])
        to, text = self.svc.sms.sent[-1]
        self.assertEqual(to, "+639171234567")
        self.assertIn(r["code"], text)
        self.assertLessEqual(len(text), 160)

    def test_bad_settings(self):
        with self.assertRaises(BadRequest):
            self.svc.set_mail(self.admin, "not-an-email", "abcdefghijklmnop")
        with self.assertRaises(BadRequest):
            self.svc.set_mail(self.admin, "a@gmail.com", "short")

    def test_password_file_is_private(self):
        import os
        import stat
        self.svc.set_mail(self.admin, "aureoclean.sti@gmail.com", "abcdefghijklmnop")
        mode = stat.S_IMODE(os.stat(self.svc.mailer.file).st_mode)
        self.assertEqual(mode, 0o600)


class MailerEnv(Base):
    def test_config_file_settings_work_too(self):
        m = Mailer(self.svc.mailer.file.with_name("none.json"))
        with mock.patch.dict("os.environ", {"AUREOCLEAN_SMTP_USER": "a@gmail.com", "AUREOCLEAN_SMTP_PASSWORD": "x" * 16}):
            self.assertTrue(m.configured)
