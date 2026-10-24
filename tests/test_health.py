"""System health panel: checks, job tracking, SIM load via USSD."""
import unittest
from unittest import mock

from backend.health import HealthMonitor, _peso_amount
from backend.sms import ModemSender, SmsError
from tests.test_service import Base
from tests.test_sms import FakeSerial


class UssdModem(unittest.TestCase):
    def modem(self, reply):
        m = ModemSender("/dev/fake", 9600)
        fake = FakeSerial()
        orig = fake.write

        def write(data):
            if data.startswith(b"AT+CUSD=1"):
                fake.writes.append(data)
                fake.out += b"\r\nOK\r\n" + reply
            else:
                orig(data)
        fake.write = write
        m._open = lambda: fake
        m.fake = fake
        return m

    def test_balance_reply_is_read(self):
        m = self.modem(b'\r\n+CUSD: 0,"Your balance is P15.50 valid until 11/30/2026.",15\r\n')
        self.assertEqual(m.ussd("*143#"), "Your balance is P15.50 valid until 11/30/2026.")
        self.assertIn(b'AT+CUSD=1,"*143#",15\r', b"".join(m.fake.writes))

    def test_bad_code_and_no_answer(self):
        m = self.modem(b"")
        with self.assertRaises(SmsError):
            m.ussd("143; AT+CMGD")  # only * # and digits are allowed
        with self.assertRaises(SmsError):
            m.ussd("*143#", timeout=0.3)

    def test_peso_amounts(self):
        self.assertEqual(_peso_amount("Bal: PHP 120.00"), 120.0)
        self.assertEqual(_peso_amount("Load ₱5"), 5.0)
        self.assertIsNone(_peso_amount("You have 3 free texts"))


class HealthReport(Base):
    def checks(self, **kw):
        with mock.patch.object(HealthMonitor, "internet",
                               lambda self: {"key": "internet", "name": "Internet", "status": "ok", "detail": ""}):
            report = self.svc.health.report(**kw)
        return report, {c["key"]: c for c in report["checks"]}

    def test_test_mode_and_jobs_not_run_are_warnings(self):
        report, c = self.checks()
        self.assertEqual(c["modem"]["status"], "warn")
        self.assertNotIn("balance", c)  # optional, hidden until a balance code is set
        self.assertEqual(c["promo"]["status"], "off")
        self.assertEqual(c["job_web"]["status"], "warn")
        self.assertEqual(report["status"], "warn")
        self.assertGreaterEqual(report["problems"], 2)

    def test_job_tracking(self):
        self.svc.run_job("web", lambda: (0, 0))
        _, c = self.checks()
        self.assertEqual(c["job_web"]["status"], "ok")
        with self.assertRaises(RuntimeError):
            self.svc.run_job("sms_inbox", mock.Mock(side_effect=RuntimeError("port gone")))
        _, c = self.checks()
        self.assertEqual(c["job_sms_inbox"]["status"], "fail")
        self.assertIn("port gone", c["job_sms_inbox"]["detail"])

    def test_low_balance_warns(self):
        modem = mock.Mock(mode="modem")
        modem.check.return_value = {"ok": True, "detail": "Signal good"}
        modem.ussd.return_value = "Your balance is P8.00"
        self.svc.sms = modem
        self.svc.health.balance_code = "*143#"
        _, c = self.checks()
        self.assertEqual(c["modem"]["status"], "ok")
        self.assertEqual(c["balance"]["status"], "warn")
        self.assertIn("Low load", c["balance"]["detail"])
        self.checks()
        self.assertEqual(modem.ussd.call_count, 1)  # cached for an hour
        self.checks(force_balance=True)
        self.assertEqual(modem.ussd.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class SimPromoTracking(Base):
    def setUp(self):
        super().setUp()
        from datetime import datetime, timedelta

        from backend.service import PH_TZ
        today = datetime.now(PH_TZ).date()
        self.days_ago = lambda n: (today - timedelta(days=n)).isoformat()

    def status(self):
        with mock.patch.object(HealthMonitor, "internet",
                               lambda self: {"key": "internet", "name": "Internet", "status": "ok", "detail": ""}):
            return {c["key"]: c for c in self.svc.health.report()["checks"]}

    def test_promo_countdown(self):
        p = self.svc.set_sim_promo(self.admin, "Globe unli calls & texts", 15)
        self.assertEqual((p["daysLeft"], p["name"]), (15, "Globe unli calls & texts"))
        self.assertEqual(self.status()["promo"]["status"], "ok")
        self.svc.set_sim_promo(self.admin, "Globe unli", 15, self.days_ago(14))  # 1 day left
        self.assertEqual(self.status()["promo"]["status"], "warn")
        self.assertIn("tomorrow", self.status()["promo"]["detail"])
        self.svc.set_sim_promo(self.admin, "Globe unli", 15, self.days_ago(16))
        self.assertEqual(self.status()["promo"]["status"], "fail")
        from backend.service import BadRequest
        with self.assertRaises(BadRequest):
            self.svc.set_sim_promo(self.admin, "x", 0)
        self.assertEqual(self.svc.clear_sim_promo(self.admin), {"set": False})

    def test_reminder_texts_admins_once_per_stage(self):
        from tests.test_two_way import Recorder
        rec = Recorder()
        rec.mode = "modem"
        self.svc.sms = rec
        self.svc.store.transact(self.svc.C_USERS, self.admin.uid,
                                lambda u: {**u, "smsPhone": "+639171234567", "smsAlerts": True})
        self.svc.set_sim_promo(self.admin, "Globe unli", 15)
        self.assertFalse(self.svc.promo_reminder())  # plenty of days left
        self.svc.set_sim_promo(self.admin, "Globe unli", 15, self.days_ago(14))
        self.assertTrue(self.svc.promo_reminder())
        self.assertIn("ends tomorrow", rec.sent[-1][1])
        self.assertFalse(self.svc.promo_reminder())  # only once
        self.svc.set_sim_promo(self.admin, "Globe unli", 15, self.days_ago(16))
        self.assertTrue(self.svc.promo_reminder())
        self.assertIn("has ended", rec.sent[-1][1])
        self.assertTrue(all(len(t) <= 160 for _, t in rec.sent))

    def test_failed_texts_show_up(self):
        from backend.sms import SmsError
        bad = mock.Mock(mode="modem")
        bad.send.side_effect = SmsError("The network refused the text (CMS ERROR 21).")
        self.svc.sms = bad
        for _ in range(2):
            with self.assertRaises(SmsError):
                self.svc.sms.send("+639181234567", "hi")
        c = self.status()["sending"]
        self.assertEqual(c["status"], "fail")
        self.assertIn("promo may have run out", c["detail"])
        bad.send.side_effect = None
        self.svc.sms.send("+639181234567", "hi")
        self.assertEqual(self.status()["sending"]["status"], "ok")
