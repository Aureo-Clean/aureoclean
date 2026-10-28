"""Sensor alerts: fixed rules that turn a bad sensor reading into a report and texts.

An administrator sets a limit on an Arduino variable ("Temperature above 35 °C for
5 minutes", "BME680 gas resistance below 40 kΩ", "Smoke is on"). Every 15 seconds the
Pi compares the live values with the limits. When a value stays over its limit long
enough, it makes a report (source "sensor") and texts people with the same fixed rules
as every other report:

* Emergency  → texts everyone with SMS alerts on (critical priority)
* Urgent     → texts the janitors (high priority); no reply in 10 min → the admins
* Report only → a report in the lists, no text

No AI is involved. One report per problem: while that report is open, the sensor
going over the limit again only adds a note. When the value is back to normal for
2 minutes, a note says so (the janitor still marks the job done). After a job is
marked done, the same alert waits 30 minutes before it can start a new report.

Rules are kept on the Pi in ~/.config/aureoclean/sensor_alerts.json.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .security import generate_id, iso, utcnow

log = logging.getLogger("aureoclean.sensor_alerts")

CHECK_SECONDS = 15
CLEAR_SECONDS = 120          # back to normal this long → "back to normal" note
AFTER_DONE_SECONDS = 30 * 60  # a job marked done while still over the limit: wait before a new report
STALE_SECONDS = 3 * 60 * 60  # a reading older than this (device off?) is ignored
MAX_RULES = 60
MAX_REPORTS_PER_HOUR = 12    # sensor reports per hour (emergencies always get through)

LEVELS = {
    "emergency": {"label": "Emergency", "priority": "critical", "notify": "everyone", "sms": "EMERGENCY",
                  "about": "Report + text everyone"},
    "urgent": {"label": "Urgent", "priority": "high", "notify": "janitors", "sms": "SENSOR ALERT",
               "about": "Report + text the janitors (admins after 10 min)"},
    "report": {"label": "Report only", "priority": "normal", "notify": "none", "sms": "",
               "about": "Report in the lists, no text"},
}
OPS = {"above": "above", "below": "below", "on": "is on", "off": "is off"}

# What a variable is, from its name/type: (pattern, kind)
KINDS = [
    (r"smoke|fire|flame", "smoke"),
    (r"leak|flood|water_?detect|wet", "leak"),
    (r"iaq|air_?quality", "iaq"),
    (r"ammonia|nh3", "ammonia"),
    (r"co2|carbon_?dioxide", "co2"),
    (r"resist|ohm|gas|voc|bme", "gas"),
    (r"temp", "temperature"),
    (r"humid|rh\b", "humidity"),
    (r"soap", "soap"),
    (r"tissue|paper|towel", "paper"),
    (r"water|tank|level", "water"),
]
CATEGORY = {"smoke": "emergency", "leak": "facility", "iaq": "cleanliness", "ammonia": "cleanliness", "co2": "cleanliness",
            "gas": "cleanliness", "temperature": "facility", "humidity": "facility", "soap": "supplies",
            "paper": "supplies", "water": "facility"}
ISSUES = {"iaq": ["bad_odor"], "ammonia": ["bad_odor"], "gas": ["bad_odor"], "co2": ["bad_odor"], "leak": ["leak"],
          "soap": ["no_soap"], "paper": ["no_tissue"], "water": ["no_water"]}


class SensorAlertError(Exception):
    pass


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _bool(v) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if _num(v) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "false", "on", "off"):
        return v.strip().lower() in ("true", "on")
    return None


def kind_of(name: str, label: str = "", typ: str = "") -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", f"{name} {label}").lower() + " " + (typ or "").lower()
    for pattern, kind in KINDS:
        if re.search(pattern, text):
            return kind
    return ""


def show(value, unit: str = "") -> str:
    """41.25 → "41.3 °C", 52000 → "52,000 Ω" (for titles and texts)."""
    if isinstance(value, bool) or value is None:
        return "on" if value else "off"
    if not _num(value):
        return str(value)[:20]
    a = abs(value)
    text = f"{value:,.0f}" if a >= 100 or float(value).is_integer() else f"{value:,.1f}" if a >= 10 else f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{text} {unit}".strip() if unit else text


def _nice_round(x: float) -> float:
    """A round limit near x (34.6 → 35, 55000 → 55000, 47312 → 45000)."""
    if x == 0:
        return 0
    mag = 10 ** math.floor(math.log10(abs(x)))
    step = mag / 2
    return round(round(x / step) * step, 6)


def _age_seconds(updated: Optional[str], now: float) -> Optional[float]:
    if not updated:
        return None
    try:
        t = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return now - t.timestamp()
    except ValueError:
        return None


def _age_text(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h {s % 3600 // 60} min" if s % 3600 >= 60 and s < 36000 else f"{s // 3600} h"
    return f"{s // 86400} days"


def suggest(v: dict, history: Optional[list] = None) -> dict:
    """A sensible first limit for a variable, from what it measures. Admins adjust it after
    watching the chart. history: recent [[time, value], …] (used for the BME680 gas reading)."""
    name, label, typ, unit, value = v.get("name", ""), v.get("label", ""), v.get("type", ""), v.get("unit", ""), v.get("value")
    kind = kind_of(name, label, typ)
    base = {"kind": kind, "minutes": 2, "level": "urgent"}
    is_bool = _bool(value) is not None and (isinstance(value, bool) or "BOOL" in typ.upper() or typ.upper() in ("STATUS",))
    if is_bool:
        if kind == "smoke":
            return {**base, "op": "on", "limit": None, "level": "emergency", "minutes": 0,
                    "why": "Smoke or fire: text everyone at once."}
        if kind == "leak":
            return {**base, "op": "on", "limit": None, "minutes": 0, "why": "A leak needs a janitor soon."}
        return {**base, "op": "on", "limit": None, "level": "report", "minutes": 0, "why": "Make a report when this turns on."}
    vals = [p[1] for p in history or [] if _num(p[1])]
    typical = sorted(vals)[len(vals) // 2] if vals else (value if _num(value) else None)
    if kind == "temperature":
        f = "F" in unit or "F" in typ.upper().split("_")[-1:]
        return {**base, "op": "above", "limit": 95 if f else 35, "minutes": 5,
                "why": "Above 35 °C a closed restroom is too hot (fan or aircon problem, or a fire nearby)."}
    if kind == "humidity":
        return {**base, "op": "above", "limit": 85, "level": "report", "minutes": 10,
                "why": "Very damp air for long: wet floor, leak, or the exhaust fan is off."}
    if kind == "iaq":
        return {**base, "op": "above", "limit": 200, "minutes": 3,
                "why": "BME680 air quality index: 0–100 good, 151–200 bad, above 200 very bad."}
    if kind == "ammonia":
        return {**base, "op": "above", "limit": 25, "minutes": 3, "why": "Ammonia above 25 ppm smells strongly (urine); clean and ventilate."}
    if kind == "co2":
        return {**base, "op": "above", "limit": 1500, "level": "report", "minutes": 10, "why": "CO₂ above 1500 ppm: poor ventilation."}
    if kind == "gas":
        # BME680 gas resistance: the reading DROPS when the air gets worse (ammonia, VOCs).
        ppm = unit == "ppm" and not re.search(r"resist|ohm|bme", f"{name} {label}".lower())
        if ppm:
            return {**base, "op": "above", "limit": _nice_round((typical or 100) * 1.5), "minutes": 3,
                    "why": "Gas concentration well above its usual level."}
        limit = _nice_round(typical * 0.5) if _num(typical) and typical > 0 else 20000
        return {**base, "op": "below", "limit": limit, "minutes": 3,
                "why": "BME680 gas resistance drops when the air gets worse. The suggested limit is half of its "
                       "usual reading — watch the chart for a day and adjust."}
    if kind in ("soap", "paper", "water"):
        return {**base, "op": "below", "limit": 15 if unit == "%" or not _num(typical) or typical <= 100 else _nice_round(typical * 0.15),
                "level": "report", "minutes": 5, "why": "Running low: refill before it's empty."}
    if _num(typical):
        return {**base, "op": "above", "limit": _nice_round(typical * 1.3 if typical else 1), "level": "report",
                "why": "Set the limit where this value means trouble."}
    return {**base, "op": "above", "limit": 0, "level": "report", "why": ""}


class SensorAlerts:
    def __init__(self, service, settings_file: Path, clock: Callable[[], float] = time.time):
        self.svc = service
        self.file = settings_file
        self.clock = clock
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self.last_check: Optional[float] = None
        self.last_error: Optional[str] = None
        self.why: dict[str, str] = {}        # rule id → why it did / didn't alert (shown to admins)
        self._tested: dict[str, float] = {}

    # ------------------------------------------------------------------ settings file
    def _load(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            if isinstance(data, dict):
                data.setdefault("rules", [])
                data.setdefault("state", {})
                return data
        except (OSError, ValueError):
            pass
        return {"rules": [], "state": {}}

    def _save(self, data: dict) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, self.file)

    def rules(self) -> list[dict]:
        return self._load()["rules"]

    # ------------------------------------------------------------------ admin
    def save_rule(self, data: dict, by: str = "") -> dict:
        op = data.get("op")
        if op not in OPS:
            raise SensorAlertError("Choose when to alert (above, below, on or off).")
        limit = data.get("limit")
        if op in ("above", "below"):
            if isinstance(limit, str):
                try:
                    limit = float(limit.replace(",", ""))
                except ValueError:
                    limit = None
            if not _num(limit) or abs(limit) > 1e9:
                raise SensorAlertError("Type the limit as a number.")
            limit = int(limit) if float(limit).is_integer() else round(float(limit), 4)
        else:
            limit = None
        level = data.get("level")
        if level not in LEVELS:
            raise SensorAlertError("Choose what happens (emergency, urgent or report only).")
        try:
            minutes = int(data.get("minutes", 0))
        except (TypeError, ValueError):
            raise SensorAlertError("Minutes must be a whole number.") from None
        if not 0 <= minutes <= 120:
            raise SensorAlertError("Minutes must be between 0 and 120.")
        thing_id, prop_id = str(data.get("thingId") or "")[:80], str(data.get("propertyId") or "")[:80]
        if not thing_id or not prop_id:
            raise SensorAlertError("Pick the sensor value.")
        location = " ".join(str(data.get("location") or "").split())[:60]
        with self._lock:
            saved = self._load()
            rules = saved["rules"]
            rule_id = str(data.get("id") or "")
            old = next((r for r in rules if r["id"] == rule_id), None) if rule_id else None
            if rule_id and old is None:
                raise SensorAlertError("That alert was deleted. Make it again.")
            if old is None and len(rules) >= MAX_RULES:
                raise SensorAlertError(f"At most {MAX_RULES} sensor alerts.")
            rule = {
                "id": rule_id or generate_id()[:12], "thingId": thing_id, "propertyId": prop_id,
                "variable": str(data.get("variable") or "")[:80], "label": str(data.get("label") or "")[:60] or "Sensor",
                "thing": str(data.get("thing") or "")[:80], "unit": str(data.get("unit") or "")[:12],
                "op": op, "limit": limit, "level": level, "minutes": minutes, "location": location,
                "locationCode": str(data.get("locationCode") or "")[:12] or None,
                "on": bool(data.get("on", True)),
                "kind": kind_of(str(data.get("variable") or ""), str(data.get("label") or "")),
                "createdAt": (old or {}).get("createdAt") or iso(utcnow()), "updatedAt": iso(utcnow()),
                "by": by[:40],
            }
            if old is not None:
                rules[rules.index(old)] = rule
                st = saved["state"].get(rule["id"]) or {}
                st.pop("since", None)          # new limit: start counting again
                saved["state"][rule["id"]] = st
            else:
                rules.append(rule)
            self._save(saved)
        return rule

    def delete_rule(self, rule_id: str) -> dict:
        with self._lock:
            saved = self._load()
            rule = next((r for r in saved["rules"] if r["id"] == rule_id), None)
            if rule is None:
                raise SensorAlertError("That alert was already deleted.")
            saved["rules"].remove(rule)
            saved["state"].pop(rule_id, None)
            self._save(saved)
        return rule

    def describe(self, rule: dict) -> str:
        """"Temperature above 35 °C for 5 min" """
        what = f"{rule['label']} {OPS[rule['op']]}"
        if rule["op"] in ("above", "below"):
            what += f" {show(rule['limit'], rule.get('unit', ''))}"
        if rule.get("minutes"):
            what += f" for {rule['minutes']} min"
        return what

    def overview(self) -> dict:
        """Rules with their live state, for the admin screens."""
        saved = self._load()
        out = []
        for r in saved["rules"]:
            st = saved["state"].get(r["id"]) or {}
            out.append({**r, "text": self.describe(r), "levelText": LEVELS[r["level"]]["about"],
                        "state": self._state_word(r, st), "since": st.get("since"),
                        "active": st.get("active"), "lastValue": st.get("lastValue"),
                        "why": self.why.get(r["id"]) or ("Switched off." if not r.get("on") else "Not checked yet.")})
        return {"rules": out, "levels": [{"code": k, "label": v["label"], "about": v["about"]} for k, v in LEVELS.items()],
                "checker": self.checker()}

    def checker(self) -> dict:
        """Is the background check running? {"running", "ago" (seconds since the last check), "error"}"""
        alive = bool(self._thread and self._thread.is_alive())
        ago = None if self.last_check is None else max(0, round(self.clock() - self.last_check))
        return {"running": alive, "ago": ago, "error": self.last_error, "every": CHECK_SECONDS}

    @staticmethod
    def _state_word(rule: dict, st: dict) -> str:
        if not rule.get("on"):
            return "off"
        if st.get("active"):
            return "alert"
        if st.get("since"):
            return "over"     # over the limit, waiting for the minutes to pass
        return "ok"

    def for_screens(self) -> dict:
        """{property id: [{op, limit, level, state, ref}]} — tiles turn red while over a limit."""
        saved = self._load()
        out: dict[str, list] = {}
        for r in saved["rules"]:
            if not r.get("on"):
                continue
            st = saved["state"].get(r["id"]) or {}
            out.setdefault(r["propertyId"], []).append({
                "id": r["id"], "op": r["op"], "limit": r["limit"], "level": r["level"], "text": self.describe(r),
                "state": self._state_word(r, st), "ref": (st.get("active") or {}).get("ref"), "why": self.why.get(r["id"], "")})
        return out

    # ------------------------------------------------------------------ checking
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def loop():
            while True:
                time.sleep(CHECK_SECONDS)
                try:
                    self.svc.run_job("sensor_alerts", self.check)
                except Exception:  # noqa: BLE001 - shown in System health; try again next round
                    log.warning("Sensor alert check failed; will retry", exc_info=True)

        self._thread = threading.Thread(target=loop, daemon=True, name="sensor-alerts")
        self._thread.start()
        log.info("Sensor alerts: checking %d alert(s) every %d s", len(self.rules()), CHECK_SECONDS)

    @staticmethod
    def _breach(rule: dict, value) -> Optional[bool]:
        """True = over the limit, False = fine, None = can't tell (no value)."""
        if rule["op"] in ("on", "off"):
            b = _bool(value)
            return None if b is None else (b if rule["op"] == "on" else not b)
        if not _num(value):
            return None
        return value > rule["limit"] if rule["op"] == "above" else value < rule["limit"]

    @staticmethod
    def _back_to_normal(rule: dict, value) -> bool:
        """Clearly fine again: a small margin keeps a value hovering at the limit from flapping."""
        if rule["op"] in ("on", "off"):
            return SensorAlerts._breach(rule, value) is False
        if not _num(value):
            return False
        margin = max(abs(rule["limit"]) * 0.02, 1e-9)
        return value < rule["limit"] - margin if rule["op"] == "above" else value > rule["limit"] + margin

    def check(self) -> int:
        """Compare live values with the limits. Returns how many new reports were made."""
        with self._lock:
            saved = self._load()
            rules = [r for r in saved["rules"] if r.get("on")]
            if not rules or not self.svc.arduino.configured:
                self.last_check = self.clock()
                self.last_error = None if self.svc.arduino.configured else "Arduino Cloud isn't connected."
                return 0
            try:
                data = self.svc.arduino.readings()
            except Exception as exc:
                self.last_error = f"Can't read the sensors: {exc}"
                raise
            values = {(v["thingId"], v["id"]): v for t in data.get("things", []) for v in t["properties"]}
            now = self.clock()
            made = 0
            changed = False
            errors = []
            for rule in rules:
                st = saved["state"].setdefault(rule["id"], {})
                before = json.dumps(st, sort_keys=True)
                try:
                    made += self._check_rule(rule, st, values.get((rule["thingId"], rule["propertyId"])), now)
                except Exception as exc:  # noqa: BLE001 - one broken alert must not stop the others
                    log.exception("Sensor alert %s failed", rule["id"])
                    self.why[rule["id"]] = f"Error while checking: {exc}"[:200]
                    errors.append(f"{rule['label']}: {exc}")
                changed = changed or json.dumps(st, sort_keys=True) != before
            if changed or made:
                self._save(saved)
            self.last_check, self.last_error = now, ("; ".join(errors)[:300] or None)
            return made

    def _report_status(self, report_id: str) -> Optional[str]:
        r = self.svc.store.get(self.svc.C_REPORTS, report_id)
        return r.get("status") if r else None

    def _check_rule(self, rule: dict, st: dict, v: Optional[dict], now: float) -> int:
        rid, unit = rule["id"], rule.get("unit", "")
        if v is None:
            self.why[rid] = "This value isn't on Arduino Cloud any more (renamed or deleted?). Make the alert again."
            return 0
        age = _age_seconds(v.get("updatedAt"), now)
        if age is not None and age > STALE_SECONDS:
            # No fresh reading (device off?): don't alert on an old value, don't clear either.
            self.why[rid] = (f"Not checked: the last reading is {_age_text(age)} old. Is the device on and online? "
                             f"Alerts only use readings from the last {STALE_SECONDS // 3600} hours.")
            return 0
        value = v.get("value")
        breach = self._breach(rule, value)
        if breach is None:
            self.why[rid] = "No reading yet." if value is None else f"Can't compare “{str(value)[:20]}” with the limit."
            return 0
        now_text = show(value, unit)
        st["lastValue"] = value
        active = st.get("active")
        if active:
            status = self._report_status(active["reportId"])
            if status != "resolved" and status is not None:
                self.why[rid] = f"Alert sent: report #{active.get('ref')} is still open (now {now_text})."
                if breach:
                    st.pop("clearSince", None)
                    worse = (value > active.get("peak", value)) if rule["op"] == "above" else (value < active.get("peak", value)) if rule["op"] == "below" else False
                    if worse:
                        active["peak"] = value
                elif self._back_to_normal(rule, value):
                    st["clearSince"] = st.get("clearSince") or now
                    if now - st["clearSince"] >= CLEAR_SECONDS:
                        self._note(active["reportId"], f"Back to normal: {rule['label']} {show(value, rule.get('unit', ''))}"
                                   + (f" (worst was {show(active.get('peak'), rule.get('unit', ''))})." if rule["op"] in ("above", "below") else "."))
                        st.pop("active", None)
                        st.pop("clearSince", None)
                        st.pop("since", None)
                        st["openReport"] = active["reportId"]   # over again while still open → a note, not a new report
                return 0
            # The job was marked done (or the report deleted).
            st.pop("active", None)
            st.pop("clearSince", None)
            st.pop("openReport", None)
            st["quietUntil"] = now + AFTER_DONE_SECONDS if breach else 0
            st.pop("since", None)
        if not breach:
            if self._back_to_normal(rule, value):
                st.pop("since", None)
            self.why[rid] = f"Normal: now {now_text}."
            return 0
        st["since"] = st.get("since") or now
        held, need = now - st["since"], rule.get("minutes", 0) * 60
        if held < need:
            self.why[rid] = f"Over the limit (now {now_text}) for {_age_text(held)} — a report after {rule['minutes']} min."
            return 0
        if now < (st.get("quietUntil") or 0):
            left = st["quietUntil"] - now
            self.why[rid] = f"Over the limit (now {now_text}), but the last report was just marked done: waiting {_age_text(left)} more."
            return 0
        # Over the limit long enough.
        reopen = st.get("openReport")
        if reopen and self._report_status(reopen) not in (None, "resolved"):
            ref = (self.svc.store.get(self.svc.C_REPORTS, reopen) or {}).get("smsRef")
            self._note(reopen, f"Over the limit again: {rule['label']} {show(value, rule.get('unit', ''))}.")
            st["active"] = {"reportId": reopen, "ref": ref, "at": iso(utcnow()), "peak": value}
            self.why[rid] = f"Over the limit again: added to the open report #{ref}."
            return 0
        st.pop("openReport", None)
        if rule["level"] != "emergency" and self._recent_sensor_reports() >= MAX_REPORTS_PER_HOUR:
            if not st.get("throttled"):
                st["throttled"] = True
                self.svc.audit("sensor_alert_held", {"type": "system", "username": "sensors"}, rule["id"],
                               {"reason": "too many sensor reports this hour", "rule": self.describe(rule)})
            self.why[rid] = f"Held back: already {MAX_REPORTS_PER_HOUR} sensor reports in the last hour."
            return 0
        st.pop("throttled", None)
        report = self._make_report(rule, v, value, now - st["since"])
        st["active"] = {"reportId": report["id"], "ref": report["smsRef"], "at": report["createdAt"], "peak": value}
        st.pop("quietUntil", None)
        self.why[rid] = f"Alert sent: report #{report['smsRef']} (now {now_text})."
        log.info("Sensor alert: %s → report #%s", report["title"], report["smsRef"])
        return 1

    def send_test(self, rule_id: str, by: str) -> dict:
        """Make a report and send the texts now, as if the limit was crossed — to check the whole chain."""
        rule = next((r for r in self.rules() if r["id"] == rule_id), None)
        if rule is None:
            raise SensorAlertError("That alert was deleted.")
        if self.clock() - self._tested.get(rule_id, 0) < 60:
            raise SensorAlertError("A test was just sent. Wait a minute before the next one.")
        self._tested[rule_id] = self.clock()
        v, value = {}, None
        try:
            data = self.svc.arduino.readings()
            v = next((x for t in data["things"] for x in t["properties"]
                      if x["thingId"] == rule["thingId"] and x["id"] == rule["propertyId"]), {}) or {}
            value = v.get("value")
        except Exception:  # noqa: BLE001 - a test works without a reading too
            pass
        if value is None or self._breach(rule, value) is None:
            value = rule["limit"] if rule["op"] in ("above", "below") else (rule["op"] == "on")
        return self._make_report(rule, v, value, 0, test_by=by)

    def _recent_sensor_reports(self) -> int:
        hour_ago = iso(datetime.fromtimestamp(self.clock() - 3600, timezone.utc))
        return sum(1 for _, r in self.svc.store.query(self.svc.C_REPORTS, source="sensor") if r.get("createdAt", "") >= hour_ago)

    def _note(self, report_id: str, text: str) -> None:
        try:
            self.svc._add_report_note(report_id, {"type": "system", "username": "sensors"}, text)
        except Exception:  # noqa: BLE001 - the report may have been deleted
            pass

    def _make_report(self, rule: dict, v: dict, value, held: float, test_by: str = "") -> dict:
        from .two_way import alert_text

        unit = rule.get("unit") or v.get("unit") or ""
        kind = rule.get("kind") or kind_of(rule.get("variable", ""), rule["label"])
        level = LEVELS[rule["level"]]
        if rule["op"] == "above":
            title = f"{rule['label']} too high: {show(value, unit)}"
        elif rule["op"] == "below":
            title = f"{rule['label']} too low: {show(value, unit)}"
        else:
            title = f"{rule['label']} detected" if rule["op"] == "on" else f"{rule['label']} is off"
        limit = f"limit {OPS[rule['op']]} {show(rule['limit'], unit)}" if rule["op"] in ("above", "below") else ""
        mins = int(held // 60)
        details = "Sensor alert" + (f" ({limit}" + (f", for {mins} min" if mins >= 1 else "") + ")" if limit else "") + "."
        if rule.get("thing"):
            details += f" From {rule['thing']}."
        if test_by:
            title = f"TEST {title}"
            details = f"Test alert sent by {test_by} — nothing is wrong. " + details
        category = "emergency" if rule["level"] == "emergency" else CATEGORY.get(kind, "other")
        issues = ["emergency"] if rule["level"] == "emergency" else ISSUES.get(kind, [])
        report = self.svc._save_report(
            category=category, title=title[:80], location=rule.get("location") or "", details=details[:500],
            priority=level["priority"], created_by={"type": "system", "username": "sensors"},
            extra={"source": "sensor", "channel": "arduino", "issues": issues,
                   "urgency": "high" if rule["level"] != "report" else "medium",
                   "locationCode": rule.get("locationCode"),
                   "sensor": {"ruleId": rule["id"], "thingId": rule["thingId"], "propertyId": rule["propertyId"],
                              "label": rule["label"], "variable": rule.get("variable", ""), "value": value,
                              "unit": unit, "op": rule["op"], "limit": rule["limit"], "rule": self.describe(rule),
                              **({"test": True} if test_by else {})}})
        self.svc.audit("sensor_alert", {"type": "system", "username": "sensors"}, report["id"],
                       {"rule": self.describe(rule), "value": str(value)[:30], "level": rule["level"]})
        if level["notify"] != "none":
            text = alert_text(("TEST " if test_by else "") + level["sms"], report["smsRef"], report["title"].removeprefix("TEST "),
                              report["location"],
                              limit[:1].upper() + limit[1:] if limit else "")
            report["texted"] = self.svc.alert_people(report["id"], "emergency" if level["notify"] == "everyone" else "janitors",
                                                     text, force=bool(test_by))
        else:
            report["texted"] = 0
        return report
