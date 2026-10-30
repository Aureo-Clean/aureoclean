"""Daily advice on the Home screen: a plan for the janitors and advice for the admins.

Made every morning when quiet hours end (5 AM), and on request. It reads the patterns the
smart monitor learned (busiest hours per unit, hours when the air is usually bad, uses since
cleaning, faults) plus the open jobs and the last 7 days of reports.

* With the AI set up, the AI writes it (and may suggest better smart settings, which an admin
  applies with one tap — nothing changes on its own).
* Without the AI (or if it fails), the same facts are turned into advice by fixed rules, so the
  card is never empty.

Advice only: alerts and texts are always the smart monitor's fixed rules.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from .analysis import LOCAL_TZ, dataset, scrub, summarize
from .llm import LlmError, parse_json
from .smart import SETTINGS, _hour

SYSTEM = """You plan the day for the cleaning team of AureoClean, a smart restroom system at STI College \
Caloocan (Philippines). Each restroom unit (urinal or toilet) has an ESP32 with a ToF sensor that counts \
uses, a BME680 air sensor (Bosch BSEC IAQ: 0–50 excellent, 51–100 good, 101–150 fair, 151–200 bad, \
above 200 very bad) and an auto-flush actuator.

From the facts given, write:
- "janitor": the janitors' plan for today, in time order: when, where, what to do and why (cite the \
numbers: busiest hours, uses, air). Plan cleanings BEFORE the busy hours and before the air usually gets bad. \
Max 6 items, each task under 14 words, plain simple English.
- "admin": up to 5 points for the administrators: patterns, repairs (flush, sensors, offline units), \
supplies, staffing at peak hours. Cite numbers.
- "suggestions": optional, up to 2 changes to the smart settings when the data clearly supports it \
(e.g. cleaning due every N uses). Only use the setting names given, with values inside the allowed range.
- "headline": one sentence for the day.

Rules: use only the data given; never invent sensors or numbers; times are Philippine time; you do not decide \
alerts or emergencies (fixed rules do that). If there is little data, say so and keep it short. Return JSON only."""
FORMAT = {
    "headline": "one sentence",
    "janitor": [{"time": "e.g. 10:45 AM or Now", "where": "unit / restroom", "task": "what to do", "why": "short reason with numbers",
                 "priority": "high|medium|low"}],
    "admin": [{"title": "short", "detail": "1–2 sentences with numbers", "priority": "high|medium|low"}],
    "suggestions": [{"setting": "one of the setting names", "value": 0, "why": "short reason"}],
}
REFRESH_GAP = 600
TUNABLE = ("cleanEvery", "hotTemp", "iaqBad", "airJobMinutes", "airSmsMinutes", "flushMissUses")


def _t(v, n=200) -> str:
    return " ".join(str(v or "").split())[:n]


def clean(answer: Optional[dict], current: dict) -> dict:
    a = answer if isinstance(answer, dict) else {}
    pr = lambda p: p if p in ("high", "medium", "low") else "medium"  # noqa: E731
    jan = [{"time": _t(x.get("time"), 20), "where": _t(x.get("where"), 60), "task": _t(x.get("task"), 140),
            "why": _t(x.get("why"), 200), "priority": pr(_t(x.get("priority"), 8).lower())}
           for x in (a.get("janitor") or [])[:6] if isinstance(x, dict) and _t(x.get("task"))]
    adm = [{"title": _t(x.get("title"), 90), "detail": _t(x.get("detail"), 300), "priority": pr(_t(x.get("priority"), 8).lower())}
           for x in (a.get("admin") or [])[:5] if isinstance(x, dict) and _t(x.get("title"))]
    sug = []
    for x in (a.get("suggestions") or [])[:2]:
        if not isinstance(x, dict) or x.get("setting") not in TUNABLE:
            continue
        lo, hi, typ, _, label = SETTINGS[x["setting"]]
        try:
            v = typ(float(x.get("value")))
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi and v != current.get(x["setting"]):
            sug.append({"setting": x["setting"], "label": label, "value": v, "current": current.get(x["setting"]),
                        "why": _t(x.get("why"), 240)})
    return {"headline": _t(a.get("headline"), 240), "janitor": jan, "admin": adm, "suggestions": sug}


class Advice:
    def __init__(self, service):
        self.svc = service
        self.running = False
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._tried_day: Optional[str] = None

    @property
    def collection(self) -> str:
        return f"{self.svc.settings.collection_prefix}Insights"

    def saved(self) -> Optional[dict]:
        try:
            return self.svc.store.get(self.collection, "advice")
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ facts
    def facts(self) -> dict:
        smart = self.svc.smart
        cfg = smart.config()
        now = datetime.fromtimestamp(self.svc.smart.clock(), LOCAL_TZ)
        records = dataset(self.svc, 7)
        summary = summarize(records)
        open_jobs = [{"title": scrub(r.get("title", ""))[:80], "where": r.get("location") or "", "priority": r.get("priority"),
                      "status": r.get("status"), "source": r.get("source")}
                     for r in self.svc.list_reports() if r.get("status") != "resolved"][:15]
        return {
            "now": now.strftime("%A %Y-%m-%d %I:%M %p"),
            "quietHours": smart.quiet_text(cfg),
            "units": smart.patterns(),
            "openJobs": open_jobs,
            "reportsLast7Days": {k: summary.get(k) for k in ("totalReports", "byLocation", "byIssue", "byHour", "medianMinutesToResolve")
                                 if k in summary},
            "settings": {k: {"value": cfg[k], "min": SETTINGS[k][0], "max": SETTINGS[k][1], "meaning": SETTINGS[k][4]} for k in TUNABLE},
        }

    # ------------------------------------------------------------------ fixed-rule advice (no AI)
    def rules(self, f: dict) -> dict:
        now = datetime.fromtimestamp(self.svc.smart.clock(), LOCAL_TZ)
        jan, adm, sug = [], [], []
        cfg = self.svc.smart.config()
        for u in f["units"]:
            where = u["location"] if u["location"] == u["unit"] else f"{u['unit']} · {u['location']}"
            for title in u["openProblems"][:2]:
                jan.append({"time": "Now", "where": where, "task": title, "why": "Open smart alert.", "priority": "high"})
            since, every = u.get("usesSinceCleaned"), u.get("cleanEvery") or 0
            if since is not None and every and since >= every * 0.75 and not any("Cleaning due" in t for t in u["openProblems"]):
                jan.append({"time": "Soon", "where": where, "task": f"Clean the {u['kind'] if u['kind'] != 'other' else 'unit'}",
                            "why": f"{since} of {every} uses since the last cleaning.", "priority": "medium"})
            for b in u["busiestHours"][:2]:
                if b["hour"] > now.hour:
                    jan.append({"time": f"{_hour(b['hour'] - 1)[:-3]}:45 {_hour(b['hour'] - 1)[-2:]}", "where": where,
                                "task": "Clean and check supplies before the rush",
                                "why": f"Busiest at {b['label']} (about {round(b['avgUses'])} uses).", "priority": "medium"})
            for a in u["airUsuallyBadAt"][:1]:
                if a["hour"] >= now.hour:
                    jan.append({"time": f"{_hour(a['hour'] - 1)[:-3]}:30 {_hour(a['hour'] - 1)[-2:]}", "where": where,
                                "task": "Flush, clean and air out", "why": f"Air is usually bad around {a['label']} (IAQ ~{a['avgIaq']}).",
                                "priority": "medium"})
            if not u.get("online"):
                adm.append({"title": f"{u['unit']} is offline", "detail": "No data from it right now — check its power and Wi-Fi.", "priority": "high"})
            elif u.get("iaqCalibrating"):
                adm.append({"title": f"{u['unit']}: air sensor still calibrating",
                            "detail": "BSEC IAQ needs a few hours to a few days to reach accuracy 3; until then bad air is judged from the raw gas reading.",
                            "priority": "low"})
            if u.get("busiestHours"):
                b = u["busiestHours"][0]
                adm.append({"title": f"{u['unit']}: busiest at {b['label']}",
                            "detail": f"About {round(b['avgUses'])} uses in that hour on average; {u.get('usesPerDay') or 0} uses a day.",
                            "priority": "low"})
            per_day = u.get("usesPerDay") or 0
            if per_day and every and u.get("daysOfData", 0) >= 3 and per_day / every > 8:
                value = int(min(SETTINGS["cleanEvery"][1], round(per_day / 6 / 10) * 10 or 10))
                if value != cfg["cleanEvery"]:
                    sug.append({"setting": "cleanEvery", "label": SETTINGS["cleanEvery"][4], "value": value, "current": cfg["cleanEvery"],
                                "why": f"{u['unit']} is used about {per_day} times a day, so cleaning every {every} uses means "
                                       f"{round(per_day / every)} cleanings a day."})
        if not f["units"]:
            adm.append({"title": "No smart units yet", "detail": "Connect Arduino Cloud; units with the AureoClean variables show up here.", "priority": "low"})
        rep = f["reportsLast7Days"]
        if rep.get("totalReports"):
            top = sorted((rep.get("byLocation") or {}).items(), key=lambda kv: -kv[1])[:1] if isinstance(rep.get("byLocation"), dict) else []
            adm.append({"title": f"{rep['totalReports']} reports in the last 7 days",
                        "detail": (f"Most from {top[0][0]} ({top[0][1]})." if top else "") +
                                  (f" Median time to fix: {rep['medianMinutesToResolve']} min." if rep.get("medianMinutesToResolve") is not None else ""),
                        "priority": "low"})
        order = {"Now": 0, "Soon": 1}
        jan.sort(key=lambda x: (order.get(x["time"], 2), _clock(x["time"])))
        if not jan:
            jan.append({"time": "Today", "where": "All restrooms", "task": "Do the normal rounds",
                        "why": "Nothing unusual from the sensors. Advice gets better after a few days of data.", "priority": "low"})
        busy = sum(len(u["openProblems"]) for u in f["units"])
        headline = (f"{busy} smart alert{'s' if busy != 1 else ''} open — start there." if busy else
                    "All units look normal. Plan cleanings before the busy hours.")
        return {"headline": headline, "janitor": jan[:6], "admin": adm[:5], "suggestions": sug[:2]}

    # ------------------------------------------------------------------ making it
    def run(self, actor: dict, reason: str = "manual") -> dict:
        with self._lock:
            if self.running:
                raise LlmError("The advice is being made right now.")
            self.running = True
        began = time.monotonic()
        try:
            f = self.facts()
            cfg = self.svc.smart.config()
            source, note = "rules", ""
            result = None
            if self.svc.llm.configured:
                try:
                    user = (f"Facts (JSON):\n{json.dumps(f, ensure_ascii=False, default=str)[:24000]}\n\n"
                            f"Answer with JSON in this format:\n{json.dumps(FORMAT)}")
                    text = self.svc.llm.ask(SYSTEM, user, max_tokens=4096, fast=True)
                    result = clean(parse_json(text), cfg)
                    if not result["janitor"] and not result["admin"]:
                        result, note = None, "The AI gave no usable advice, so fixed rules were used."
                    else:
                        source = "ai"
                except LlmError as exc:
                    note = f"AI unavailable ({exc}); fixed rules were used."
            if result is None:
                result = self.rules(f)
            doc = {**result, "source": source, "note": note, "reason": reason, "seconds": round(time.monotonic() - began),
                   "createdAt": datetime.now(timezone.utc).isoformat(), "units": len(f["units"]),
                   "by": actor.get("username") if actor.get("type") != "system" else "daily"}
            self.svc.store.set(self.collection, "advice", doc)
            self.last_error = None
            self.svc.job_status["advice"] = {"at": doc["createdAt"], "error": None}
            return doc
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            self.svc.job_status["advice"] = {"at": datetime.now(timezone.utc).isoformat(), "error": self.last_error}
            raise
        finally:
            self.running = False

    def start(self, actor: dict, reason: str = "manual") -> None:
        threading.Thread(target=lambda: self._quiet_run(actor, reason), daemon=True, name="advice").start()

    def _quiet_run(self, actor, reason):
        try:
            self.run(actor, reason)
        except Exception:  # noqa: BLE001 - kept in last_error / job_status
            pass
        if reason == "daily" and self.svc.arduino.configured:
            try:
                self.svc.alert_ideas.run(actor)   # new alert suggestions with the day's advice
            except Exception:  # noqa: BLE001
                pass

    def today(self, role: str) -> dict:
        """The day's advice for this role. Older than 20 hours (or none yet) → fixed-rule advice made now."""
        doc = self.saved()
        fresh = False
        if doc and doc.get("createdAt"):
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(doc["createdAt"])).total_seconds()
            fresh = age < 20 * 3600
        if not fresh:
            f = self.facts()
            doc = {**self.rules(f), "source": "rules", "note": "", "createdAt": datetime.now(timezone.utc).isoformat(), "live": True}
        out = {k: doc.get(k) for k in ("headline", "janitor", "source", "note", "createdAt")}
        out["running"] = self.running
        if role == "admin":
            out["admin"] = doc.get("admin") or []
            out["suggestions"] = doc.get("suggestions") or []
            out["ai"] = self.svc.llm.configured
        return out

    def tick(self) -> bool:
        """Once a day, when quiet hours end (5 AM): make the day's advice (AI when set up)."""
        cfg = self.svc.smart.config()
        now = datetime.fromtimestamp(self.svc.smart.clock(), LOCAL_TZ)
        day = now.strftime("%Y-%m-%d")
        if self.running or self._tried_day == day or now.hour < cfg["quietEnd"]:
            return False
        doc = self.saved()
        if doc and doc.get("createdAt") and not doc.get("live"):
            made = datetime.fromisoformat(doc["createdAt"]).astimezone(LOCAL_TZ)
            if made.strftime("%Y-%m-%d") == day and made.hour >= cfg["quietEnd"]:
                self._tried_day = day
                return False
        self._tried_day = day
        self.start({"type": "system", "username": "daily"}, "daily")
        return True


def _clock(text: str) -> int:
    """'10:45 AM' → minutes since midnight (for sorting the plan)."""
    try:
        t = datetime.strptime(text.strip().upper(), "%I:%M %p")
        return t.hour * 60 + t.minute
    except ValueError:
        return 9999
