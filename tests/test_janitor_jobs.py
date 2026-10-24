"""Janitor 'My jobs' in the app: take, done, report a problem (same rules as SMS)."""
from backend.service import BadRequest, Conflict
from tests.test_service import PW, Base
from tests.test_two_way import Recorder


class JanitorJobs(Base):
    def setUp(self):
        super().setUp()
        self.svc.sms = Recorder()
        self.svc.sync_notifications = True
        self.ana = self.janitor("AnaReyes", "ana@example.com", "Ana Reyes")
        self.ben = self.janitor("BenCruz", "ben@example.com", "Ben Cruz")
        self.r1 = self.svc.create_report(self.admin, "facility", "Clogged toilet", "2nd Floor · Male")["id"]
        self.r2 = self.svc.create_report(self.admin, "emergency", "Smoke", "Ground Floor · Male")["id"]

    def janitor(self, username, email, name):
        req = self.svc.submit_janitor_request(name, username, email, "")
        code = self.svc.approve_request(self.admin, req["requestId"])["code"]
        self.svc.activate_janitor(username, code, PW)
        return self.svc.require_session(self.svc.login(username, PW)["token"], "janitor")

    def test_lists_open_jobs_urgent_first(self):
        jobs = self.svc.janitor_jobs(self.ana)
        self.assertEqual([j["title"] for j in jobs["open"]], ["Smoke", "Clogged toilet"])
        self.assertEqual(jobs["mine"], [])
        self.assertTrue(all(j["ref"] for j in jobs["open"]))

    def test_take_then_done(self):
        job = self.svc.janitor_take(self.ana, self.r1)
        self.assertTrue(job["mine"])
        self.assertEqual(job["status"], "in_progress")
        # Ben sees it as taken, not open, and can't take or finish it.
        ben = self.svc.janitor_jobs(self.ben)
        self.assertEqual([j["takenBy"] for j in ben["taken"]], ["Ana"])
        with self.assertRaises(Conflict):
            self.svc.janitor_take(self.ben, self.r1)
        with self.assertRaises(Conflict):
            self.svc.janitor_done(self.ben, self.r1)

        done = self.svc.janitor_done(self.ana, self.r1, "used the plunger")
        self.assertEqual((done["status"], done["doneBy"]), ("resolved", "Ana"))
        jobs = self.svc.janitor_jobs(self.ana)
        self.assertEqual([j["title"] for j in jobs["done"]], ["Clogged toilet"])
        report = next(r for r in self.svc.list_reports() if r["id"] == self.r1)
        self.assertIn("Marked done by Ana in the app: used the plunger", [n["text"] for n in report["notes"]])
        with self.assertRaises(Conflict):
            self.svc.janitor_done(self.ana, self.r1)

        # The dashboard numbers see who did it, same as SMS replies.
        from backend.analysis import dataset, summarize
        self.assertEqual(summarize(dataset(self.svc))["byJanitor"]["Ana"]["handled"], 1)

    def test_done_without_taking_first(self):
        job = self.svc.janitor_done(self.ben, self.r1)
        self.assertEqual(job["doneBy"], "Ben")

    def test_report_a_problem(self):
        before = len(self.svc.sms.sent)
        r = self.svc.report_problem(self.ana, "2nd Floor · Female", ["no_tissue", "no_soap", "bogus"], "", "2F-F")
        self.assertEqual(r["title"], "No toilet paper, No soap")
        report = next(x for x in self.svc.list_reports() if x["id"] == r["id"])
        self.assertEqual((report["category"], report["priority"], report["source"]), ("supplies", "normal", "janitor"))
        self.assertEqual(report["issues"], ["no_tissue", "no_soap"])
        self.assertEqual(len(self.svc.sms.sent), before)  # not an emergency: no texts
        with self.assertRaises(BadRequest):
            self.svc.report_problem(self.ana, "2nd Floor · Female", [])
        with self.assertRaises(BadRequest):
            self.svc.report_problem(self.ana, "", ["no_soap"])

    def test_emergency_report_texts_everyone_else(self):
        self.svc.store.transact(self.svc.C_USERS, self.admin.uid,
                                lambda u: {**u, "smsPhone": "+639190000000", "smsAlerts": True})
        r = self.svc.report_problem(self.ana, "3rd Floor · Male", ["emergency"])
        self.assertEqual(r["notified"], 1)
        self.assertIn("EMERGENCY", self.svc.sms.to("+639190000000")[-1])

    def test_options_come_from_the_public_page_files(self):
        opts = self.svc.report_options()
        self.assertEqual(opts["locations"][0], {"code": "GF-M", "name": "Ground Floor · Male"})
        self.assertIn("no_soap", [i["code"] for i in opts["issues"]])
