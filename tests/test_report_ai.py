"""AI inside reporting: tips on reports, filling the report form, written period summaries.
Advice only — it never changes priority, category, status or sends texts."""
import json
import re

from backend.report_ai import AUTO_TIPS_PER_DAY, clean_tip
from backend.service import BadRequest
from tests.test_insights import FakeAI
from tests.test_service import PW, Base

TIP = {"summary": "Clogged toilet in 2nd stall, 2F Male.", "likelyCause": "Too much tissue",
       "steps": ["Put up the wet-floor sign", "Use the plunger", "Flush twice to check", "Mop", "Restock tissue", "Extra"],
       "bring": ["plunger", "gloves", "mop", "pail", "tissue", "sign", "too many"], "minutes": "20 min",
       "safety": "Wear gloves.", "helper": "plumber", "pattern": "3rd clog this month", "priority": "critical"}
FILL = {"locationCode": "2F-M", "issues": ["clogged_toilet", "emergency", "not_a_code", "no_tissue"],
        "details": "Second stall clogged since morning, call 0917 123 4567", "title": "Clogged toilet 2F",
        "category": "emergency", "possibleEmergency": False}
PERIOD = {"title": "Restroom report: last 30 days", "overview": "7 reports, 2 fixed.", "highlights": ["Quick fixes"],
          "concerns": ["2F Male repeats"], "nextSteps": ["Check 2F drains"]}


class ReportFake(FakeAI):
    def answer(self, user):
        fmt = re.search(r'"answerFormat":\s*\{"(\w+)"', user)
        key = fmt.group(1) if fmt else ""
        if key == "summary":
            return json.dumps(TIP)
        if key == "locationCode":
            return json.dumps(FILL)
        if key == "title":
            return json.dumps(PERIOD)
        return super().answer(user)


class ReportAIBase(Base):
    def setUp(self):
        super().setUp()
        self.svc.sync_notifications = True
        self.ai = ReportFake()
        self.svc.llm.http = self.ai
        self.svc.set_llm(self.admin, "gemini", "AIzaSyTESTKEY-1234567890", model="gemini-3.6-flash")

    def janitor(self):
        req = self.svc.submit_janitor_request("Ana Reyes", "AnaReyes", "ana@example.com", "")
        code = self.svc.approve_request(self.admin, req["requestId"])["code"]
        self.svc.activate_janitor("AnaReyes", code, PW)
        return self.svc.require_session(self.svc.login("AnaReyes", PW)["token"], "janitor")


class Tips(ReportAIBase):
    def test_new_report_gets_a_tip_but_nothing_else_changes(self):
        ana = self.janitor()
        self.svc.report_problem(self.admin, "2nd Floor · Male", ["clogged_toilet"], "older one", "2F-M")
        r = self.svc.report_problem(ana, "2nd Floor · Male", ["clogged_toilet"], "Second stall", "2F-M")
        saved = self.svc.store.get(self.svc.C_REPORTS, r["id"])
        tip = saved["ai"]
        self.assertEqual(tip["summary"], TIP["summary"])
        self.assertEqual(len(tip["steps"]), 5)
        self.assertEqual(len(tip["bring"]), 6)
        self.assertEqual(tip["minutes"], 20)
        self.assertEqual(tip["helper"], "plumber")
        self.assertTrue(tip["auto"])
        self.assertNotIn("priority", tip)                       # the AI's "priority" is thrown away
        self.assertEqual((saved["priority"], saved["status"], saved["category"]), ("normal", "open", "facility"))
        # The similar older report went into the prompt (same restroom).
        prompt = json.loads(self.ai.prompts[-1])
        self.assertEqual(prompt["similarPastReports"][0]["title"], "Clogged toilet")
        # Janitors see it on the job card.
        job = next(j for j in self.svc.janitor_jobs(ana)["open"] if j["id"] == r["id"])
        self.assertEqual(job["ai"]["bring"][0], "plunger")

    def test_switch_off_and_daily_cap(self):
        self.svc.set_insights_daily(self.admin, auto_tips=False)
        r = self.svc.report_problem(self.admin, "Ground Floor · Female", ["no_soap"], "", "GF-F")
        self.assertNotIn("ai", self.svc.store.get(self.svc.C_REPORTS, r["id"]))
        self.svc.set_insights_daily(self.admin, auto_tips=True)
        self.svc.report_ai._auto_count = AUTO_TIPS_PER_DAY
        from datetime import datetime
        from backend.analysis import LOCAL_TZ
        self.svc.report_ai._auto_day = datetime.now(LOCAL_TZ).date().isoformat()
        r = self.svc.report_problem(self.admin, "Ground Floor · Female", ["no_soap"], "", "GF-F")
        self.assertNotIn("ai", self.svc.store.get(self.svc.C_REPORTS, r["id"]))
        # …but asking for one still works.
        self.assertEqual(self.svc.report_tip(self.admin, r["id"])["summary"], TIP["summary"])

    def test_ai_trouble_never_blocks_a_report(self):
        self.ai.status = 500
        r = self.svc.report_problem(self.admin, "Ground Floor · Female", ["no_soap"], "", "GF-F")
        self.assertIn("sent", r["message"])
        self.assertIn("error", self.svc.job_status["report_ai"]["error"].lower())
        with self.assertRaises(BadRequest):
            self.svc.report_tip(self.admin, r["id"])

    def test_clean_tip(self):
        self.assertEqual(clean_tip({"minutes": "9999", "helper": "wizard"})["minutes"], None)
        self.assertEqual(clean_tip(None)["steps"], [])


class FillForm(ReportAIBase):
    def test_fill_never_picks_emergency(self):
        f = self.svc.report_fill(self.admin, "baradong inidoro sa 2nd floor male, tawagan 09171234567")
        self.assertEqual(f["location"]["code"], "2F-M")
        self.assertEqual(f["issues"], ["clogged_toilet", "no_tissue"])         # unknown + emergency dropped
        self.assertTrue(f["possibleEmergency"])                                # …and flagged for the person to decide
        self.assertIsNone(f["category"])                                       # "emergency" isn't a suggestion
        self.assertNotIn("4567", f["details"])
        prompt = json.loads(self.ai.prompts[-1])
        self.assertNotIn("1234567", prompt["description"])                     # scrubbed before it left the Pi
        with self.assertRaises(BadRequest):
            self.svc.report_fill(self.admin, "x")


class Period(ReportAIBase):
    def test_period_summary_is_saved_and_reused(self):
        self.svc.report_problem(self.admin, "Ground Floor · Female", ["no_soap"], "", "GF-F")
        self.assertIsNone(self.svc.period_summary_saved(30)["summary"])
        doc = self.svc.period_summary(self.admin, 30)
        self.assertEqual(doc["overview"], PERIOD["overview"])
        n = len(self.ai.calls)
        self.assertEqual(self.svc.period_summary(self.admin, 30)["createdAt"], doc["createdAt"])   # cached
        self.assertEqual(len(self.ai.calls), n)
        self.svc.period_summary(self.admin, 30, force=True)
        self.assertEqual(len(self.ai.calls), n + 1)
        self.assertEqual(self.svc.period_summary_saved(30)["summary"]["title"], PERIOD["title"])
