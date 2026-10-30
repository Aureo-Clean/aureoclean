"""AI insights: providers, settings, analysis, questions, daily run — and that the AI only advises."""
import json
import os
import stat
import time
from datetime import datetime, timedelta, timezone

from backend.insights import clean_insight
from backend.llm import LlmError, parse_json, pick_model
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


GEMINI_MODELS = [   # like Google's list in late 2026: 2.5 Flash is gone
    {"name": "models/gemini-3.6-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
    {"name": "models/gemini-3.8-flash-preview", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash-lite", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
    {"name": "models/gemini-3.8-live", "supportedGenerationMethods": ["bidiGenerateContent"]},
    {"name": "models/gemini-embedding-2", "supportedGenerationMethods": ["embedContent"]},
]
CLAUDE_MODELS = [{"id": "claude-haiku-5-5", "created_at": "2026-05-01T00:00:00Z"},
                 {"id": "claude-haiku-4-5", "created_at": "2025-10-01T00:00:00Z"},
                 {"id": "claude-sonnet-5-5", "created_at": "2026-05-01T00:00:00Z"}]


class FakeAI:
    """Answers like Gemini / Claude / an OpenAI-compatible server."""

    def __init__(self, reply=None, status=200):
        self.reply = reply if reply is not None else INSIGHT
        self.status = status
        self.calls = []
        self.gemini_models = GEMINI_MODELS
        self.claude_models = CLAUDE_MODELS

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params, "get": True})
        if "generativelanguage" in url:
            return Resp(200, {"models": self.gemini_models})
        return Resp(200, {"data": self.claude_models})

    think = 0          # tokens a "thinking" model spends before answering (like Gemini 2.5+/3.x)

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if "generativelanguage" in url and json["generationConfig"]["maxOutputTokens"] <= self.think:
            return Resp(200, {"candidates": [{"content": {"parts": [{"text": ""}]}, "finishReason": "MAX_TOKENS"}]})
        if self.status != 200:
            return Resp(self.status, {"error": {"message": "nope"}})
        if "generativelanguage" in url:
            model = url.split("/models/")[1].split(":")[0]
            if model not in [m["name"].removeprefix("models/") for m in self.gemini_models]:
                return Resp(404, {"error": {"code": 404, "message": f"models/{model} is not found for API version v1beta"}})
        if "anthropic" in url and json["model"] not in [m["id"] for m in self.claude_models]:
            return Resp(404, {"error": {"type": "not_found_error", "message": f"model: {json['model']}"}})
        user = (json.get("contents") or [{}])[0].get("parts", [{}])[0].get("text") if "contents" in json else \
            (json["messages"][-1]["content"])
        self.prompts = getattr(self, "prompts", []) + [user]
        text = self.answer(user)
        if "generativelanguage" in url:
            return Resp(200, {"candidates": [{"content": {"parts": [{"text": text}]}}]})
        if "anthropic" in url:
            return Resp(200, {"content": [{"type": "text", "text": text}]})
        return Resp(200, {"choices": [{"message": {"content": text}}]})

    def answer(self, user):
        if '{"ok": true}' in user:
            text = '{"ok": true}'
        elif '"answerFormat":{"answer"' in user:
            text = '{"answer": "2nd floor male, 3 soap reports.", "points": ["3 reports", "soap 20%"], "basedOn": "both"}'
        else:
            text = "```json\n" + __import__("json").dumps(self.reply) + "\n```"
        return text


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
        self.assertIn("Connected to Google Gemini · gemini-3.6-flash — picked automatically", r["message"])
        call = self.ai.calls[-1]
        self.assertIn("/models/gemini-3.6-flash:generateContent", call["url"])
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
        for status, words in ((429, "free limit"), (500, "error (500)")):
            self.ai.status = status
            with self.assertRaises(LlmError) as cm:
                self.svc.llm.ask("s", "u")
            self.assertIn(words, str(cm.exception))

    def test_thinking_model_that_runs_out_of_room(self):
        # Gemini thinks first; with a small limit it returned no text and the setup was thrown away.
        self.ai.think = 2000
        r = self.connect()
        self.assertNotIn("warning", r)
        self.assertTrue(self.svc.llm.configured)
        budgets = [c["json"]["generationConfig"]["maxOutputTokens"] for c in self.ai.calls if c.get("json")]
        self.assertEqual(budgets[:2], [256, 16384])            # asked again with room to answer
        latest = self.svc.insights.run_now(self.admin.actor)
        self.assertEqual(latest["headline"], INSIGHT["headline"])

    def test_busy_provider_keeps_the_key(self):
        # Set up once: a hiccup during the test (free limit for now, slow internet) keeps the settings.
        self.ai.status = 429
        r = self.connect(model="gemini-3.6-flash")
        self.assertTrue(r["warning"])
        self.assertIn("Saved Google Gemini", r["message"])
        self.assertTrue(self.svc.llm.configured)
        self.assertIn("first test didn't finish", self.svc.insights_status()["lastError"])
        self.ai.status = 200
        self.assertEqual(self.svc.insights.run_now(self.admin.actor)["model"], "gemini-3.6-flash")

    def test_retired_model_switches_by_itself(self):
        # What happened on the Pi: gemini-2.5-flash was retired by Google (404).
        r = self.connect(model="gemini-2.5-flash")
        self.assertIn("gemini-3.6-flash", r["message"])
        self.assertIn("“gemini-2.5-flash” is no longer offered", r["message"])
        self.assertEqual(self.svc.llm.settings()["model"], "gemini-3.6-flash")      # saved for next time
        # Later Google retires 3.6 too: the next run moves on without anyone touching the settings.
        self.ai.gemini_models = [m for m in GEMINI_MODELS if "3.6" not in m["name"]]
        latest = self.svc.insights.run_now(self.admin.actor)
        self.assertEqual(latest["model"], "gemini-3.5-flash")
        # Nothing usable at all: a clear message, settings untouched.
        self.ai.gemini_models = [m for m in GEMINI_MODELS if "embedding" in m["name"]]
        with self.assertRaises(LlmError) as cm:
            self.svc.llm.ask("s", "u")
        self.assertIn("doesn't offer “gemini-3.5-flash” any more", str(cm.exception))
        self.assertEqual(self.svc.llm.settings()["model"], "gemini-3.5-flash")

    def test_claude_retired_model(self):
        r = self.connect("claude", "sk-ant-api03-test-abcdefghijkl", model="claude-haiku-3")
        self.assertIn("claude-haiku-5-5", r["message"])

    def test_pick_model(self):
        self.assertEqual(pick_model("gemini", GEMINI_MODELS), "gemini-3.6-flash")       # stable beats preview
        only_preview = [m for m in GEMINI_MODELS if "preview" in m["name"] or "lite" in m["name"]]
        self.assertEqual(pick_model("gemini", only_preview), "gemini-3.8-flash-preview")
        self.assertEqual(pick_model("gemini", [GEMINI_MODELS[3]]), "gemini-3.8-flash-lite")
        self.assertEqual(pick_model("gemini", []), "")
        self.assertEqual(pick_model("claude", CLAUDE_MODELS), "claude-haiku-5-5")

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

        texts = [c["json"]["contents"][0]["parts"][0]["text"] for c in self.ai.calls if c.get("json")]
        prompt = next(json.loads(t) for t in reversed(texts) if t.startswith("{") and '"records"' in t)
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


class FastAi(__import__("unittest").TestCase):
    def test_think_less_settings_per_model(self):
        from backend.llm import LlmClient
        self.assertEqual(LlmClient.thinking_config("gemini-2.5-flash"), {"thinkingBudget": 0})
        self.assertEqual(LlmClient.thinking_config("gemini-3.5-flash"), {"thinkingLevel": "low"})
        self.assertIsNone(LlmClient.thinking_config("gemini-2.0-flash"))
        self.assertIsNone(LlmClient.thinking_config("claude-haiku-5-5"))

    def test_fast_calls_send_it_and_fall_back_if_refused(self):
        import tempfile
        from pathlib import Path
        from backend.llm import LlmClient

        class R:
            def __init__(self, status, data):
                self.status_code, self._d, self.text = status, data, json.dumps(data)

            def json(self):
                return self._d

        class Http:
            def __init__(self):
                self.bodies = []

            def post(self, url, headers=None, json=None, timeout=None):
                self.bodies.append(json)
                if "thinkingConfig" in json["generationConfig"]:
                    return R(400, {"error": {"message": "Thinking level is not supported for this model."}})
                return R(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}, "finishReason": "STOP"}]})

        f = Path(tempfile.mkdtemp()) / "llm.json"
        f.write_text(json.dumps({"provider": "gemini", "apiKey": "k" * 30, "model": "gemini-3.5-flash"}))
        http = Http()
        c = LlmClient(f, http=http)
        self.assertEqual(c.ask("s", "u", fast=True), '{"ok": true}')
        self.assertEqual(len(http.bodies), 2)                      # refused once, then asked without it
        c.ask("s", "u", fast=True)
        self.assertNotIn("thinkingConfig", http.bodies[-1]["generationConfig"])   # remembered
        c.ask("s", "u", fast=False)
        self.assertNotIn("thinkingConfig", http.bodies[-1]["generationConfig"])
