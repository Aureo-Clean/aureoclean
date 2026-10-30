"""AI help inside reporting (advice only):

* ``tip``    - for one report: a one-line summary, likely cause, steps, what to bring, minutes,
               safety note and who else may be needed. Made automatically for new reports (in
               the background, a few per day at most) and on request. Saved on the report as "ai"
               and shown to admins (report page) and janitors (job card).
* ``fill``   - turns "baradong inidoro sa 2nd floor male" into the report form's restroom,
               problem chips and a clean note. The person still checks and sends it.
* ``period`` - a short written summary of the reports for 7/30/90 days (Charts page and PDF).

Never: the AI doesn't set priorities, choose "emergency", send texts or change a report's
status. Emergencies stay with the fixed rules; for anything that sounds dangerous the AI only
says "follow the alert procedure / tap Emergency yourself".
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Optional

from .analysis import LOCAL_TZ, dataset, scrub, summarize
from .insights import _text, sensor_context
from .llm import LlmError, parse_json
from .report_schema import issue_map, schema

AUTO_TIPS_PER_DAY = 25        # keeps a free API key's daily limit for insights and questions
REQUESTS_PER_HOUR = 40        # tips on request + form filling + period summaries
PERIOD_CACHE_HOURS = 6

HELPERS = ("none", "maintenance", "plumber", "electrician", "security", "nurse/clinic", "supplier")

TIP_SYSTEM = """You help the janitorial team of AureoClean (restroom hygiene monitoring, STI College \
Caloocan, Philippines) handle ONE restroom problem report. You get the report, similar past reports \
from the same place, and the latest restroom sensor readings.

Rules:
- Be practical for a janitor: short sentences, plain English (Filipino terms are fine for supplies).
- Base it on the data; if unsure, say so briefly. Do not invent past events.
- You never decide alerts, priority or emergencies — fixed rules do. If anything sounds dangerous \
(fire, smoke, gas, electricity, flooding, injury), the first step is "Follow the alert procedure and \
keep people away", and say so in "safety".
- Return JSON only, matching the requested format."""
TIP_FORMAT = {"summary": "one sentence: what's wrong and where", "likelyCause": "short guess, or ''",
              "steps": ["up to 5 short steps"], "bring": ["up to 6 items/tools"], "minutes": "estimated minutes (number)",
              "safety": "one short safety note, or ''", "helper": "|".join(HELPERS),
              "pattern": "one short line if similar reports show a repeat problem, else ''"}

FILL_SYSTEM = """You turn a staff member's quick description of a restroom problem (English, Filipino or \
Taglish) into the fields of AureoClean's report form.

Rules:
- locationCode: one of the given restroom codes, or null if the place isn't clear.
- issues: codes from the given list that clearly match; never include "emergency".
- possibleEmergency: true if it could be dangerous (fire, smoke, gas, sparks/electric, flooding, \
injury, someone unwell). You do not choose emergency yourself — the person does.
- details: a clean, short English note with the specifics (stall, sink, since when); no names or numbers.
- title: up to 8 words. category: one of the given categories except "emergency".
- Return JSON only, matching the requested format."""
FILL_FORMAT = {"locationCode": "code or null", "issues": ["codes"], "details": "short note", "title": "short title",
               "category": "facility|supplies|cleanliness|system|other", "possibleEmergency": False}

PERIOD_SYSTEM = """You write a short management summary of restroom problem reports for AureoClean \
(STI College Caloocan, Philippines) for the period given, from the pre-computed summary and records.

Rules:
- Quote numbers from the summary; do not invent any. Plain, professional English.
- Emergencies were handled by fixed alert rules; don't judge or decide alerts.
- Return JSON only, matching the requested format."""
PERIOD_FORMAT = {"title": "e.g. Restroom report: last 30 days", "overview": "3-5 sentences",
                 "highlights": ["what went well, with numbers"], "concerns": ["problems, with numbers and places"],
                 "nextSteps": ["concrete actions for next period"]}


def _list(v, n, limit=160) -> list[str]:
    items = v if isinstance(v, list) else ([v] if v else [])
    return [t for t in (_text(x, limit) for x in items) if t][:n]


def clean_tip(d: Optional[dict]) -> dict:
    d = d if isinstance(d, dict) else {}
    try:
        minutes = int(float(str(d.get("minutes", "")).split()[0]))
        minutes = minutes if 1 <= minutes <= 480 else None
    except (ValueError, IndexError):
        minutes = None
    helper = _text(d.get("helper"), 20).lower()
    return {
        "summary": _text(d.get("summary"), 200), "likelyCause": _text(d.get("likelyCause"), 200),
        "steps": _list(d.get("steps"), 5), "bring": _list(d.get("bring"), 6, 60), "minutes": minutes,
        "safety": _text(d.get("safety"), 200), "helper": helper if helper in HELPERS and helper != "none" else "",
        "pattern": _text(d.get("pattern"), 200),
    }


def clean_period(d: Optional[dict]) -> dict:
    d = d if isinstance(d, dict) else {}
    return {"title": _text(d.get("title"), 120), "overview": _text(d.get("overview"), 1200),
            "highlights": _list(d.get("highlights"), 5, 240), "concerns": _list(d.get("concerns"), 5, 240),
            "nextSteps": _list(d.get("nextSteps"), 6, 240)}


class ReportAI:
    def __init__(self, service):
        self.service = service
        self._jobs: "queue.Queue[str]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._auto_day = ""
        self._auto_count = 0
        self._requests: deque = deque()
        self._lock = threading.Lock()

    @property
    def llm(self):
        return self.service.llm

    def _limit(self) -> None:
        now = time.monotonic()
        with self._lock:
            while self._requests and now - self._requests[0] > 3600:
                self._requests.popleft()
            if len(self._requests) >= REQUESTS_PER_HOUR:
                raise LlmError("The AI has been asked a lot this hour — try again a bit later.")
            self._requests.append(now)

    # ------------------------------------------------------------------ tips for one report
    def _similar(self, report_id: str, r: dict, days: int = 60) -> list[dict]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        issues = set(r.get("issues") or [])
        out = []
        for other_id, o in self.service.store.query(self.service.C_REPORTS):
            if other_id == report_id or (o.get("createdAt") or "") < since:
                continue
            same_place = r.get("location") and o.get("location") == r.get("location")
            same_issue = issues and issues & set(o.get("issues") or [])
            if same_place or (same_issue and not r.get("location")):
                out.append({"when": o.get("createdAt", "")[:10], "title": o.get("title"), "status": o.get("status"),
                            "sameProblem": bool(same_issue or o.get("title") == r.get("title")),
                            "notes": [scrub(n.get("text", ""))[:100] for n in (o.get("notes") or []) if n.get("by") != "sms"][-2:]})
        out.sort(key=lambda x: x["when"], reverse=True)
        return out[:8]

    def tip(self, report_id: str, actor: Optional[dict] = None, auto: bool = False) -> dict:
        if not self.llm.configured:
            raise LlmError("The AI isn't set up yet (Reports → AI insights).")
        if not auto:
            self._limit()
        r = self.service.store.get(self.service.C_REPORTS, report_id)
        if r is None:
            raise LlmError("Report not found.")
        labels = {c: i["label"] for c, i in issue_map().items()}
        report = {"title": r.get("title"), "category": r.get("category"), "place": r.get("location"),
                  "problems": [labels.get(c, c) for c in r.get("issues") or []], "details": scrub(r.get("details") or "")[:500],
                  "reported": r.get("createdAt"), "status": r.get("status"),
                  "notes": [scrub(n.get("text", ""))[:150] for n in (r.get("notes") or []) if n.get("by") != "sms"][-4:]}
        user = {"task": "Help the janitor handle this report.", "report": report,
                "similarPastReports": self._similar(report_id, r), "sensors": sensor_context(self.service),
                "helpers": list(HELPERS), "answerFormat": TIP_FORMAT}
        text = self.llm.ask(TIP_SYSTEM, json.dumps(user, ensure_ascii=False, default=str), max_tokens=2048, fast=True)
        tip = {**clean_tip(parse_json(text)), "at": datetime.now(timezone.utc).isoformat(),
               "model": self.llm.settings()["model"], "auto": auto}
        if not tip["summary"] and not tip["steps"]:
            raise LlmError("The AI didn't give a usable tip this time. Try again.")

        def put(current):
            if current is None:
                return None
            current["ai"] = tip
            return current
        self.service.store.transact(self.service.C_REPORTS, report_id, put)
        if actor:
            self.service.audit("report_ai_tip", actor, report_id, {})
        return tip

    # ------------------------------------------------------------------ automatic tips for new reports
    def queue_new(self, report_id: str) -> bool:
        s = self.llm.settings()
        if not (self.llm.configured and s.get("autoTips", True)):
            return False
        today = datetime.now(LOCAL_TZ).date().isoformat()
        with self._lock:
            if self._auto_day != today:
                self._auto_day, self._auto_count = today, 0
            if self._auto_count >= AUTO_TIPS_PER_DAY:
                return False
            self._auto_count += 1
        self._jobs.put(report_id)
        if getattr(self.service, "sync_notifications", False):   # tests: do it now
            self._drain()
            return True
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(target=self._loop, daemon=True, name="report-ai")
            self._worker.start()
        return True

    def _drain(self) -> None:
        while True:
            try:
                report_id = self._jobs.get_nowait()
            except queue.Empty:
                return
            self._one(report_id)

    def _loop(self) -> None:
        while True:
            try:
                report_id = self._jobs.get(timeout=60)
            except queue.Empty:
                return
            self._one(report_id)
            time.sleep(2)   # gentle on free API limits

    def _one(self, report_id: str) -> None:
        try:
            self.tip(report_id, auto=True)
            self.service.job_status["report_ai"] = {"at": datetime.now(timezone.utc).isoformat(), "error": None}
        except Exception as exc:  # noqa: BLE001 - a missing tip never blocks a report
            self.service.job_status["report_ai"] = {"at": datetime.now(timezone.utc).isoformat(),
                                                    "error": f"{type(exc).__name__}: {exc}"[:200]}

    # ------------------------------------------------------------------ filling the report form
    def fill(self, text: str, locations: list[dict], admin_form: bool = False) -> dict:
        text = " ".join((text or "").split())[:400]
        if len(text) < 4:
            raise LlmError("Describe the problem in a few words first.")
        if not self.llm.configured:
            raise LlmError("The AI isn't set up yet (Reports → AI insights).")
        self._limit()
        issues = [{"code": i["code"], "label": i["label"]} for i in schema()["issues"]]
        cats = [c for c in schema()["categories"] if c != "emergency"]
        user = {"description": scrub(text), "restrooms": [{"code": loc["code"], "name": loc["name"]} for loc in locations],
                "issueCodes": issues, "categories": cats, "answerFormat": FILL_FORMAT}
        d = parse_json(self.llm.ask(FILL_SYSTEM, json.dumps(user, ensure_ascii=False), max_tokens=1024, fast=True)) or {}
        known = issue_map()
        codes = [c for c in (d.get("issues") or []) if isinstance(c, str) and c in known and c != "emergency"]
        loc = next((x for x in locations if x["code"] == d.get("locationCode")), None)
        category = d.get("category") if d.get("category") in cats else None
        danger = bool(d.get("possibleEmergency")) or "emergency" in (d.get("issues") or [])
        return {"location": loc, "issues": list(dict.fromkeys(codes))[:6], "details": scrub(_text(d.get("details"), 300)),
                "title": _text(d.get("title"), 80), "category": category, "possibleEmergency": danger}

    # ------------------------------------------------------------------ written period summary
    @property
    def collection(self) -> str:
        return self.service.insights.collection

    def period(self, days: int, actor: dict, force: bool = False) -> dict:
        days = days if days in (7, 30, 90) else 30
        doc_id = f"period-{days}"
        old = self.service.store.get(self.collection, doc_id)
        if old and not force:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(old["createdAt"])).total_seconds()
            if age < PERIOD_CACHE_HOURS * 3600:
                return old
        if not self.llm.configured:
            raise LlmError("The AI isn't set up yet (Reports → AI insights).")
        self._limit()
        records = dataset(self.service, days)
        summary = summarize(records)
        shown = [{k: r[k] for k in ("createdAt", "category", "priority", "status", "location", "issues",
                                    "minutesToResolve", "rating")} for r in records[-200:]]
        user = {"task": f"Write the summary for the last {days} days.", "days": days, "summary": summary,
                "records": shown, "answerFormat": PERIOD_FORMAT}
        text = self.llm.ask(PERIOD_SYSTEM, json.dumps(user, ensure_ascii=False, default=str), max_tokens=6144)
        doc = {**clean_period(parse_json(text)), "days": days, "reports": len(records),
               "createdAt": datetime.now(timezone.utc).isoformat(), "model": self.llm.settings()["model"]}
        if not doc["overview"]:
            raise LlmError("The AI didn't write a usable summary this time. Try again.")
        self.service.store.set(self.collection, doc_id, doc)
        self.service.audit("period_summary", actor, None, {"days": days})
        return doc

    def period_saved(self, days: int) -> Optional[dict]:
        return self.service.store.get(self.collection, f"period-{days if days in (7, 30, 90) else 30}")
