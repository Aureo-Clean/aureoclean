"""System health: one list of checks so nothing breaks silently.

Each check returns {"key", "name", "status": ok|warn|fail|off, "detail"}.
Slow checks (modem signal, SIM balance) are cached so the panel can refresh often
without tying up the modem.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import time
from pathlib import Path
from typing import Optional

from .security import iso, parse_iso, utcnow
from .sms import SmsError

ORDER = {"fail": 0, "warn": 1, "ok": 2, "off": 3}
STARTED = time.time()


def _check(key, name, status, detail):
    return {"key": key, "name": name, "status": status, "detail": detail}


class HealthMonitor:
    def __init__(self, service):
        self.svc = service
        self._cache: dict[str, tuple[float, dict]] = {}
        self.balance_code = os.environ.get("AUREOCLEAN_SIM_BALANCE_USSD", "").strip()
        self.low_balance = float(os.environ.get("AUREOCLEAN_SIM_LOW_BALANCE", "20") or 20)

    def _cached(self, key: str, seconds: float, fn):
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < seconds:
            return hit[1]
        value = fn()
        self._cache[key] = (time.monotonic(), value)
        return value

    # ------------------------------------------------------------------ checks
    def internet(self):
        t0 = time.monotonic()
        try:
            with socket.create_connection(("firestore.googleapis.com", 443), timeout=4):
                pass
            return _check("internet", "Internet", "ok", f"Online ({(time.monotonic() - t0) * 1000:.0f} ms)")
        except OSError as exc:
            return _check("internet", "Internet", "fail", f"Offline: {exc.__class__.__name__}. Check the Wi-Fi.")

    def cloud(self):
        if self.svc.settings.mode != "firebase":
            return _check("cloud", "Firebase", "off", "Local mode: data is stored on this Pi only.")
        t0 = time.monotonic()
        try:
            self.svc.store.get(self.svc.C_COUNTERS, "reportRef")
            return _check("cloud", "Firebase", "ok", f"Connected ({(time.monotonic() - t0) * 1000:.0f} ms)")
        except Exception as exc:  # noqa: BLE001
            return _check("cloud", "Firebase", "fail", str(exc)[:160])

    def modem(self):
        sms = self.svc.sms
        if sms.mode != "modem":
            return _check("modem", "SMS modem", "warn", "Test mode: texts are NOT sent. Tools → Find & use modem.")

        def probe():
            try:
                st = sms.check()
                return _check("modem", "SMS modem", "ok" if st.get("ok") else "fail", st.get("detail", ""))
            except SmsError as exc:
                return _check("modem", "SMS modem", "fail", str(exc))
        return self._cached("modem", 60, probe)

    def balance(self, force: bool = False):
        sms = self.svc.sms
        if sms.mode != "modem":
            return _check("balance", "SIM balance", "off", "Only checked when the GSM modem is on.")
        if not self.balance_code:
            return _check("balance", "SIM balance", "off", "Not set up (optional).")

        def probe():
            try:
                text = sms.ussd(self.balance_code)
            except SmsError as exc:
                return _check("balance", "SIM balance", "warn", f"Couldn't check: {exc}")
            amount = _peso_amount(text)
            status = "warn" if amount is not None and amount < self.low_balance else "ok"
            prefix = f"Low load! ₱{amount:g}. " if status == "warn" else (f"₱{amount:g}. " if amount is not None else "")
            return _check("balance", "SIM balance", status, (prefix + text)[:200])
        if force:
            self._cache.pop("balance", None)
        return self._cached("balance", 3600, probe)

    def promo(self):
        p = self.svc.get_sim_promo()
        if not p["set"]:
            return _check("promo", "SIM promo", "off",
                          "Not set. If the SIM is on a promo (e.g. unli texts), enter it in Tools → SMS → Set promo "
                          "so you're warned before it ends.")
        days, ends = p["daysLeft"], _nice_date(p["expires"])
        if days < 0:
            return _check("promo", "SIM promo", "fail",
                          f"{p['name']} ended on {ends}. Texts may stop going out: register again, then update it here.")
        if days <= 2:
            left = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
            return _check("promo", "SIM promo", "warn", f"{p['name']} ends {left} ({ends}). Register again soon.")
        return _check("promo", "SIM promo", "ok", f"{p['name']}: {days} days left (ends {ends})")

    def sending(self):
        t = self.svc.send_tracker
        if t.failures_in_a_row:
            hint = (" The promo may have run out, or may not cover that network (e.g. a Globe promo "
                    "texting a Smart/TNT number).")
            status = "fail" if t.failures_in_a_row >= 2 else "warn"
            return _check("sending", "Sending texts", status,
                          f"Last {t.failures_in_a_row} text(s) failed — {t.last_error[:120]}.{hint}")
        if t.last_ok_at:
            age = (utcnow() - parse_iso(t.last_ok_at)).total_seconds()
            ago = f"{int(age // 60)} min ago" if age < 3600 else f"{int(age // 3600)} h ago"
            return _check("sending", "Sending texts", "ok", f"Last text went out {ago}.")
        return _check("sending", "Sending texts", "off", "No texts sent since the service started.")

    def jobs(self):
        out = []
        for key, name, every in (("web", "Website sign-ups & reports", 20), ("sms_inbox", "SMS replies", 20)):
            st = self.svc.job_status.get(key)
            if not st:
                out.append(_check(f"job_{key}", name, "warn", "Not run yet (starting up?)."))
                continue
            age = (utcnow() - parse_iso(st["at"])).total_seconds()
            if st.get("error"):
                out.append(_check(f"job_{key}", name, "fail", f"Failing: {st['error'][:140]}"))
            elif age > every * 6:
                out.append(_check(f"job_{key}", name, "warn", f"Last checked {int(age // 60)} min ago."))
            else:
                out.append(_check(f"job_{key}", name, "ok", f"Checked {int(age)} s ago."))
        return out

    def pi(self):
        out = []
        temp = _cpu_temp()
        if temp is not None:
            status = "fail" if temp >= 80 else "warn" if temp >= 70 else "ok"
            out.append(_check("temp", "Pi temperature", status, f"{temp:.0f} °C"
                              + (" — add a fan or heatsink" if status != "ok" else "")))
        free_gb = shutil.disk_usage(Path.home()).free / 1e9
        out.append(_check("disk", "Storage", "fail" if free_gb < 0.5 else "warn" if free_gb < 2 else "ok",
                          f"{free_gb:.1f} GB free"))
        mem = _mem_available_mb()
        if mem is not None:
            out.append(_check("memory", "Memory", "warn" if mem < 200 else "ok", f"{mem:.0f} MB free"))
        up = time.time() - STARTED
        out.append(_check("uptime", "AureoClean service", "ok",
                          f"Running for {int(up // 3600)} h {int(up % 3600 // 60)} min"))
        return out

    # ------------------------------------------------------------------ all
    def report(self, force_balance: bool = False) -> dict:
        checks = [self.internet(), self.cloud(), self.modem(), self.promo(), self.sending()]
        bal = self.balance(force_balance)
        if bal["status"] != "off" or self.balance_code:  # optional: only shown once a balance code is set
            checks.append(bal)
        checks += [*self.jobs(), *self.pi()]
        worst = min((c["status"] for c in checks), key=lambda s: ORDER[s])
        return {"status": worst if worst != "off" else "ok", "checkedAt": iso(utcnow()),
                "problems": sum(c["status"] in ("warn", "fail") for c in checks), "checks": checks}


def _peso_amount(text: str) -> Optional[float]:
    m = re.search(r"(?:P|PHP|₱)\s?(\d{1,6}(?:[.,]\d{1,2})?)", text or "", re.I)
    return float(m.group(1).replace(",", ".")) if m else None


def _cpu_temp() -> Optional[float]:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()) / 1000
    except (OSError, ValueError):
        return None


def _mem_available_mb() -> Optional[float]:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError):
        pass
    return None


def _nice_date(iso_date: str) -> str:
    from datetime import date
    d = date.fromisoformat(iso_date)
    return f"{d:%a %b} {d.day}"
