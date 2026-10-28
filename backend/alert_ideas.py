"""Suggested alerts: ideas for new sensor alerts, from the last 3 days of readings.

The built-in smart alerts (backend/smart.py) cover fire/heat, bad air, cleaning, flush and
faults. This finds what else the data says is worth watching — e.g. a cubicle occupied for
20+ minutes (someone may need help), damp air that keeps coming back, a blocked ToF sensor —
and suggests it as a ready-made alert. An admin adds it with one tap (it becomes one of
"Your alerts", a fixed rule) or dismisses it. Nothing is added on its own.

* Trend rules (always): simple, explainable checks on each variable's 3-day statistics.
* AI (when set up): reads the same statistics and may suggest more; every idea is checked
  against the real variables and limits before it's shown, and can't be an emergency.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Optional

from .arduino import ArduinoError
from .llm import LlmError, parse_json

SKIP = re.compile(r"^(total|current).*|.*count$|manualflush|resetgasbaseline|resetiaqcalibration|flushthreshold|iaqaccuracy|sensorstatus", re.I)
COVERED = re.compile(r"^(temperature|iaq|gasresistance|staticiaq|co2equivalent|breathvoc|gasbaseline)$", re.I)   # smart alerts do these

SYSTEM = """You suggest extra sensor alerts for AureoClean, a smart restroom system (STI College Caloocan, \
Philippines). Built-in alerts ALREADY cover: fire/heat, hot room, bad air (IAQ / gas), cleaning due by use \
count, auto-flush not working, sensor faults and offline units — don't suggest those again.

You get each variable's statistics for the last 3 days. Suggest up to 4 alerts that would help janitors \
(safety, hygiene, faults), only when the numbers support it. Each alert: thingId, propertyId (from the list), \
op ("above"/"below" for numbers, "on"/"off" for yes/no values), limit (number, null for on/off), minutes \
(how long it must last, 0–120), level ("report" = job only, or "urgent" = text the janitors), a short title \
and why (cite the numbers). Never "emergency". Return JSON only: {"ideas": [...]}."""


def _pct(vals: list, q: float) -> float:
    s = sorted(vals)
    if not s:
        return math.nan
    i = (len(s) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def _r(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else (round(x, 1) if abs(x) < 100 else round(x))


class AlertIdeas:
    def __init__(self, service):
        self.svc = service
        self.running = False

    @property
    def collection(self) -> str:
        return f"{self.svc.settings.collection_prefix}Insights"

    def saved(self) -> dict:
        try:
            return self.svc.store.get(self.collection, "alertIdeas") or {}
        except Exception:  # noqa: BLE001
            return {}

    def view(self) -> dict:
        doc = self.saved()
        dismissed = set(doc.get("dismissed") or [])
        existing = {(r["thingId"], r["propertyId"], r["op"]) for r in self.svc.sensor_alerts.rules()}
        ideas = [i for i in doc.get("ideas") or [] if i["id"] not in dismissed
                 and (i["rule"]["thingId"], i["rule"]["propertyId"], i["rule"]["op"]) not in existing]
        return {"ideas": ideas, "createdAt": doc.get("createdAt"), "source": doc.get("source"), "note": doc.get("note", ""),
                "running": self.running, "ai": self.svc.llm.configured}

    # ------------------------------------------------------------------ data
    def stats(self) -> list[dict]:
        data = self.svc.arduino.readings()
        out = []
        for t in data.get("things", []):
            for v in t["properties"]:
                name = v["name"]
                if SKIP.match(name) or v.get("writable") and not isinstance(v.get("value"), bool):
                    continue
                val = v.get("value")
                is_bool = isinstance(val, bool) or (v.get("type") or "").upper() in ("BOOL", "STATUS")
                if not is_bool and not (isinstance(val, (int, float)) and not isinstance(val, bool)):
                    continue
                item = {"thingId": t["id"], "thing": t["name"], "propertyId": v["id"], "name": name, "label": v["label"],
                        "unit": v.get("unit") or "", "now": val, "yesNo": is_bool, "coveredByBuiltIn": bool(COVERED.match(name))}
                try:
                    pts = [p[1] for p in self.svc.arduino.series(t["id"], v["id"], 72)]
                except ArduinoError:
                    pts = []
                if pts:
                    if is_bool:
                        item["shareOn"] = round(sum(1 for x in pts if x > 0.5) / len(pts), 3)
                    else:
                        item.update(min=_r(min(pts)), p5=_r(_pct(pts, .05)), median=_r(_pct(pts, .5)),
                                    p95=_r(_pct(pts, .95)), max=_r(max(pts)), points=len(pts))
                out.append(item)
        return out

    # ------------------------------------------------------------------ trend rules
    @staticmethod
    def _idea(s: dict, op: str, limit, minutes: int, level: str, title: str, why: str, source: str = "trend") -> dict:
        return {"id": f"{s['thingId']}:{s['propertyId']}:{op}", "title": title[:90], "why": why[:300], "source": source,
                "thing": s["thing"], "label": s["label"], "unit": s["unit"],
                "rule": {"thingId": s["thingId"], "propertyId": s["propertyId"], "label": s["label"], "op": op,
                         "limit": limit, "minutes": minutes, "level": level}}

    def trend_ideas(self, stats: list[dict]) -> list[dict]:
        ideas = []
        for s in stats:
            n = s["name"].lower()
            if s["yesNo"] and re.search(r"occupied", n) and not re.search(r"urinal", s["thing"].lower()):
                ideas.append(self._idea(s, "on", None, 20, "urgent", f"{s['thing']}: cubicle occupied for 20+ minutes",
                                        "Someone inside a cubicle that long may need help (fainted, injured). Texts the janitors to check."))
            elif s["yesNo"] and re.search(r"occupied", n):
                ideas.append(self._idea(s, "on", None, 15, "report", f"{s['thing']}: someone in front for 15+ minutes",
                                        "Unusually long — the ToF sensor may be blocked, or someone may need help."))
            elif re.search(r"humid", n) and s.get("p95") is not None:
                limit = max(85, min(95, round(s["p95"] + 2)))
                if s["p95"] >= 80:
                    ideas.append(self._idea(s, "above", limit, 15, "report", f"{s['thing']}: damp air above {limit}%",
                                            f"Humidity reached {s['p95']}% or more 5% of the time in 3 days (median {s['median']}%). "
                                            "Long damp spells mean a wet floor, a leak or the exhaust fan off."))
            elif re.search(r"tof|distance", n) and s.get("p5") is not None and s.get("median") is not None:
                limit = 3 if s["unit"] in ("", "cm") else 30
                ideas.append(self._idea(s, "below", limit, 30, "report", f"{s['thing']}: ToF sensor blocked",
                                        f"The distance normally sits around {s['median']} {s['unit'] or 'cm'}. Below {limit} for 30 min "
                                        "means something covers the sensor (tape, dirt), so uses aren't counted."))
            elif re.search(r"pressure", n):
                continue
        return ideas

    # ------------------------------------------------------------------ AI
    def ai_ideas(self, stats: list[dict]) -> tuple[list[dict], str]:
        if not self.svc.llm.configured:
            return [], ""
        existing = [{"thingId": r["thingId"], "propertyId": r["propertyId"], "rule": self.svc.sensor_alerts.describe(r)}
                    for r in self.svc.sensor_alerts.rules()]
        user = (f"Variables (last 3 days):\n{json.dumps(stats, default=str)[:16000]}\n\nAlerts the admin already made:\n"
                f"{json.dumps(existing)}\n\nAnswer as JSON: {{\"ideas\": [{{\"thingId\": \"\", \"propertyId\": \"\", \"op\": \"above\", "
                f"\"limit\": 0, \"minutes\": 10, \"level\": \"report\", \"title\": \"\", \"why\": \"\"}}]}}")
        try:
            parsed = parse_json(self.svc.llm.ask(SYSTEM, user, max_tokens=6000)) or {}
        except LlmError as exc:
            return [], f"AI unavailable ({exc}); only trend ideas are shown."
        by = {(s["thingId"], s["propertyId"]): s for s in stats}
        out = []
        for x in (parsed.get("ideas") or [])[:4] if isinstance(parsed, dict) else []:
            if not isinstance(x, dict):
                continue
            s = by.get((x.get("thingId"), x.get("propertyId")))
            op, level = x.get("op"), x.get("level")
            if not s or level not in ("report", "urgent") or op not in (("on", "off") if s["yesNo"] else ("above", "below")):
                continue
            limit = None
            if not s["yesNo"]:
                try:
                    limit = float(x.get("limit"))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(limit) or abs(limit) > 1e7:
                    continue
                limit = int(limit) if limit.is_integer() else round(limit, 2)
            try:
                minutes = max(0, min(120, int(x.get("minutes") or 0)))
            except (TypeError, ValueError):
                minutes = 10
            title = " ".join(str(x.get("title") or "").split())[:90] or f"{s['label']} {op} {limit if limit is not None else ''}"
            out.append(self._idea(s, op, limit, minutes, level, title, " ".join(str(x.get("why") or "").split())[:300], "ai"))
        return out, ""

    # ------------------------------------------------------------------ making them
    def run(self, actor: Optional[dict] = None) -> dict:
        if self.running:
            return self.view()
        self.running = True
        try:
            stats = self.stats()
            trend = self.trend_ideas(stats)
            ai, note = self.ai_ideas(stats)
            seen, ideas = set(), []
            for i in ai + trend:                      # AI first; a trend idea for the same thing is dropped
                if i["id"] not in seen:
                    seen.add(i["id"])
                    ideas.append(i)
            doc = self.saved()
            doc.update(ideas=ideas[:8], createdAt=datetime.now(timezone.utc).isoformat(), note=note,
                       source="ai" if ai else "trend", dismissed=doc.get("dismissed") or [])
            self.svc.store.set(self.collection, "alertIdeas", doc)
            return self.view()
        finally:
            self.running = False

    def dismiss(self, idea_id: str) -> None:
        doc = self.saved()
        doc["dismissed"] = sorted(set(doc.get("dismissed") or []) | {idea_id})[-200:]
        self.svc.store.set(self.collection, "alertIdeas", doc)

    def find(self, idea_id: str) -> Optional[dict]:
        return next((i for i in self.saved().get("ideas") or [] if i["id"] == idea_id), None)
