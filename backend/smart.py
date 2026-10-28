"""Smart monitoring: the trend engine that watches every ESP32 unit and turns trends into jobs.

Works on its own (no AI, no setup beyond picking the restroom of each unit) and runs on
the Pi every 5 seconds, so it keeps working without internet to the AI. For each Arduino
Thing that has the AureoClean variables (temperature, iaq, iaqAccuracy, gasResistance,
sensorStatus, totalUses/totalToFUsers, totalFlushes …) it looks for:

* Fire / heat   — very hot for 30 s, temperature rising fast, or heat + bad air together
                  → EMERGENCY: texts everyone active today, at any hour.
* Hot room      — above 35 °C for 10 min → a job; still hot after 20 min → texts the janitors.
* Bad air       — BSEC IAQ above 150 (or, while IAQ is still calibrating, the raw gas reading
                  far below this hour's normal) for 10 min → a job; 20 min → texts the janitors.
                  The job says whether it came after heavy use or while the unit was barely used.
* Cleaning due  — uses counted by the ToF sensor since the last cleaning → a job.
* Auto-flush    — uses keep coming but the flush count doesn't move → a "broken flush" job.
* Sensor faults — BME680 error status, or a unit that stopped sending → a job for the admin.

Tiers: 1 = a job in the app (no text), 2 = text the janitors (held until 5 AM during quiet
hours, 7 PM–5 AM), 3 = emergency (everyone active today, immediately). One report per
problem; it can move up a tier, never down. Back to normal for 2 minutes → a note. Marked done
but still happening → a new report after 30 minutes.

It also learns each unit's normal per hour of the day (air, temperature) and counts uses per
hour; the daily advice (backend/advice.py) uses those patterns.

Settings: ~/.config/aureoclean/smart.json · learned data: smart_state.json (same folder).
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Callable, Optional

from .analysis import LOCAL_TZ
from .security import iso, utcnow

log = logging.getLogger("aureoclean.smart")

CHECK_SECONDS = 5
SAVE_SECONDS = 60
WINDOW_SECONDS = 45 * 60      # recent readings kept in memory
CLEAR_SECONDS = 120           # back to normal this long → "back to normal" note
AFTER_DONE_SECONDS = 30 * 60  # marked done but still happening → wait before a new report
KEEP_DAYS = 14

# Settings: name → (min, max, type, default, label)
SETTINGS = {
    "quietStart": (0, 23, int, 19, "Quiet hours start (hour)"),
    "quietEnd": (0, 23, int, 5, "Quiet hours end (hour)"),
    "fireTemp": (40, 90, float, 50, "Emergency: temperature at or above (°C)"),
    "fireSeconds": (10, 300, int, 30, "…for at least (seconds)"),
    "riseDegrees": (3, 30, float, 8, "Emergency: rises this much within 2 minutes (°C)"),
    "riseMinTemp": (30, 80, float, 38, "…and is at least (°C)"),
    "hotTemp": (28, 60, float, 35, "Hot room: above (°C)"),
    "hotMinutes": (1, 120, int, 10, "…for (minutes) → job; twice as long → text janitors"),
    "iaqBad": (51, 400, int, 150, "Bad air: IAQ above"),
    "iaqVeryBad": (100, 500, int, 250, "Very bad air: IAQ above (texts sooner)"),
    "airJobMinutes": (1, 120, int, 10, "Bad air for (minutes) → job"),
    "airSmsMinutes": (2, 240, int, 20, "Bad air for (minutes) → text janitors"),
    "gasDropPercent": (10, 90, int, 40, "While IAQ calibrates: gas reading this % below normal"),
    "cleanEvery": (10, 2000, int, 120, "Cleaning due after (uses)"),
    "flushMissUses": (2, 50, int, 5, "Broken flush: uses without a flush"),
    "offlineMinutes": (3, 240, int, 10, "Unit offline after (minutes without data)"),
}
DEFAULTS = {k: v[3] for k, v in SETTINGS.items()}

# Arduino variable names (lower case) → what they are
ALIASES = {
    "temperature": ("temperature", "temp"),
    "humidity": ("humidity",),
    "iaq": ("iaq",),
    "accuracy": ("iaqaccuracy",),
    "gas": ("gasresistance",),
    "status": ("sensorstatus",),
    "uses": ("totaltofusers", "totaluses", "usecount", "uses"),
    "flushes": ("totalflushes", "flushcount"),
    "flushThreshold": ("flushthreshold",),
    "occupied": ("cubicleoccupied", "occupied"),
    "voc": ("breathvoc",),
    "co2": ("co2equivalent",),
}
TRACKED = ("temperature", "iaq", "accuracy", "gas", "status", "uses", "flushes", "humidity")
KINDS = {  # problem kind → (category, issues, which values it's about)
    "fire": ("emergency", ["emergency"], ("temperature",)),
    "hot": ("facility", [], ("temperature",)),
    "air": ("cleanliness", ["bad_odor"], ("iaq", "gas")),
    "clean": ("cleanliness", ["dirty_toilet"], ("uses",)),
    "flush": ("facility", ["broken_flush"], ("flushes", "uses")),
    "sensor": ("system", [], ("status",)),
    "offline": ("system", [], ()),
}
TIER_TEXT = {0: "Watching", 1: "Job (no text)", 2: "Texted the janitors", 3: "Emergency"}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _ts(updated, fallback: float) -> float:
    try:
        t = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
    except (TypeError, ValueError):
        return fallback


def air_word(iaq: Optional[float]) -> str:
    """Bosch's IAQ scale in plain words."""
    if not _num(iaq):
        return ""
    for limit, word in ((50, "Excellent"), (100, "Good"), (150, "Fair"), (200, "Bad"), (300, "Very bad")):
        if iaq <= limit:
            return word
    return "Extremely bad"


def fmt(v, unit: str = "") -> str:
    if not _num(v):
        return "—"
    text = f"{v:,.0f}" if abs(v) >= 100 or float(v).is_integer() else f"{v:,.1f}"
    return f"{text} {unit}".strip() if unit else text


def mins(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    return f"{s // 3600} h {s % 3600 // 60} min"


def unit_kind(name: str) -> str:
    n = (name or "").lower()
    return "urinal" if "urinal" in n else "toilet" if re.search(r"toilet|wc|bowl|cubicle", n) else "other"


class Smart:
    def __init__(self, service, folder: Path, clock: Callable[[], float] = time.time):
        self.svc = service
        self.config_file = folder / "smart.json"
        self.state_file = folder / "smart_state.json"
        self.clock = clock
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self.samples: dict[tuple, deque] = {}
        self._seen: dict[tuple, str] = {}
        self.state = self._load(self.state_file)
        for k in ("baselines", "usage", "airHours", "counters", "problems", "seeded"):
            self.state.setdefault(k, {})
        self._dirty = False
        self._saved_at = 0.0
        self._deferred_at = 0.0
        self.last_check: Optional[float] = None
        self.last_error: Optional[str] = None
        self.units_seen: dict[str, dict] = {}     # thing id → latest unit snapshot (for the screens)
        self._last_learned: dict[tuple, float] = {}

    # ------------------------------------------------------------------ files
    @staticmethod
    def _load(path: Path) -> dict:
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _write(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")))
        os.replace(tmp, path)

    def _save_state(self, force: bool = False) -> None:
        if (self._dirty and self.clock() - self._saved_at >= SAVE_SECONDS) or force:
            self._write(self.state_file, self.state)
            self._dirty, self._saved_at = False, self.clock()

    # ------------------------------------------------------------------ settings
    def config(self) -> dict:
        saved = self._load(self.config_file)
        out = {"enabled": bool(saved.get("enabled", True)), "units": saved.get("units") or {},
               "kindsOff": [k for k in saved.get("kindsOff") or [] if k in KINDS]}
        for k, (lo, hi, typ, default, _) in SETTINGS.items():
            v = saved.get(k, default)
            out[k] = typ(v) if _num(v) and lo <= v <= hi else default
        return out

    def save_config(self, changes: dict) -> dict:
        with self._lock:
            cfg = self.config()
            for k, v in changes.items():
                if k == "enabled":
                    cfg["enabled"] = bool(v)
                elif k == "kindsOff":
                    if not isinstance(v, list):
                        raise ValueError("kindsOff must be a list.")
                    cfg["kindsOff"] = [x for x in KINDS if x in v and x != "fire"]   # fire can't be switched off
                elif k == "units":
                    if not isinstance(v, dict):
                        raise ValueError("Units must be a list of restrooms.")
                    for thing_id, u in v.items():
                        if not isinstance(u, dict):
                            continue
                        cur = cfg["units"].setdefault(str(thing_id)[:80], {})
                        if "location" in u:
                            cur["location"] = " ".join(str(u["location"] or "").split())[:60]
                            cur["locationCode"] = str(u.get("locationCode") or "")[:12] or None
                        if u.get("kind") in ("urinal", "toilet", "other"):
                            cur["kind"] = u["kind"]
                        if "on" in u:
                            cur["on"] = bool(u["on"])
                elif k in SETTINGS:
                    lo, hi, typ, _, label = SETTINGS[k]
                    if not _num(v) or not lo <= v <= hi:
                        raise ValueError(f"{label}: pick a number from {lo} to {hi}.")
                    cfg[k] = typ(v)
            if cfg["iaqVeryBad"] <= cfg["iaqBad"]:
                raise ValueError("“Very bad air” must be higher than “Bad air”.")
            if cfg["airSmsMinutes"] <= cfg["airJobMinutes"]:
                raise ValueError("Texting the janitors must come after the job (more minutes).")
            self._write(self.config_file, cfg)
            return cfg

    def quiet(self, now: Optional[float] = None, cfg: Optional[dict] = None) -> bool:
        """Quiet hours (7 PM–5 AM by default): no non-emergency texts."""
        cfg = cfg or self.config()
        h = datetime.fromtimestamp(self.clock() if now is None else now, LOCAL_TZ).hour
        a, b = cfg["quietStart"], cfg["quietEnd"]
        return False if a == b else (a <= h or h < b) if a > b else (a <= h < b)

    def quiet_text(self, cfg: Optional[dict] = None) -> str:
        cfg = cfg or self.config()
        return f"{_hour(cfg['quietStart'])}–{_hour(cfg['quietEnd'])}"

    # ------------------------------------------------------------------ background
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while True:
                time.sleep(CHECK_SECONDS)
                try:
                    self.svc.run_job("smart", self.check)
                except Exception:  # noqa: BLE001 - shown in System health; try again
                    log.warning("Smart monitoring check failed; will retry", exc_info=True)

        self._thread = threading.Thread(target=loop, daemon=True, name="smart")
        self._thread.start()
        log.info("Smart monitoring: checking every %d s", CHECK_SECONDS)

    # ------------------------------------------------------------------ reading
    def _units(self, data: dict, cfg: dict) -> list[dict]:
        """Arduino Things with AureoClean variables → units with their values by meaning."""
        out = []
        for t in data.get("things", []):
            by = {}
            names = {v["name"].lower(): v for v in t["properties"]}
            for meaning, aliases in ALIASES.items():
                v = next((names[a] for a in aliases if a in names), None)
                if v is not None:
                    by[meaning] = v
            if not ({"temperature", "iaq", "uses"} & set(by)):
                continue
            u = cfg["units"].get(t["id"], {})
            out.append({"id": t["id"], "name": t["name"], "vars": by, "location": u.get("location") or "",
                        "locationCode": u.get("locationCode"), "kind": u.get("kind") or unit_kind(t["name"]),
                        "on": u.get("on", True)})
        return out

    def _ingest(self, unit: dict, now: float) -> None:
        for meaning in TRACKED:
            v = unit["vars"].get(meaning)
            if v is None or not _num(v.get("value")):
                continue
            key = (unit["id"], meaning)
            stamp = str(v.get("updatedAt") or "")
            if stamp and self._seen.get(key) == stamp:
                continue
            self._seen[key] = stamp
            t = _ts(stamp, now) if stamp else now
            dq = self.samples.setdefault(key, deque(maxlen=1500))
            if dq and t <= dq[-1][0]:
                t = dq[-1][0] + 0.001
            dq.append((t, float(v["value"])))
            while dq and dq[0][0] < now - WINDOW_SECONDS:
                dq.popleft()

    def recent(self, thing: str, meaning: str, seconds: float, now: float) -> list:
        return [p for p in self.samples.get((thing, meaning), ()) if p[0] >= now - seconds]

    def latest(self, thing: str, meaning: str) -> Optional[tuple]:
        dq = self.samples.get((thing, meaning))
        return dq[-1] if dq else None

    def _value(self, unit: dict, meaning: str):
        v = unit["vars"].get(meaning)
        return v.get("value") if v else None

    # ------------------------------------------------------------------ learning
    def _learn(self, unit: dict, now: float, problems_now: set) -> None:
        local = datetime.fromtimestamp(now, LOCAL_TZ)
        hour_key, hour = local.strftime("%Y-%m-%dT%H"), str(local.hour)
        tid = unit["id"]
        accuracy = self._value(unit, "accuracy")
        status = self._value(unit, "status")
        sensor_ok = not _num(status) or status >= 0
        # Baselines per hour of the day (not while there's an air problem, so trouble doesn't become "normal").
        for meaning in ("iaq", "gas", "temperature"):
            last = self.latest(tid, meaning)
            if not last or now - last[0] > 120 or not sensor_ok:
                continue
            if meaning == "iaq" and not (_num(accuracy) and accuracy >= 2):
                continue
            if meaning in ("iaq", "gas") and "air" in problems_now:
                continue
            if self._last_learned.get((tid, meaning)) == last[0]:
                continue
            self._last_learned[(tid, meaning)] = last[0]
            b = self.state["baselines"].setdefault(tid, {}).setdefault(meaning, {})
            n, mean, var = b.get(hour, [0, 0.0, 0.0])
            n = min(n + 1, 720)
            delta = last[1] - mean
            mean += delta / n
            var += (delta * (last[1] - mean) - var) / n
            b[hour] = [n, round(mean, 4), round(max(var, 0.0), 4)]
            self._dirty = True
            if meaning == "iaq":
                bucket = self.state["airHours"].setdefault(tid, {}).setdefault(hour_key, [0.0, 0])
                bucket[0] = round(bucket[0] + last[1], 2)
                bucket[1] += 1
        # Uses per hour (from the counter).
        uses = self._value(unit, "uses")
        c = self.state["counters"].setdefault(tid, {})
        if _num(uses):
            uses = int(uses)
            prev = c.get("uses")
            if prev is not None and 0 < uses - prev < 1000:
                u = self.state["usage"].setdefault(tid, {})
                u[hour_key] = u.get(hour_key, 0) + (uses - prev)
            if prev is None:                          # first look at this unit
                c.setdefault("cleanedAtUses", uses)
                c.setdefault("cleanedAt", iso(utcnow()))
                c.setdefault("usesAtFlush", uses)
            elif uses < prev:                         # the board's counter started again from 0: keep counting
                c["cleanedAtUses"] = c.get("cleanedAtUses", prev) - prev
                c["usesAtFlush"] = c.get("usesAtFlush", prev) - prev
                if 0 < uses < 1000:
                    u = self.state["usage"].setdefault(tid, {})
                    u[hour_key] = u.get(hour_key, 0) + uses
            if prev != uses:
                c["uses"] = uses
                self._dirty = True
        flushes = self._value(unit, "flushes")
        if _num(flushes):
            flushes = int(flushes)
            if c.get("flushes") is None or flushes != c["flushes"]:
                if c.get("flushes") is not None and (flushes > c["flushes"] or 0 < flushes < c["flushes"]) and _num(uses):
                    c["usesAtFlush"] = int(uses)
                    c["flushedAt"] = now
                c["flushes"] = flushes
                self._dirty = True

    def _prune(self, now: float) -> None:
        cutoff = datetime.fromtimestamp(now - KEEP_DAYS * 86400, LOCAL_TZ).strftime("%Y-%m-%dT%H")
        for table in ("usage", "airHours"):
            for rows in self.state[table].values():
                for k in [k for k in rows if k < cutoff]:
                    del rows[k]

    def baseline(self, thing: str, meaning: str, hour: int) -> Optional[tuple]:
        """(mean, sd, n) for this hour of the day, or the all-day mean when this hour has too little data."""
        b = self.state["baselines"].get(thing, {}).get(meaning, {})
        n, mean, var = b.get(str(hour), [0, 0, 0])
        if n >= 20:
            return mean, math.sqrt(var), n
        total = sum(x[0] for x in b.values())
        if total >= 30:
            m = sum(x[0] * x[1] for x in b.values()) / total
            return m, math.sqrt(sum(x[0] * x[2] for x in b.values()) / total), total
        return None

    def typical_uses(self, thing: str, hour: int, now: float) -> Optional[float]:
        rows = self.state["usage"].get(thing, {})
        vals = [v for k, v in rows.items() if int(k[-2:]) == hour and k < datetime.fromtimestamp(now, LOCAL_TZ).strftime("%Y-%m-%dT%H")]
        return sum(vals) / len(vals) if len(vals) >= 2 else None

    def uses_in(self, thing: str, seconds: float, now: float) -> Optional[int]:
        pts = self.samples.get((thing, "uses"))
        if not pts:
            return None
        old = [p for p in pts if p[0] <= now - seconds]
        first = old[-1][1] if old else pts[0][1]
        return int(max(0, pts[-1][1] - first))

    # ------------------------------------------------------------------ detectors
    def _held(self, thing: str, kind: str, now: float) -> float:
        p = self.state["problems"].get(f"{thing}:{kind}")
        return now - p["since"] if p and p.get("since") else 0.0

    def detect(self, unit: dict, cfg: dict, now: float) -> list[dict]:
        tid, name = unit["id"], unit["name"]
        out = []
        status = self._value(unit, "status")
        sensor_ok = not _num(status) or status >= 0
        starting = _num(status) and status == 1
        # --- offline: nothing new from the unit for a while
        stamps = [dq[-1][0] for (t, _), dq in self.samples.items() if t == tid and dq]
        newest = max([_ts(v.get("updatedAt"), 0) for v in unit["vars"].values()] + stamps + [0])
        if newest and now - newest > cfg["offlineMinutes"] * 60:
            out.append({"kind": "offline", "tier": 1, "title": f"{name} is offline",
                        "detail": f"No data for {mins(now - newest)}. Check its power and Wi-Fi. Smart alerts can't "
                                  "work for this unit until it's back.", "facts": {"lastData": iso(datetime.fromtimestamp(newest, timezone.utc))}})
            return out
        # --- BME680 error
        if _num(status) and status < 0:
            held = self._held(tid, "sensor", now)
            out.append({"kind": "sensor", "tier": 1 if held >= 300 else 0, "title": "Air sensor not working",
                        "detail": f"The BME680 reports error {int(status)} on {name} (the board retries every 30 s). "
                                  "Air and temperature alerts can't work until it's fixed. Check the wiring.",
                        "facts": {"status": status}})
        temps = [p for p in self.recent(tid, "temperature", 600, now) if -20 <= p[1] <= 120]
        # --- fire / heat (emergency)
        if sensor_ok and len(temps) >= 3 and now - temps[-1][0] < 90:
            run = []
            for p in reversed(temps):
                if p[1] < cfg["fireTemp"]:
                    break
                run.append(p)
            held_hot = run[0][0] - run[-1][0] if run else 0
            window = [p for p in temps if p[0] >= temps[-1][0] - 120]
            low = min(p[1] for p in window)
            rise = temps[-1][1] - low
            rise_prev = temps[-2][1] - min(p[1] for p in window if p[0] <= temps[-2][0])
            air_jump = self._air_jump(tid, now)
            fire = None
            if len(run) >= 3 and held_hot >= cfg["fireSeconds"] - 6:
                fire = (f"Very high temperature: {fmt(temps[-1][1], '°C')}",
                        f"{fmt(temps[-1][1], '°C')} for {mins(held_hot)} (emergency limit {fmt(cfg['fireTemp'], '°C')}). Possible fire.")
            elif (temps[-1][1] < cfg["fireTemp"] and rise >= cfg["riseDegrees"] and rise_prev >= cfg["riseDegrees"] * 0.75
                  and temps[-1][1] >= cfg["riseMinTemp"]):   # (at or above fireTemp: wait for the fireSeconds rule)
                fire = (f"Temperature rising fast: +{fmt(rise)} °C in 2 min",
                        f"Now {fmt(temps[-1][1], '°C')}, up {fmt(rise)} °C in 2 minutes. Possible fire.")
            elif rise >= 4 and air_jump and temps[-1][1] >= 34 and len(window) >= 3:
                fire = ("Possible smoke or fire: heat and bad air together",
                        f"Temperature up {fmt(rise)} °C to {fmt(temps[-1][1], '°C')} while {air_jump} — both within 2 minutes.")
            if fire:
                out.append({"kind": "fire", "tier": 3, "title": fire[0], "detail": fire[1] + " Check the restroom now; "
                            "call security / 911 if there is smoke or fire.", "facts": {"temperature": temps[-1][1], "rise": round(rise, 1)}})
        # --- hot room
        if sensor_ok and temps and not any(c["kind"] == "fire" for c in out):
            recent = [p[1] for p in temps if p[0] >= temps[-1][0] - 120]
            now_t = median(recent)
            if now_t > cfg["hotTemp"]:
                held = self._held(tid, "hot", now)
                tier = 2 if held >= cfg["hotMinutes"] * 120 else 1 if held >= cfg["hotMinutes"] * 60 else 0
                out.append({"kind": "hot", "tier": tier, "title": f"Very hot: {fmt(now_t, '°C')}",
                            "detail": f"Above {fmt(cfg['hotTemp'], '°C')} for {mins(held)}. Check the exhaust fan, ventilation "
                                      "and the windows; tell an admin if the aircon is off.", "facts": {"temperature": now_t}})
        # --- bad air
        if sensor_ok and not starting:
            air = self._air(unit, cfg, now)
            if air:
                held = self._held(tid, "air", now)
                tier = 0
                if held >= cfg["airJobMinutes"] * 60:
                    tier = 1
                if held >= cfg["airSmsMinutes"] * 60 or (air["level"] >= cfg["iaqVeryBad"] and held >= 300):
                    tier = 2
                out.append({"kind": "air", "tier": tier, "title": air["title"],
                            "detail": f"{air['detail']} for {mins(held)}. {self._usage_context(tid, now)}",
                            "facts": air["facts"]})
        # --- cleaning due (uses since the last cleaning)
        c = self.state["counters"].get(tid, {})
        uses = self._value(unit, "uses")
        if _num(uses) and "cleanedAtUses" in c:
            since = int(uses) - int(c["cleanedAtUses"])
            if since >= cfg["cleanEvery"]:
                tier = 2 if since >= cfg["cleanEvery"] * 1.5 else 1
                out.append({"kind": "clean", "tier": tier, "title": f"Cleaning due: {since} uses since the last cleaning",
                            "detail": f"{name} has been used {since} times since it was last cleaned ({_ago(c.get('cleanedAt'))}). "
                                      f"Cleaning is due every {cfg['cleanEvery']} uses.", "facts": {"uses": since}})
        # --- auto-flush not flushing
        if _num(uses) and "usesAtFlush" in c and _num(self._value(unit, "flushes")):
            per = self._value(unit, "flushThreshold")
            per = int(per) if _num(per) and per >= 1 else 1
            missed = int(uses) - int(c["usesAtFlush"])
            if missed >= per + cfg["flushMissUses"] - 1:
                tier = 2 if missed >= per + cfg["flushMissUses"] * 2 else 1
                out.append({"kind": "flush", "tier": tier, "title": f"Auto-flush may be broken: {missed} uses, no flush",
                            "detail": f"{name} counted {missed} uses since its last flush" + (f" (it flushes every {per} uses)" if per > 1 else "")
                                      + ". Check the linear actuator, its relays and the 12 V supply; try Flush now on the dashboard.",
                            "facts": {"usesWithoutFlush": missed}})
        return out

    def _air_jump(self, tid: str, now: float) -> str:
        """Sudden worse air in the last 2 minutes (for the smoke pattern), in words; '' if none."""
        iaq = self.recent(tid, "iaq", 150, now)
        if len(iaq) >= 2 and iaq[-1][1] - min(p[1] for p in iaq) >= 80:
            return f"IAQ jumped to {fmt(iaq[-1][1])}"
        gas = self.recent(tid, "gas", 150, now)
        if len(gas) >= 2 and max(p[1] for p in gas) > 0 and gas[-1][1] <= max(p[1] for p in gas) * 0.6:
            return "the gas reading dropped sharply"
        return ""

    def _air(self, unit: dict, cfg: dict, now: float) -> Optional[dict]:
        tid = unit["id"]
        accuracy = self._value(unit, "accuracy")
        iaq = [p[1] for p in self.recent(tid, "iaq", 120, now)]
        if iaq and _num(accuracy) and accuracy >= 2:
            level = median(iaq)
            if level > cfg["iaqBad"]:
                return {"level": level, "title": f"Bad air: IAQ {fmt(level)} ({air_word(level).lower()})",
                        "detail": f"Air quality index {fmt(level)} (bad above {cfg['iaqBad']})", "facts": {"iaq": level}}
            return None
        # IAQ still calibrating: compare the raw gas reading with this hour's normal.
        gas = [p[1] for p in self.recent(tid, "gas", 120, now)]
        hour = datetime.fromtimestamp(now, LOCAL_TZ).hour
        base = self.baseline(tid, "gas", hour)
        if gas and base and base[0] > 0:
            g = median(gas)
            drop = 1 - g / base[0]
            if drop * 100 >= cfg["gasDropPercent"]:
                unit_txt = "kΩ" if base[0] < 5000 else "Ω"
                return {"level": cfg["iaqBad"] + 1, "title": f"Bad air: gas reading {round(drop * 100)}% below normal",
                        "detail": f"Gas reading {fmt(g, unit_txt)} vs. {fmt(base[0], unit_txt)} normally at this hour "
                                  "(IAQ is still calibrating, so the raw reading is used)",
                        "facts": {"gas": g, "normal": round(base[0], 1), "dropPercent": round(drop * 100)}}
        return None

    def _usage_context(self, tid: str, now: float) -> str:
        used = self.uses_in(tid, 1800, now)
        if used is None:
            return ""
        typical = self.typical_uses(tid, datetime.fromtimestamp(now, LOCAL_TZ).hour, now)
        usual = f" (usually about {round(typical / 2)} per half hour)" if typical is not None else ""
        if (typical is not None and used >= max(4, typical)) or (typical is None and used >= 12):
            return f"It follows heavy use: {used} uses in the last 30 min{usual}. Clean and flush, then check again."
        if used <= 1:
            return (f"It's barely being used ({used} use in 30 min){usual}, so the smell has another cause: "
                    "check the floor drain, a leak, the trash bin, or a clog.")
        return f"{used} uses in the last 30 min{usual}."

    # ------------------------------------------------------------------ main check
    def check(self) -> int:
        """Read the units, learn, detect, and open/raise/close jobs. Returns new reports made."""
        with self._lock:
            cfg = self.config()
            if not cfg["enabled"] or not self.svc.arduino.configured:
                self.last_check, self.last_error = self.clock(), None
                return 0
            try:
                data = self.svc.arduino.readings()
            except Exception as exc:
                self.last_error = f"Can't read the sensors: {exc}"
                raise
            now = self.clock()
            made = 0
            for unit in self._units(data, cfg):
                self._ingest(unit, now)
                if not unit["on"]:
                    continue
                ongoing = {k.split(":", 1)[1] for k, p in self.state["problems"].items() if k.startswith(unit["id"] + ":") and p.get("since")}
                self._learn(unit, now, ongoing)
                conds = [c for c in self.detect(unit, cfg, now) if c["kind"] not in cfg["kindsOff"]]
                made += self._manage(unit, conds, cfg, now)
                self.units_seen[unit["id"]] = self._snapshot(unit, cfg, now)
            if now - self._deferred_at >= 60:
                self._deferred_at = now
                self._prune(now)
                try:
                    self.svc.send_deferred_texts()
                except Exception:  # noqa: BLE001 - retried next minute
                    log.warning("Sending held texts failed", exc_info=True)
            self._save_state()
            self.last_check, self.last_error = now, None
            return made

    def _manage(self, unit: dict, conds: list[dict], cfg: dict, now: float) -> int:
        tid = unit["id"]
        made = 0
        present = {c["kind"]: c for c in conds}
        problems = self.state["problems"]
        for kind in KINDS:
            key = f"{tid}:{kind}"
            p = problems.get(key)
            c = present.get(kind)
            if c:
                if p is None:
                    p = problems[key] = {"since": now, "tier": 0}
                    self._dirty = True
                if not p.get("since"):
                    p["since"] = now
                    if p.get("reportId") and self._status(p["reportId"]) not in (None, "resolved"):
                        self._note(p["reportId"], f"It's back: {c['title']}.")
                p.pop("clearSince", None)
                p["title"] = c["title"]
                if p.get("reportId"):
                    status = self._status(p["reportId"])
                    if status in (None, "resolved"):
                        p.update(reportId=None, ref=None, tier=0, doneAt=now)
                        self._dirty = True
                    elif c["tier"] > p.get("tier", 0):
                        self._raise(p, unit, c, cfg)
                        self._dirty = True
                        continue
                    else:
                        continue
                if c["tier"] >= 1 and now - (p.get("doneAt") or 0) >= AFTER_DONE_SECONDS:
                    self._open(p, unit, c, cfg)
                    made += 1
                    self._dirty = True
            elif p:
                if kind == "clean" or not p.get("reportId"):
                    del problems[key]           # never reported (or cleaned): just forget it
                    self._dirty = True
                    continue
                if self._status(p["reportId"]) in (None, "resolved"):
                    del problems[key]
                    self._dirty = True
                    continue
                if p.get("since"):
                    p["clearSince"] = p.get("clearSince") or now
                    if now - p["clearSince"] >= CLEAR_SECONDS:
                        self._note(p["reportId"], f"Back to normal on {unit['name']} (it was: {p.get('title', kind)}).")
                        p["since"] = None
                        p.pop("clearSince", None)
                        self._dirty = True
        return made

    # ------------------------------------------------------------------ reports and texts
    def _status(self, report_id: Optional[str]) -> Optional[str]:
        if not report_id:
            return None
        r = self.svc.store.get(self.svc.C_REPORTS, report_id)
        return r.get("status") if r else None

    def _note(self, report_id: str, text: str) -> None:
        try:
            self.svc._add_report_note(report_id, {"type": "system", "username": "smart"}, text)
        except Exception:  # noqa: BLE001 - the report may have been deleted
            pass

    def _open(self, p: dict, unit: dict, c: dict, cfg: dict) -> None:
        category, issues, _ = KINDS[c["kind"]]
        if c["kind"] == "clean" and unit["kind"] == "other":
            issues = ["dirty_floor"]
        tier = c["tier"]
        # A report people already made about the same thing in this restroom → add to it instead.
        same = self._matching_report(unit, issues) if c["kind"] in ("air", "clean") else None
        if same:
            rid, r = same
            self._note(rid, f"Sensors agree ({unit['name']}): {c['title']}. {c['detail']}")
            p.update(reportId=rid, ref=r.get("smsRef"), tier=min(tier, 1), opened=iso(utcnow()))
            if tier >= 2:
                self._raise(p, unit, c, cfg)
            return
        priority = {1: "normal", 2: "high", 3: "critical"}[tier]
        report = self.svc._save_report(
            category="emergency" if tier == 3 else category, title=c["title"][:80],
            location=unit["location"] or unit["name"], details=f"{unit['name']}: {c['detail']}"[:500],
            priority=priority, created_by={"type": "system", "username": "smart"},
            extra={"source": "smart", "channel": "arduino", "issues": ["emergency"] if tier == 3 else issues,
                   "urgency": "high" if tier >= 2 else "medium", "locationCode": unit.get("locationCode"),
                   "smart": {"kind": c["kind"], "thingId": unit["id"], "thing": unit["name"], "tier": tier,
                             "facts": c.get("facts") or {}}})
        p.update(reportId=report["id"], ref=report["smsRef"], tier=tier, opened=report["createdAt"])
        self.svc.audit("smart_alert", {"type": "system", "username": "smart"}, report["id"],
                       {"kind": c["kind"], "tier": tier, "thing": unit["name"], "title": c["title"][:80]})
        log.info("Smart: %s (%s) → report #%s, tier %s", c["title"], unit["name"], report["smsRef"], tier)
        if tier >= 2:
            self._text(report["id"], report, unit, c, tier)

    def _raise(self, p: dict, unit: dict, c: dict, cfg: dict) -> None:
        """Move an open report up a tier: higher priority, and the texts for the new tier."""
        tier = c["tier"]

        def bump(current):
            if current is None:
                return None
            current["priority"] = {1: "normal", 2: "high", 3: "critical"}[tier]
            if tier == 3:
                current["category"] = "emergency"
            current["title"] = c["title"][:80]
            current.setdefault("smart", {})["tier"] = tier
            return current
        report = self.svc.store.transact(self.svc.C_REPORTS, p["reportId"], bump)
        p["tier"] = tier
        if report is None:
            return
        self._note(p["reportId"], {2: f"Still not fixed: {c['title']}. Texting the janitors.",
                                   3: f"EMERGENCY: {c['title']}. Texting everyone."}.get(tier, c["title"]))
        self.svc.audit("smart_alert_raised", {"type": "system", "username": "smart"}, p["reportId"],
                       {"kind": c["kind"], "tier": tier})
        if tier >= 2:
            self._text(p["reportId"], report, unit, c, tier)

    def _text(self, report_id: str, report: dict, unit: dict, c: dict, tier: int) -> None:
        from .two_way import alert_text
        label = "EMERGENCY" if tier == 3 else "ALERT"
        text = alert_text(label, report["smsRef"], f"{c['title']} ({unit['name']})", report.get("location", ""))
        self.svc.alert_people(report_id, "emergency" if tier == 3 else "janitors", text)

    def _matching_report(self, unit: dict, issues: list) -> Optional[tuple]:
        if not unit["location"]:
            return None
        cutoff = iso(datetime.fromtimestamp(self.clock() - 6 * 3600, timezone.utc))
        for status in ("open", "in_progress"):
            for rid, r in self.svc.store.query(self.svc.C_REPORTS, status=status):
                if (r.get("location") == unit["location"] and r.get("source") not in ("smart", "sensor")
                        and r.get("createdAt", "") >= cutoff and set(r.get("issues") or []) & (set(issues) | {"bad_odor", "dirty_toilet", "dirty_floor"})):
                    return rid, r
        return None

    # ------------------------------------------------------------------ cleaning
    def cleaned(self, thing_id: str) -> dict:
        """A janitor cleaned this unit: start counting uses again."""
        with self._lock:
            c = self.state["counters"].setdefault(thing_id, {})
            if c.get("uses") is None:
                raise ValueError("No use count from this unit yet.")
            c["cleanedAtUses"] = c["uses"]
            c["cleanedAt"] = iso(utcnow())
            self._dirty = True
            self._save_state(force=True)
            return c

    def report_done(self, report: dict) -> None:
        """A job was marked done: a cleaning job (or a cleanliness report in the unit's restroom) resets the count."""
        smart = report.get("smart") or {}
        cfg = self.config()
        with self._lock:
            targets = []
            if smart.get("kind") == "clean" and smart.get("thingId"):
                targets.append(smart["thingId"])
            elif report.get("category") == "cleanliness" and report.get("location"):
                targets += [tid for tid, u in cfg["units"].items() if u.get("location") == report["location"]]
            for tid in targets:
                c = self.state["counters"].get(tid)
                if c and c.get("uses") is not None:
                    c["cleanedAtUses"] = c["uses"]
                    c["cleanedAt"] = iso(utcnow())
                    self._dirty = True
            if smart.get("kind") == "flush" and smart.get("thingId"):
                c = self.state["counters"].get(smart["thingId"])
                if c and c.get("uses") is not None:
                    c["usesAtFlush"] = c["uses"]       # fixed: count misses from now
            if targets:
                self._save_state(force=True)

    # ------------------------------------------------------------------ screens
    def _snapshot(self, unit: dict, cfg: dict, now: float) -> dict:
        tid = unit["id"]
        c = self.state["counters"].get(tid, {})
        iaq, acc, temp = self._value(unit, "iaq"), self._value(unit, "accuracy"), self._value(unit, "temperature")
        uses = self._value(unit, "uses")
        stamps = [_ts(v.get("updatedAt"), 0) for v in unit["vars"].values()]
        newest = max(stamps + [0])
        problems = []
        for kind in KINDS:
            p = self.state["problems"].get(f"{tid}:{kind}")
            if p and (p.get("since") or p.get("reportId")):
                problems.append({"kind": kind, "title": p.get("title", kind), "tier": p.get("tier", 0),
                                 "tierText": TIER_TEXT.get(p.get("tier", 0), ""), "ref": p.get("ref"),
                                 "reportId": p.get("reportId"), "active": bool(p.get("since")),
                                 "for": mins(now - p["since"]) if p.get("since") else ""})
        status = self._value(unit, "status")
        calibrating = _num(iaq) and not (_num(acc) and acc >= 2)
        return {
            "id": tid, "name": unit["name"], "kind": unit["kind"], "location": unit["location"], "on": unit["on"],
            "online": bool(newest) and now - newest <= cfg["offlineMinutes"] * 60, "lastData": newest or None,
            "temperature": temp if _num(temp) else None,
            "iaq": iaq if _num(iaq) else None, "accuracy": int(acc) if _num(acc) else None, "calibrating": calibrating,
            "air": air_word(iaq) if _num(iaq) and not calibrating else ("Calibrating" if _num(iaq) else ""),
            "sensorOk": not _num(status) or status >= 0,
            "uses": int(uses) if _num(uses) else None,
            "usesSinceCleaned": int(uses) - int(c["cleanedAtUses"]) if _num(uses) and "cleanedAtUses" in c else None,
            "cleanEvery": cfg["cleanEvery"], "cleanedAt": c.get("cleanedAt"),
            "usesToday": self._uses_today(tid, now),
            "problems": sorted(problems, key=lambda x: (-x["tier"], x["kind"])),
        }

    def _uses_today(self, tid: str, now: float) -> Optional[int]:
        rows = self.state["usage"].get(tid)
        if rows is None:
            return None
        day = datetime.fromtimestamp(now, LOCAL_TZ).strftime("%Y-%m-%d")
        return sum(v for k, v in rows.items() if k.startswith(day))

    def overview(self) -> dict:
        cfg = self.config()
        alive = bool(self._thread and self._thread.is_alive())
        return {"enabled": cfg["enabled"], "quiet": self.quiet(cfg=cfg), "quietHours": self.quiet_text(cfg),
                "units": list(self.units_seen.values()),
                "checker": {"running": alive, "every": CHECK_SECONDS, "error": self.last_error,
                            "ago": None if self.last_check is None else round(self.clock() - self.last_check)}}

    @staticmethod
    def automatic_rules(cfg: dict) -> list[dict]:
        """The built-in alerts in plain words, with today's limits (for the Alerts page)."""
        c = cfg
        rules = [
            ("fire", "Fire or extreme heat",
             f"{fmt(c['fireTemp'], '°C')} or more for {c['fireSeconds']} s · up {fmt(c['riseDegrees'])} °C within 2 min "
             f"(and at least {fmt(c['riseMinTemp'], '°C')}) · heat with suddenly bad air",
             "Emergency report · texts everyone active today, at any hour", "red"),
            ("hot", "Hot room", f"Above {fmt(c['hotTemp'], '°C')} for {c['hotMinutes']} min",
             f"Job · still hot after {c['hotMinutes'] * 2} min → texts the janitors", "amber"),
            ("air", "Bad air (odour)",
             f"IAQ above {c['iaqBad']} for {c['airJobMinutes']} min (while the sensor warms up: gas reading {c['gasDropPercent']}% below normal)",
             f"Job · after {c['airSmsMinutes']} min, or IAQ above {c['iaqVeryBad']} for 5 min → texts the janitors", "amber"),
            ("clean", "Cleaning due", f"{c['cleanEvery']} uses since the last cleaning (ToF count)",
             f"Job · at {int(c['cleanEvery'] * 1.5)} uses → texts the janitors", "blue"),
            ("flush", "Auto-flush not working", f"{c['flushMissUses']} uses without a flush",
             f"Job · {c['flushMissUses'] * 2} uses → texts the janitors", "blue"),
            ("sensor", "Air sensor fault", "BME680 error for 5 min", "Job for the admins (no text)", ""),
            ("offline", "Unit offline", f"No data for {c['offlineMinutes']} min", "Job for the admins (no text)", ""),
        ]
        return [{"kind": k, "title": ti, "when": w, "then": th, "color": col, "on": k not in c["kindsOff"],
                 "canTurnOff": k != "fire"} for k, ti, w, th, col in rules]

    def problems_for_screens(self) -> dict:
        """{thing id: {variable name: tier}} so the dashboard tiles light up with smart problems too."""
        out: dict = {}
        for snap in self.units_seen.values():
            for p in snap["problems"]:
                if not p["active"]:
                    continue
                for meaning in KINDS[p["kind"]][2]:
                    out.setdefault(snap["id"], {})[meaning] = max(out.get(snap["id"], {}).get(meaning, 0), p["tier"])
        return out

    # ------------------------------------------------------------------ patterns (for the daily advice)
    def patterns(self) -> list[dict]:
        """Per unit: busiest hours, hours when the air is usually bad, uses yesterday — from the last 7 days."""
        now = self.clock()
        today = datetime.fromtimestamp(now, LOCAL_TZ)
        week_ago = datetime.fromtimestamp(now - 7 * 86400, LOCAL_TZ).strftime("%Y-%m-%dT%H")
        yesterday = datetime.fromtimestamp(now - 86400, LOCAL_TZ).strftime("%Y-%m-%d")
        cfg = self.config()
        out = []
        for tid, snap in self.units_seen.items():
            usage = {k: v for k, v in self.state["usage"].get(tid, {}).items() if k >= week_ago}
            days = max(1, len({k[:10] for k in usage}))
            by_hour = {}
            for k, v in usage.items():
                by_hour[int(k[-2:])] = by_hour.get(int(k[-2:]), 0) + v
            busiest = sorted(((h, round(v / days, 1)) for h, v in by_hour.items()), key=lambda x: -x[1])[:3]
            air = {}
            for k, (s, n) in self.state["airHours"].get(tid, {}).items():
                if k >= week_ago and n:
                    air.setdefault(int(k[-2:]), []).append(s / n)
            bad_hours = sorted((h, round(sum(v) / len(v))) for h, v in air.items() if sum(v) / len(v) > cfg["iaqBad"] * 0.85)
            out.append({
                "unit": snap["name"], "thingId": tid, "kind": snap["kind"], "location": snap["location"] or snap["name"],
                "usesYesterday": sum(v for k, v in usage.items() if k.startswith(yesterday)),
                "usesToday": snap.get("usesToday"), "usesPerDay": round(sum(usage.values()) / days, 1) if usage else None,
                "busiestHours": [{"hour": h, "label": _hour(h), "avgUses": v} for h, v in busiest if v > 0],
                "airUsuallyBadAt": [{"hour": h, "label": _hour(h), "avgIaq": v} for h, v in bad_hours][:4],
                "usesSinceCleaned": snap.get("usesSinceCleaned"), "cleanEvery": cfg["cleanEvery"],
                "cleanedAt": snap.get("cleanedAt"), "online": snap.get("online"),
                "iaqCalibrating": snap.get("calibrating"), "openProblems": [p["title"] for p in snap["problems"] if p["reportId"]],
                "daysOfData": days if usage else 0, "date": today.strftime("%Y-%m-%d"),
            })
        return out

    # ------------------------------------------------------------------ first start: learn from Arduino's history
    def seed(self) -> int:
        """Fill the patterns from the last 3 days of Arduino history (once per unit), so the advice works on day 1."""
        try:
            data = self.svc.arduino.readings()
        except Exception:  # noqa: BLE001
            return 0
        cfg = self.config()
        done = 0
        for unit in self._units(data, cfg):
            tid = unit["id"]
            if self.state["seeded"].get(tid):
                continue
            for meaning in ("uses", "iaq", "gas"):
                v = unit["vars"].get(meaning)
                if v is None:
                    continue
                try:
                    pts = self.svc.arduino.series(tid, v["id"], 72)
                except Exception:  # noqa: BLE001
                    continue
                with self._lock:
                    if meaning == "uses":
                        u = self.state["usage"].setdefault(tid, {})
                        if u:
                            continue
                        for (_t0, a), (t1, b) in zip(pts, pts[1:]):
                            if 0 < b - a < 1000:
                                k = datetime.fromtimestamp(t1, LOCAL_TZ).strftime("%Y-%m-%dT%H")
                                u[k] = u.get(k, 0) + int(b - a)
                    else:
                        base = self.state["baselines"].setdefault(tid, {}).setdefault(meaning, {})
                        if base:
                            continue
                        for t, val in pts:
                            h = str(datetime.fromtimestamp(t, LOCAL_TZ).hour)
                            n, mean, var = base.get(h, [0, 0.0, 0.0])
                            n += 1
                            d = val - mean
                            mean += d / n
                            var += (d * (val - mean) - var) / n
                            base[h] = [n, round(mean, 4), round(max(var, 0), 4)]
                            if meaning == "iaq":
                                k = datetime.fromtimestamp(t, LOCAL_TZ).strftime("%Y-%m-%dT%H")
                                b2 = self.state["airHours"].setdefault(tid, {}).setdefault(k, [0.0, 0])
                                b2[0] += val
                                b2[1] += 1
            with self._lock:
                self.state["seeded"][tid] = True
                self._dirty = True
            done += 1
        self._save_state(force=True)
        return done


def _hour(h: int) -> str:
    h = int(h) % 24
    return "12 AM" if h == 0 else "12 PM" if h == 12 else f"{h} AM" if h < 12 else f"{h - 12} PM"


def _ago(iso_text: Optional[str]) -> str:
    if not iso_text:
        return "not recorded"
    try:
        secs = (utcnow() - datetime.fromisoformat(iso_text)).total_seconds()
    except ValueError:
        return "not recorded"
    return f"{mins(secs)} ago"
