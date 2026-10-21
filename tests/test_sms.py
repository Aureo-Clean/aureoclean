"""SMS: phone verification flow, alert opt-in, admin test SMS, modem AT driver."""
import re
import unittest

from backend.service import BadRequest, Forbidden, ServiceUnavailable, TooManyAttempts
from backend.sms import ModemSender, SmsError, gsm_text, normalize_phone
from tests.test_service import Base


class FakeSender:
    mode = "modem"

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, to, text):
        if self.fail:
            raise SmsError("The modem is not answering.")
        self.sent.append((to, text))

    def check(self):
        return {"mode": "modem", "ok": True, "detail": "fake"}

    def last_code(self):
        return re.search(r"\d{6}", self.sent[-1][1]).group(0)


class PhoneNumbers(unittest.TestCase):
    def test_normalize(self):
        for raw in ["09171234567", "0917 123 4567", "917-123-4567", "639171234567", "+63 917 123 4567"]:
            self.assertEqual(normalize_phone(raw), "+639171234567")
        self.assertEqual(normalize_phone("+6591234567"), "+6591234567")
        for bad in ["12345", "0817123456", "abc", ""]:
            with self.assertRaises(ValueError):
                normalize_phone(bad)

    def test_gsm_text(self):
        self.assertEqual(gsm_text("Señor “ok” — done"), 'Senor "ok" - done')
        self.assertEqual(len(gsm_text("x" * 300)), 160)


class SmsFlow(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeSender()
        self.svc.sms = self.fake
        rid, code = self.approved()
        self.svc.activate_janitor("MarkAgot", code, "Janitor123")
        self.jan = self.svc.require_session(self.svc.login("MarkAgot", "Janitor123")["token"])

    def test_add_verify_toggle_remove(self):
        res = self.svc.start_phone_verification(self.jan, "0917 123 4567")
        self.assertEqual(res["phone"], "+639171234567")
        self.assertEqual(self.fake.sent[-1][0], "+639171234567")
        self.assertEqual(self.svc.me(self.jan)["smsPendingPhone"], "+639171234567")

        with self.assertRaises(BadRequest):
            self.svc.confirm_phone(self.jan, "000000" if self.fake.last_code() != "000000" else "111111")
        profile = self.svc.confirm_phone(self.jan, self.fake.last_code())
        self.assertEqual((profile["smsPhone"], profile["smsAlerts"]), ("+639171234567", True))
        self.assertIsNone(profile["smsPendingPhone"])
        self.assertEqual([r["username"] for r in self.svc.sms_recipients("janitor")], ["MarkAgot"])

        self.assertFalse(self.svc.set_sms_alerts(self.jan, False)["smsAlerts"])
        self.assertEqual(self.svc.sms_recipients(), [])
        self.svc.set_sms_alerts(self.jan, True)

        self.assertIsNone(self.svc.remove_phone(self.jan)["smsPhone"])
        with self.assertRaises(BadRequest):
            self.svc.set_sms_alerts(self.jan, True)

    def test_wrong_codes_and_cooldown(self):
        self.svc.start_phone_verification(self.jan, "09171234567")
        with self.assertRaises(TooManyAttempts):  # 60 s cooldown
            self.svc.start_phone_verification(self.jan, "09181234567")
        right = self.fake.last_code()
        wrong = "123456" if right != "123456" else "654321"
        for _ in range(4):
            with self.assertRaises(BadRequest):
                self.svc.confirm_phone(self.jan, wrong)
        with self.assertRaises(BadRequest):
            self.svc.confirm_phone(self.jan, wrong)  # 5th wrong
        with self.assertRaises(Forbidden):  # locked: even the right code fails now
            self.svc.confirm_phone(self.jan, right)

    def test_modem_failure_is_reported(self):
        self.fake.fail = True
        with self.assertRaises(ServiceUnavailable):
            self.svc.start_phone_verification(self.jan, "09171234567")

    def test_admin_test_sms_and_links(self):
        with self.assertRaises(BadRequest):
            self.svc.send_test_sms(self.admin, self.jan.uid)
        self.svc.start_phone_verification(self.jan, "09171234567")
        self.svc.confirm_phone(self.jan, self.fake.last_code())
        self.svc.send_test_sms(self.admin, self.jan.uid)
        self.assertIn("test message", self.fake.sent[-1][1])

        self.svc.settings = __import__("dataclasses").replace(self.svc.settings, dashboard_url="https://app.arduino.cc/dashboards/x",
                                                              firebase_project_id="aureoclean-b7c2b")
        self.assertEqual([l["key"] for l in self.svc.links(self.jan)], ["dashboard"])
        self.assertEqual([l["key"] for l in self.svc.links(self.admin)], ["dashboard", "arduino", "firebase"])


class FakeSerial:
    """Answers like a SIM900A in text mode."""

    def __init__(self, registered=True, accept_sms=True):
        self.out = b""
        self.writes = []
        self.registered, self.accept_sms = registered, accept_sms

    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, n=1):
        data, self.out = self.out[:n], self.out[n:]
        return data

    def reset_input_buffer(self):
        self.out = b""

    def write(self, data):
        self.writes.append(data)
        cmd = data.decode(errors="replace")
        if cmd.startswith("AT+CMGS="):
            self.out += b"\r\n> "
        elif cmd.endswith("\x1a"):
            self.out += b"\r\n+CMGS: 12\r\n\r\nOK\r\n" if self.accept_sms else b"\r\n+CMS ERROR: 500\r\n"
        elif cmd.startswith("AT+CREG?"):
            self.out += b"\r\n+CREG: 0,1\r\n\r\nOK\r\n" if self.registered else b"\r\n+CREG: 0,2\r\n\r\nOK\r\n"
        elif cmd.startswith("AT+CSQ"):
            self.out += b"\r\n+CSQ: 18,0\r\n\r\nOK\r\n"
        else:
            self.out += b"\r\nOK\r\n"

    def close(self):
        pass


class ModemDriver(unittest.TestCase):
    def modem(self, **kw):
        m = ModemSender("/dev/fake", 9600)
        m.fake = FakeSerial(**kw)
        m._open = lambda: m.fake
        return m

    def test_send(self):
        m = self.modem()
        m.send("+639171234567", "Hello “there”")
        sent = b"".join(m.fake.writes)
        self.assertIn(b'AT+CMGF=1\r', sent)
        self.assertIn(b'AT+CMGS="+639171234567"\r', sent)
        self.assertIn(b'Hello "there"\x1a', sent)

    def test_network_rejects(self):
        with self.assertRaises(SmsError):
            self.modem(accept_sms=False).send("+639171234567", "x")

    def test_check(self):
        self.assertTrue(self.modem().check()["ok"])
        status = self.modem(registered=False).check()
        self.assertFalse(status["ok"])
        self.assertIn("searching", status["detail"])


if __name__ == "__main__":
    unittest.main()
