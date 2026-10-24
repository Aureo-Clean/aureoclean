"""Website sign-ups become SMS contacts after approval; the Reports tab rules."""
import unittest

from backend.service import BadRequest, Unauthorized
from tests.test_service import Base
from tests.test_sms import FakeSender


class WebSignups(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeSender()
        self.svc.sms = self.fake

    def drop(self, doc_id, **fields):
        data = {"fullName": "Ralph Roque", "phone": "+639171234567", "consent": True, "source": "web"}
        data.update(fields)
        self.svc.store.set(self.svc.C_WEB_SIGNUPS, doc_id, data)

    def test_import_approve_becomes_sms_contact(self):
        self.drop("w1")
        self.assertEqual(self.svc.import_web_signups(), 1)
        self.assertEqual(self.svc.store.query(self.svc.C_WEB_SIGNUPS), [])  # inbox emptied
        req = self.svc.list_requests("pending")[0]
        self.assertEqual((req["source"], req["username"], req["phone"]), ("web", "ralph.roque", "+639171234567"))

        result = self.svc.approve_request(self.admin, req["id"])
        self.assertTrue(result["contact"])
        self.assertIn("Welcome to AureoClean, Ralph", self.fake.sent[-1][1])
        self.assertEqual([r["username"] for r in self.svc.sms_recipients("janitor")], ["ralph.roque"])
        # SMS-only contacts can't sign in to the app
        with self.assertRaises(Unauthorized):
            self.svc.login("ralph.roque", "Anything123")
        # ...and can be disabled (no sign-in account involved)
        uid = next(u["uid"] for u in self.svc.list_users() if u["username"] == "ralph.roque")
        self.svc.set_user_status(self.admin, uid, False)
        self.assertEqual(self.svc.sms_recipients("janitor"), [])

    def test_duplicate_phone_and_bad_data_are_rejected_and_texted(self):
        self.drop("w1")
        self.svc.import_web_signups()
        self.drop("w2", fullName="Someone Else")  # same phone
        self.drop("w3", phone="12345")            # invalid
        self.assertEqual(self.svc.import_web_signups(), 0)
        self.assertEqual(len(self.svc.list_requests("pending")), 1)
        self.assertIn("already registered", self.fake.sent[-1][1])
        actions = [a["action"] for a in self.svc.list_audit()]
        self.assertEqual(actions.count("web_signup_rejected"), 2)

    def test_same_names_get_unique_usernames(self):
        self.drop("w1")
        self.drop("w2", phone="+639181234567")
        self.svc.import_web_signups()
        names = sorted(r["username"] for r in self.svc.list_requests("pending"))
        self.assertEqual(names, ["ralph.roque", "ralph.roque2"])


class Reports(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeSender()
        self.svc.sms = self.fake
        self.svc.sync_notifications = True
        # one janitor contact with SMS on
        self.svc.store.set(self.svc.C_WEB_SIGNUPS, "w", {"fullName": "Ralph Roque", "phone": "+639171234567"})
        self.svc.import_web_signups()
        self.svc.approve_request(self.admin, self.svc.list_requests("pending")[0]["id"])
        self.fake.sent.clear()

    def test_emergency_texts_janitors_and_logs_result(self):
        r = self.svc.create_report(self.admin, "emergency", "Fire alarm", "2F Male CR", "Smoke seen", notify="janitors")
        self.assertEqual((r["priority"], r["status"], r["notifying"]), ("critical", "open", 1))
        self.assertEqual(self.fake.sent[0][0], "+639171234567")
        self.assertIn("EMERGENCY: Fire alarm at 2F Male CR", self.fake.sent[0][1])
        report = self.svc.list_reports()[0]
        self.assertIn("SMS sent to 1 of 1", report["notes"][-1]["text"])

    def test_status_flow_and_ordering(self):
        low = self.svc.create_report(self.admin, "facility", "Loose tile", priority="low")
        crit = self.svc.create_report(self.admin, "system", "Sensor offline", priority="critical")
        self.assertEqual([r["id"] for r in self.svc.list_reports()], [crit["id"], low["id"]])
        self.svc.update_report(self.admin, crit["id"], "in_progress", "Checking the ESP32")
        done = self.svc.update_report(self.admin, crit["id"], "resolved")
        self.assertEqual(done["status"], "resolved")
        self.assertTrue(done["resolvedAt"])
        self.assertEqual([r["id"] for r in self.svc.list_reports()], [low["id"], crit["id"]])  # resolved last
        self.assertEqual(len(self.svc.list_reports(status="open")), 1)
        self.assertEqual(self.svc.admin_overview()["openReports"], 1)

    def test_validation(self):
        with self.assertRaises(BadRequest):
            self.svc.create_report(self.admin, "nonsense", "Title")
        with self.assertRaises(BadRequest):
            self.svc.create_report(self.admin, "facility", "x")
        r = self.svc.create_report(self.admin, "facility", "Broken faucet")
        with self.assertRaises(BadRequest):
            self.svc.update_report(self.admin, r["id"])
        self.assertEqual(self.fake.sent, [])  # notify defaults to none


if __name__ == "__main__":
    unittest.main()
