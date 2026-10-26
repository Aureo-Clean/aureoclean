"""AI insights: providers, settings, analysis, questions, daily run — and that the AI only advises."""
import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone

from backend.insights import clean_insight
from backend.llm import LlmError, parse_json
from backend.service import BadRequest, Forbidden
from tests.test_arduino import FakeArduino
from tests.test_service import PW, Base

INSIGHT = {
    "headline": "Restrooms are mostly fine; 2nd floor male needs soap more often.",
    "patterns": [{"finding": "Soap runs out on 2F male", "evidence": "3 reports", "locations": ["2nd Floor · Male"]}],
    "recommendations": [
        {"action": "Refill soap", "where": "2F Male", "when": "before 10:00", "priority": "LOW", "reason": "x" * 900},
        {"action": "Check the flush", "where": "GF Female", "when": "today", "priority": "high", "reason": "2 reports"},
        {"action": "Mop", "where": "3F", "when": "noon", "priority": "urgent!!", "reason": "wet floor"},
        "Restock tissue",
    ],
    "supplyPlan": [{"item": "soap", "where": "2F", "suggestion": "add a second bottle"}],
    "facilityRepairs": [], "sensorNotes": [{"finding": "Water level falling", "where": "Urinal", "suggestion": "check tank"}],
    "dataQuality": ["few ratings"], "sendAlert": True, "emergency": "declare now",
}


class Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeAI:
    """Answers like Gemini / Claude / an OpenAI-compatible server."""

    def __init__(self, reply=None, status=200):
        self.reply = reply if reply is not None else INSIGHT
        self.status = status
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if self.status != 200:
            return Resp(self.status, {"error": {"message": "nope"}})
        user = (json.get("contents") or [{}])[0].get("parts", [{}])[0].get("text") if "contents" in json else \
            (json["messages"][-1]["content"])
        if '{"ok": true}' in user:
            text = '{"ok": true}'
        elif '"answerFormat":{"answer"' in user:
            text = '{"answer": "2nd floor male, 3 soap reports.", "points": ["3 reports", "soap 20%"], "basedOn": "both"}'
        else:
            text = "```json\n" + __import__("json").dumps(self.reply) + "\n```"
        if "generativelanguage" in url:
            return Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})
        if "anthropic" in url:
            return Resp(200, {"content": [{"type": "text", "text": text}]})
        return Resp(200, {"choices": [{"message": {"content": text}}]})


class InsightsBase(Base):
    def setUp(self):
        super().setUp()
        self.ai = FakeAI()
        self.svc.llm.http = self.ai

    def connect(self, provider="gemini", key="AIzaSyTESTKEY-1234567890", **kw):
        return self.svc.set_llm(self.admin, provider, key, **kw)


class Providers(InsightsBase):
    def test_gemini_request(self):
        r = self.connect()
        self.assertIn("Connected to Google Gemini · gemini-2.5-flash", r["message"])
        call = self.ai.calls[-1]
        self.assertIn("/models/gemini-2.5-flash:generateContent", call["url"])
        self.assertEqual(call["headers"]["x-goog-api-key"], "AIzaSyTESTKEY-1234567890")
        self.assertEqual(call["json"]["generationConfig"]["responseMimeType"], "application/json")
        # The key is never shown back, and the file is private.
        st = self.svc.insights_status()["ai"]
        self.assertNotIn("1234567890", json.dumps(st))
        self.assertEqual(st["keyHint"], "…7890")
        self.assertEqual(stat.S_IMODE(os.stat(self.svc.llm.file).st_mode), 0o600)

    def test_claude_request(self):
        self.connect("claude", "sk-ant-api03-test-abcdefghijkl")
        call = self.ai.calls[-1]
        self.assertEqual(call["url"], "https://api.anthropic.com/v1/messages")
        self.assertEqual(call["headers"]["anthropic-version"], "2023-06-01")
        self.assertEqual(call["json"]["model"], "claude-haiku-5-5")
        self.assertIn("system", call["json"])

    def test_local_ollama_needs_no_key_and_waits_longer(self):
        self.connect("openai", "", model="llama3.2:3b", url="http://127.0.0.1:11434/v1/")
        call = self.ai.calls[-1]
        self.assertEqual(call["url"], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertNotIn("Authorization", call["headers"])
        self.assertEqual(call["timeout"], 600)

    def test_bad_key_keeps_old_settings(self):
        self.connect()
        self.ai.status = 401
        with self.assertRaises(BadRequest) as cm:
            self.connect("claude", "sk-ant-wrong-key-123456")
        self.assertIn("refused the API key", str(cm.exception))
        self.assertEqual(self.svc.llm.settings()["provider"], "gemini")    # still the working one
        self.ai.status = 401
        self.svc.clear_llm(self.admin)
        with self.assertRaises(BadRequest):
            self.connect()
        self.assertFalse(self.svc.llm.configured)                         # nothing half-saved

    def test_friendly_errors(self):
        self.connect()
        for status, words in ((429, "free limit"), (404, "model name"), (500, "error (500)")):
            self.ai.status = status
            with self.assertRaises(LlmError) as cm:
                self.svc.llm.ask("s", "u")
            self.assertIn(words, str(cm.exception))

    def test_validation(self):
        with self.assertRaises(BadRequest):
            self.connect("gemini", "short")
        with self.assertRaises(BadRequest):
            self.connect("skynet", "AIzaSyTESTKEY-1234567890")
        with self.assertRaises(BadRequest):
            self.connect("openai", "", model="x y z")


class Analysis(InsightsBase):
    def test_run_saves_cleaned_insights_with_sensors(self):
        self.svc.arduino.http = FakeArduino()
        self.svc.arduino.save("client-id-123", "s3cret-secret")
        self.connect()
        self.svc.report_problem(self.admin, "2nd Floor · Male", ["no_soap"], "Text me 0917 123 4567 or a@b.com", "2F-M")
        reports_before = self.svc.store.query(self.svc.C_REPORTS)
        latest = self.svc.insights.run_now(self.admin.actor)

        prompt = json.loads(self.ai.calls[-1]["json"]["contents"][0]["parts"][0]["text"])
        self.assertIn("sensors", prompt)
        self.assertEqual(prompt["sensors"]["pages"][0]["page"], "Restroom 2F Male")
        self.assertEqual({v["name"] for v in prompt["sensors"]["pages"][0]["values"]}, {"Water level", "Smoke detected", "Temperature"})
        details = prompt["records"][-1]["details"]
        self.assertNotIn("123 4567", details)
        self.assertIn("[number hidden]", details)
        self.assertIn("[email hidden]", details)

        self.assertEqual(latest["reports"], 1)
        self.assertEqual(latest["sensorValues"], 3)
        self.assertEqual([r["priority"] for r in latest["recommendations"]], ["high", "medium", "medium", "low"])
        self.assertEqual(latest["recommendations"][2]["action"], "Restock tissue")   # plain string accepted
        self.assertEqual(len(latest["recommendations"][3]["reason"]), 300)           # trimmed
        self.assertNotIn("sendAlert", latest)                                         # unknown fields dropped
        self.assertNotIn("emergency", latest)
        self.assertEqual(self.svc.insights_status()["latest"]["headline"], INSIGHT["headline"])
        # Advice only: no report was created or changed, no alert went out.
        self.assertEqual(self.svc.store.query(self.svc.C_REPORTS), reports_before)

    def test_not_set_up_and_rate_limit(self):
        with self.assertRaises(BadRequest):
            self.svc.start_insights(self.admin)
        self.connect()
        self.svc.insights.run_now(self.admin.actor)
        with self.assertRaises(BadRequest) as cm:
            self.svc.start_insights(self.admin)
        self.assertIn("just now", str(cm.exception))

    def test_background_run_and_error_shown(self):
        self.connect()
        self.ai.status = 429
        self.svc.start_insights(self.admin)
        for _ in range(100):
            if not self.svc.insights.running:
                break
            time.sleep(0.02)
        st = self.svc.insights_status()
        self.assertFalse(st["running"])
        self.assertIn("free limit", st["lastError"])
        self.assertIsNone(st["latest"])

    def test_garbage_answer_is_handled(self):
        self.connect()
        self.ai.reply = {"nothing": "useful"}
        latest = self.svc.insights.run_now(self.admin.actor)
        self.assertIn("didn't give a usable answer", latest["headline"])
        self.assertIsNone(parse_json("no json here"))
        self.assertEqual(parse_json('Sure! {"a": 1} hope that helps'), {"a": 1})
        self.assertEqual(clean_insight(None)["recommendations"], [])

    def test_questions(self):
        self.connect()
        r = self.svc.ask_insights(self.admin, "  Which restroom needs   soap? ")
        self.assertEqual(r["question"], "Which restroom needs soap?")
        self.assertEqual(r["basedOn"], "both")
        self.assertEqual(len(r["points"]), 2)
        with self.assertRaises(BadRequest):
            self.svc.ask_insights(self.admin, "?")
        self.svc.insights._questions.extend([time.monotonic()] * 40)
        with self.assertRaises(BadRequest) as cm:
            self.svc.ask_insights(self.admin, "Anything else?")
        self.assertIn("a lot of questions", str(cm.exception))


class ForJanitors(InsightsBase):
    def janitor(self):
        req = self.svc.submit_janitor_request("Ana Reyes", "AnaReyes", "ana@example.com", "")
        code = self.svc.approve_request(self.admin, req["requestId"])["code"]
        self.svc.activate_janitor("AnaReyes", code, PW)
        return self.svc.require_session(self.svc.login("AnaReyes", PW)["token"], "janitor")

    def test_tips_and_permissions(self):
        self.assertEqual(self.svc.insights_today(), {"available": False})
        self.connect()
        self.svc.insights.run_now(self.admin.actor)
        tips = self.svc.insights_today()
        self.assertTrue(tips["available"])
        self.assertEqual([t["action"] for t in tips["tips"]], ["Check the flush", "Mop", "Restock tissue"])  # no low ones
        self.janitor()
        with self.assertRaises(Forbidden):
            self.svc.require_session(self.svc.login("AnaReyes", PW)["token"], "admin")   # admin routes stay admin-only
        old = self.svc.insights.latest()
        old["createdAt"] = (datetime.now(timezone.utc) - timedelta(hours=40)).isoformat()
        self.svc.store.set(self.svc.insights.collection, "latest", old)
        self.assertEqual(self.svc.insights_today(), {"available": False})               # stale tips hidden


class Daily(InsightsBase):
    def test_morning_run_once_a_day(self):
        self.connect()
        self.svc.set_insights_daily(self.admin, True, 0)
        self.svc.insights._auto_tried = None
        self.svc.store.delete(self.svc.insights.collection, "latest")
        self.assertTrue(self.svc.insights.tick())
        for _ in range(100):
            if not self.svc.insights.running:
                break
            time.sleep(0.02)
        self.assertEqual(self.svc.insights.latest()["by"], "daily")
        self.svc.insights._auto_tried = None
        self.assertFalse(self.svc.insights.tick())             # already made today
        self.svc.set_insights_daily(self.admin, False)
        self.svc.store.delete(self.svc.insights.collection, "latest")
        self.svc.insights._auto_tried = None
        self.assertFalse(self.svc.insights.tick())             # switched off
