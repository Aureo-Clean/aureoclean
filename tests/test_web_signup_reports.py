"""Website sign-ups become SMS contacts after approval; the Reports tab rules."""
import unittest
import unittest.mock

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
        # Personal data is removed; an anonymous marker keeps the device from signing up again.
        self.assertEqual(self.svc.store.get(self.svc.C_WEB_SIGNUPS, "w1"),
                         {"processed": True, "processedAt": unittest.mock.ANY, "outcome": "imported"})
        self.assertEqual(self.svc.import_web_signups(), 0)  # markers are skipped
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


class PublicReports(Base):
    def setUp(self):
        super().setUp()
        self.fake = FakeSender()
        self.svc.sms = self.fake
        self.svc.sync_notifications = True
        self.svc.store.set(self.svc.C_WEB_SIGNUPS, "w", {"fullName": "Ralph Roque", "phone": "+639171234567"})
        self.svc.import_web_signups()
        self.svc.approve_request(self.admin, self.svc.list_requests("pending")[0]["id"])
        self.fake.sent.clear()
        self.n = 0

    def public(self, **fields):
        self.n += 1
        data = {"location": "2nd Floor · Male", "locationCode": "2F-M", "issues": ["no_soap"], "urgency": "medium",
                "deviceId": "D" * 24, "source": "qr", "createdAt": "2026-10-24T09:00:00+00:00"}
        data.update(fields)
        self.svc.store.set(self.svc.C_PUBLIC_REPORTS, f"r{self.n:019d}", data)

    def test_maps_to_admin_report_with_structured_fields(self):
        self.public(issues=["bad_odor", "no_soap"], rating=2, details="Smells bad near cubicle 2")
        self.assertEqual(self.svc.import_public_reports(), 1)
        self.assertEqual(self.svc.store.query(self.svc.C_PUBLIC_REPORTS), [])  # inbox emptied
        r = self.svc.list_reports()[0]
        self.assertEqual((r["category"], r["priority"], r["title"]), ("supplies", "normal", "Bad smell, No soap"))
        self.assertEqual((r["source"], r["channel"], r["locationCode"], r["rating"]), ("public", "qr", "2F-M", 2))
        self.assertEqual(r["issues"], ["bad_odor", "no_soap"])
        self.assertEqual(len(r["deviceHash"]), 16)  # device id is never stored as-is
        self.assertEqual(self.fake.sent, [])  # medium urgency: no texts

    def test_emergency_texts_everyone_immediately(self):
        self.public(issues=["emergency", "wet_floor"], urgency="high", details="Smoke from the ceiling")
        self.svc.import_public_reports()
        r = self.svc.list_reports()[0]
        self.assertEqual((r["category"], r["priority"]), ("emergency", "critical"))
        self.assertIn("EMERGENCY #1: Emergency, Wet or slippery floor at 2nd Floor - Male", self.fake.sent[0][1])
        self.assertIn("SMS sent to", self.svc.list_reports()[0]["notes"][-1]["text"])

    def test_urgent_texts_janitors(self):
        self.public(issues=["clogged_toilet"], urgency="high")
        self.svc.import_public_reports()
        self.assertEqual(self.svc.list_reports()[0]["category"], "facility")
        self.assertIn("URGENT #1: Clogged toilet", self.fake.sent[0][1])
        self.assertTrue(self.fake.sent[0][1].endswith("Reply OK 1 or DONE 1"))

    def test_spam_from_one_device_is_dropped_but_emergencies_get_through(self):
        for _ in range(8):
            self.public()
        self.assertEqual(self.svc.import_public_reports(), self.svc.PUBLIC_REPORTS_PER_DEVICE_PER_HOUR)
        self.public(issues=["emergency"], urgency="high")
        self.assertEqual(self.svc.import_public_reports(), 1)
        self.assertIn("public_report_dropped", [a["action"] for a in self.svc.list_audit()])

    def test_unknown_issue_codes_are_ignored(self):
        self.public(issues=["hacked", "no_tissue"])
        self.public(issues=["hacked"])
        self.assertEqual(self.svc.import_public_reports(), 1)
        self.assertEqual(self.svc.list_reports()[0]["issues"], ["no_tissue"])


class SharedSchema(unittest.TestCase):
    """The report page, the Pi and the Firestore rules must agree on the answers."""

    def test_rules_list_matches_issues_json(self):
        import json
        import re
        from pathlib import Path
        root = Path(__file__).resolve().parent.parent
        codes = [i["code"] for i in json.loads((root / "docs/assets/issues.json").read_text())["issues"]]
        rules = (root / "firestore.rules").read_text()
        block = re.search(r"issues\.hasOnly\(\[(.*?)\]\)", rules, re.S).group(1)
        self.assertEqual(re.findall(r"'([a-z_]+)'", block), codes)
        self.assertIn(f"issues.size() <= {len(codes)}", rules)


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
        self.assertIn("EMERGENCY #1: Fire alarm at 2F Male CR. Smoke seen. Reply OK 1 or DONE 1", self.fake.sent[0][1])
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


class Analysis(Base):
    """Reports turned into LLM-ready data."""

    def test_dataset_summary_prompt_and_export(self):
        import json
        import tempfile
        from pathlib import Path

        from backend.analysis import analyze, build_prompt, dataset, summarize
        from tools.export_reports import export

        self.svc.sms = FakeSender()
        self.svc.sync_notifications = True
        for i, issues in enumerate([["no_soap"], ["no_soap", "bad_odor"], ["clogged_toilet"]]):
            self.svc.store.set(self.svc.C_PUBLIC_REPORTS, f"r{i:019d}", {
                "location": "2nd Floor · Male", "issues": issues, "urgency": "medium", "rating": 2 + i,
                "deviceId": f"{i}" * 24, "source": "qr", "contactName": "Secret Person",
                "contactPhone": "+639170000000"})
        self.svc.import_public_reports()
        done = self.svc.list_reports()[0]
        self.svc.update_report(self.admin, done["id"], "resolved")

        records = dataset(self.svc)
        self.assertEqual(len(records), 3)
        blob = json.dumps(records)
        self.assertNotIn("Secret Person", blob)      # no personal data
        self.assertNotIn("+639170000000", blob)
        self.assertNotIn("deviceHash", blob)

        s = summarize(records)
        self.assertEqual(s["totalReports"], 3)
        self.assertEqual(s["openReports"], 2)
        self.assertEqual(s["byIssue"]["No soap"], 2)
        self.assertEqual(s["recurringProblems"][0], {"location": "2nd Floor · Male", "issue": "No soap", "count": 2})
        self.assertEqual(s["averageCleanlinessByLocation"]["2nd Floor · Male"], 3.0)

        prompt = build_prompt(records, s)
        payload = json.loads(prompt["user"])
        self.assertEqual(payload["summary"]["totalReports"], 3)
        self.assertIn("answerFormat", payload)
        self.assertIn("fixed alert rules", prompt["system"])

        # A plugged-in LLM gets the prompt and its JSON answer is parsed.
        seen = {}

        def fake_llm(system, user):
            seen["user"] = user
            return 'Sure! {"headline": "Soap runs out on 2F Male", "patterns": []}'
        result = analyze(self.svc, ask=fake_llm)
        self.assertEqual(result["answer"]["headline"], "Soap runs out on 2F Male")
        self.assertIn("No soap", seen["user"])

        with tempfile.TemporaryDirectory() as tmp:
            out = export(self.svc, Path(tmp), 30)
            self.assertEqual(out["count"], 3)
            lines = out["paths"]["reports"].read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(out["paths"]["reports_csv"].read_text().startswith("id,ref,createdAt"))
