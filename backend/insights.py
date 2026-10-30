"""AI insights: the restroom reports and live sensor readings, read by an AI model that
suggests what the maintenance team should do — and answers questions about them.

* ``run``   - a full analysis (headline, patterns, recommendations, supplies, repairs, sensor
              notes). Runs in the background (a model on the Pi can take minutes) and is saved
              in the database, so every screen and phone sees the same latest result.
* ``ask``   - a short answer to one question, from the same data.
* ``tick``  - called every minute; makes the daily analysis in the morning when switched on.

Safety: the model only reads data and writes suggestions. It never decides or sends alerts —
those are fixed rules (reports: AureoService.import_public_reports; SMS escalation: two_way).
Nothing the model writes is acted on automatically; the screens label it as AI suggestions.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from .analysis import LOCAL_TZ, build_prompt, dataset, summarize
from .arduino import ArduinoError
from .llm import LlmError, is_local, parse_json

RUN_GAP_SECONDS = 120          # one full analysis at most every 2 minutes
QUESTIONS_PER_HOUR = 30        # keeps a free API key (and the bill) safe
DAYS = 30
SYSTEM_ACTOR = {"type": "system", "uid": "insights", "username": "daily"}

ASK_SYSTEM = """You answer questions from the maintenance staff of AureoClean, a restroom hygiene \
monitoring system at STI College Caloocan (Philippines), using only the restroom reports, summary \
and sensor readings given.

Rules:
- Answer the question directly in 1–4 short sentences of plain English, then up to 4 short points.
- Use only the data given; quote counts and readings. If the data can't answer it, say so.
- You do not decide alerts or emergencies (fixed rules do that). For anything dangerous, tell them \
to follow the alert procedure and check in person.
- Return JSON only, matching the requested format."""
ASK_FORMAT = {"answer": "1–4 short sentences", "points": ["short supporting point with numbers"],
              "basedOn": "reports|sensors|both|none"}

LIMITS = {"headline": 300, "finding": 300, "evidence": 300, "action": 200, "where": 120, "when": 120,
          "reason": 300, "item": 80, "suggestion": 300, "problem": 200, "answer": 900}
SECTIONS = {
    "patterns": ("finding", "evidence", "locations"),
    "recommendations": ("action", "where", "when", "priority", "reason"),
    "supplyPlan": ("item", "where", "suggestion"),
    "facilityRepairs": ("problem", "where", "suggestion"),
    "sensorNotes": ("finding", "where", "suggestion"),
}


def _text(v, limit=300) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        v = ", ".join(str(x) for x in v)
    return " ".join(str(v).split())[:limit]


def clean_insight(answer: Optional[dict]) -> dict:
    """Keep only the expected fields, as short plain text (the model's answer is never trusted as-is)."""
    answer = answer if isinstance(answer, dict) else {}
    out = {"headline": _text(answer.get("headline"), LIMITS["headline"])}
    for section, fields in SECTIONS.items():
        items = []
        for raw in (answer.get(section) or [])[:8] if isinstance(answer.get(section), list) else []:
            if isinstance(raw, str):
                raw = {fields[0]: raw}
            if not isinstance(raw, dict):
                continue
            item = {}
            for f in fields:
                if f == "locations":
                    locs = raw.get(f) or []
                    item[f] = [_text(x, 80) for x in (locs if isinstance(locs, list) else [locs])][:5]
                elif f == "priority":
                    p = _text(raw.get(f), 10).lower()
                    item[f] = p if p in ("high", "medium", "low") else "medium"
                else:
                    item[f] = _text(raw.get(f), LIMITS.get(f, 300))
            if item.get(fields[0]):
                items.append(item)
        if section == "recommendations":
            items.sort(key=lambda r: ("high", "medium", "low").index(r["priority"]))
        out[section] = items
    dq = answer.get("dataQuality") or []
    out["dataQuality"] = [_text(x, 240) for x in (dq if isinstance(dq, list) else [dq])][:5]
    if not out["headline"] and not any(out[s] for s in SECTIONS):
        raw = _text(answer.get("raw"), 900)
        out["headline"] = raw or "The AI didn't give a usable answer this time. Try again."
    return out


def clean_answer(answer: Optional[dict], raw: str = "") -> dict:
    answer = answer if isinstance(answer, dict) else {}
    pts = answer.get("points") or []
    based = _text(answer.get("basedOn"), 12).lower()
    return {
        "answer": _text(answer.get("answer"), LIMITS["answer"]) or _text(raw, LIMITS["answer"]) or "No answer.",
        "points": [_text(p, 240) for p in (pts if isinstance(pts, list) else [pts]) if _text(p)][:4],
        "basedOn": based if based in ("reports", "sensors", "both", "none") else "",
    }


def _trend(points: list) -> dict:
    vals = [v for _, v in points]
    first, last = vals[0], vals[-1]
    spread = (max(vals) - min(vals)) or abs(last) or 1
    change = last - first
    return {"min": round(min(vals), 2), "max": round(max(vals), 2), "avg": round(sum(vals) / len(vals), 2),
            "trend": "steady" if abs(change) < spread * 0.15 else ("rising" if change > 0 else "falling")}


def sensor_context(service) -> Optional[dict]:
    """The live sensor readings, page by page, with last-6-hour stats; None when not connected."""
    arduino = getattr(service, "arduino", None)
    if arduino is None or not arduino.configured:
        return None
    try:
        data = arduino.readings()
    except ArduinoError as exc:
        return {"error": f"Sensors unavailable right now: {exc}"}
    try:
        series = arduino.history().get("series", {})
    except ArduinoError:
        series = {}
    now = time.time()
    pages = []
    for p in data.get("pages", [])[:12]:
        seen, values = set(), []
        for w in p.get("widgets", [])[:40]:
            for v in w.get("values", [])[:3]:
                if v.get("id") in seen:
                    continue
                seen.add(v.get("id"))
                value = v.get("value")
                if isinstance(value, str):
                    value = value[:80]
                elif isinstance(value, (dict, list)):
                    value = str(value)[:80]
                item = {"name": w.get("name") or v.get("label"), "value": value, "unit": v.get("unit") or None}
                try:
                    age = now - datetime.fromisoformat(str(v.get("updatedAt")).replace("Z", "+00:00")).timestamp()
                    item["minutesAgo"] = round(age / 60)
                    item["stale"] = age > 15 * 60
                except (TypeError, ValueError):
                    pass
                pts = series.get(v.get("id")) or []
                if len(pts) >= 3:
                    item["last6h"] = _trend(pts)
                values.append(item)
        if values:
            label = p["name"] if not p.get("dashboard") or p.get("dashboard") == p["name"] else f"{p['dashboard']} · {p['name']}"
            pages.append({"page": label, "values": values})
    return {"source": "Arduino IoT Cloud", "pages": pages}


class Insights:
    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self.running = False
        self.started_at: Optional[str] = None
        self.last_error: Optional[str] = None
        self._questions: deque = deque()
        self._answers: dict[str, tuple[float, dict]] = {}
        self._auto_tried: Optional[float] = None   # when the daily run was last tried (monotonic)

    # ------------------------------------------------------------------ storage
    @property
    def collection(self) -> str:
        return f"{self.service.settings.collection_prefix}Insights"

    def latest(self) -> Optional[dict]:
        try:
            return self.service.store.get(self.collection, "latest")
        except Exception:  # noqa: BLE001 - the screen just shows "no insights yet"
            return None

    def status(self) -> dict:
        return {"ai": self.service.llm.public_status(), "running": self.running, "startedAt": self.started_at,
                "lastError": self.last_error, "latest": self.latest()}

    # ------------------------------------------------------------------ data
    def _prompt(self, answer_format=None, system=None, question: str = "", max_records: int = 300) -> tuple[dict, dict]:
        records = dataset(self.service, DAYS)
        summary = summarize(records)
        sensors = sensor_context(self.service)
        s = self.service.llm.settings()
        local = s["provider"] == "openai" and is_local(s["url"])
        prompt = build_prompt(records, summary, question, max_records=min(60, max_records) if local else max_records, sensors=sensors,
                              answer_format=answer_format, system=system)
        sensor_values = sum(len(p["values"]) for p in (sensors or {}).get("pages", []))
        return prompt, {"reports": len(records), "sensorValues": sensor_values,
                        "sensorsConnected": sensors is not None and "error" not in (sensors or {})}

    # ------------------------------------------------------------------ full analysis
    def start(self, actor: dict, reason: str = "manual", force: bool = False) -> dict:
        """Start an analysis in the background; the screens poll status()."""
        if not self.service.llm.configured:
            raise LlmError("The AI isn't set up yet (Reports → Insights → AI settings).")
        with self._lock:
            if self.running:
                return {"running": True, "startedAt": self.started_at}
            last = self.latest()
            if not force and last and last.get("createdAt"):
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(last["createdAt"])).total_seconds()
                if age < RUN_GAP_SECONDS:
                    raise LlmError("Insights were made just now — wait a minute before making new ones.")
            self.running = True
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
        threading.Thread(target=self._run, args=(actor, reason), daemon=True, name="insights").start()
        return {"running": True, "startedAt": self.started_at}

    def run_now(self, actor: dict, reason: str = "manual") -> dict:
        """Run in this thread (tests, tools)."""
        with self._lock:
            self.running = True
            self.started_at = datetime.now(timezone.utc).isoformat()
            self.last_error = None
        self._run(actor, reason)
        if self.last_error:
            raise LlmError(self.last_error)
        return self.latest()

    def _run(self, actor: dict, reason: str) -> None:
        began = time.monotonic()
        try:
            prompt, counts = self._prompt()
            text = self.service.llm.ask(prompt["system"], prompt["user"], max_tokens=8192)
            parsed = parse_json(text)
            result = clean_insight(parsed if parsed is not None else {"raw": text})
            s = self.service.llm.settings()
            doc = {
                **result, **counts,
                "createdAt": datetime.now(timezone.utc).isoformat(), "seconds": round(time.monotonic() - began),
                "provider": s["provider"], "model": s["model"], "reason": reason,
                "by": actor.get("username") if actor.get("type") != "system" else "daily",
                "days": DAYS,
            }
            self.service.store.set(self.collection, "latest", doc)
            self.last_error = None
            self.service.audit("insights_made", actor, None, {"reason": reason, "reports": counts["reports"]})
            self.service.job_status["insights"] = {"at": doc["createdAt"], "error": None}
        except LlmError as exc:
            self.last_error = str(exc)
            self.service.job_status["insights"] = {"at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - shown on the Insights screen
            self.last_error = f"Couldn't make insights: {type(exc).__name__}: {exc}"[:300]
            self.service.job_status["insights"] = {"at": datetime.now(timezone.utc).isoformat(), "error": self.last_error}
        finally:
            self.running = False

    # ------------------------------------------------------------------ questions
    def ask(self, actor: dict, question: str) -> dict:
        question = " ".join((question or "").split())[:300]
        if len(question) < 3:
            raise LlmError("Type a question first.")
        now = time.monotonic()
        key = question.lower()
        hit = self._answers.get(key)
        if hit and now - hit[0] < 300:      # the same question again within 5 minutes: same answer, no wait
            return hit[1]
        while self._questions and now - self._questions[0] > 3600:
            self._questions.popleft()
        if len(self._questions) >= QUESTIONS_PER_HOUR:
            raise LlmError("That's a lot of questions this hour — try again a bit later.")
        self._questions.append(now)
        # Questions use the newest 100 reports (plus the full summary): a much smaller prompt answers faster.
        prompt, counts = self._prompt(answer_format=ASK_FORMAT, system=ASK_SYSTEM, question=question, max_records=100)
        text = self.service.llm.ask(prompt["system"], prompt["user"], max_tokens=2048, fast=True)
        parsed = parse_json(text)
        self.service.audit("insights_question", actor, None, {"question": question[:80]})
        result = {**clean_answer(parsed, "" if parsed else text), "question": question, **counts,
                  "askedAt": datetime.now(timezone.utc).isoformat()}
        if len(self._answers) > 50:
            self._answers.clear()
        self._answers[key] = (now, result)
        return result

    # ------------------------------------------------------------------ for janitors
    def today(self) -> dict:
        last = self.latest()
        if not last or not last.get("createdAt"):
            return {"available": False}
        age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last["createdAt"])).total_seconds() / 3600
        if age_h > 36:
            return {"available": False}
        tips = [{k: r.get(k, "") for k in ("action", "where", "when", "priority")}
                for r in last.get("recommendations", []) if r.get("priority") in ("high", "medium")][:3]
        return {"available": bool(tips), "createdAt": last["createdAt"], "tips": tips}

    # ------------------------------------------------------------------ daily
    def tick(self) -> bool:
        """Start the morning analysis when it's due. Returns True if one was started."""
        llm = self.service.llm
        s = llm.settings()
        if not (s["daily"] and llm.configured) or self.running or (
                self._auto_tried is not None and time.monotonic() - self._auto_tried < 1800):
            return False
        now = datetime.now(LOCAL_TZ)
        if now.hour < s["dailyHour"]:
            return False
        last = self.latest()
        if last and last.get("createdAt"):
            made = datetime.fromisoformat(last["createdAt"]).astimezone(LOCAL_TZ)
            if made.date() == now.date() and made.hour >= s["dailyHour"]:
                return False
        self._auto_tried = time.monotonic()
        try:
            self.start(SYSTEM_ACTOR, "daily", force=True)
            return True
        except LlmError:
            return False
