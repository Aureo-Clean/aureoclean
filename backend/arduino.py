"""Live sensor values from Arduino IoT Cloud, read with an API key.

The app shows them on Home and in the full Live sensors view, so nobody has to
sign in to Arduino Cloud: the Pi asks Arduino for the latest value of every
variable. Every page of your Arduino dashboards (multi-page dashboards included)
becomes a tab with the same name ("Urinal", "Page 2", …) and the same widgets,
drawn in AureoClean's style.

Set up once in the app (Home → Live sensors → Connect Arduino Cloud) with the
Client ID and Client Secret from https://app.arduino.cc/api-keys (Create API key).
They're kept only on this Pi (~/.config/aureoclean/arduino.json, readable by this
user only), or in the config file:

    AUREOCLEAN_ARDUINO_CLIENT_ID=...
    AUREOCLEAN_ARDUINO_CLIENT_SECRET=...
    # optional: AUREOCLEAN_ARDUINO_ORG=<organization id>  (only for an organization space)
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

API = "https://api2.arduino.cc"
TOKEN_URL = f"{API}/iot/v1/clients/token"
CACHE_SECONDS = 4          # several screens asking at once share one answer
STALE_OK_SECONDS = 60      # older than CACHE_SECONDS: answer at once with the last values, refresh in the background
THINGS_SECONDS = 60        # the list of things changes rarely
LAYOUT_SECONDS = 60        # dashboards (pages and widgets) change rarely
HISTORY_SECONDS = 180      # small charts: last few hours, refreshed every 3 minutes
HISTORY_HOURS = 6
HISTORY_POINTS = 60
HISTORY_MAX_SERIES = 16
UNITS = {                  # Arduino Cloud variable types → unit shown after the number
    "TEMPERATURE_C": "°C", "TEMPERATURE": "°C", "TEMPERATURE_F": "°F", "HUMIDITY": "%", "RELATIVE_HUMIDITY": "%",
    "PERCENTAGE": "%", "LENGTH_C": "cm", "LENGTH_M": "m", "LENGTH_MM": "mm", "VOLUME_L": "L", "VOLUME_ML": "mL",
    "LUMINANCE": "lx", "ILLUMINANCE": "lx", "PRESSURE": "hPa", "DISTANCE": "cm", "VOLTAGE": "V", "CURRENT": "A",
    "POWER": "W", "ENERGY": "kWh", "FLOW_RATE": "L/min", "CONCENTRATION": "ppm", "GAS": "ppm", "SOUND": "dB",
    "HOME_TEMPERATURE_C": "°C", "HOME_TEMPERATURE_F": "°F",
}


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _simple_options(options) -> dict:
    """Widget settings worth showing (min/max, step, note text …); nothing nested."""
    out = {}
    for k, v in (options or {}).items() if isinstance(options, dict) else ():
        if isinstance(v, bool) or _is_number(v):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:300]
    return out


class ArduinoError(Exception):
    pass


def unit_from_name(variable: str) -> str:
    """Plain Float/Int variables have no unit type in Arduino Cloud: guess it from the name
    (temperature → °C, humidity → %, gasResistance → kΩ, tofDistanceCm → cm …)."""
    n = (variable or "").lower()
    for pattern, unit in ((r"temp", "°C"), (r"humid", "%"), (r"pressure", "hPa"), (r"gasresistance|gasbaseline", "kΩ"),
                          (r"cm$|distancecm", "cm"), (r"co2", "ppm"), (r"voc", "ppm"), (r"ammonia|nh3|ppm$", "ppm")):
        if re.search(pattern, n):
            return unit
    return ""


def nice_name(variable: str) -> str:
    """waterLevel / water_level → Water level"""
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", variable or "").replace("_", " ").split()
    text = " ".join(words).lower()
    return text[:1].upper() + text[1:] if text else variable


class ArduinoCloud:
    def __init__(self, settings_file: Path, http=None):
        self.file = settings_file
        self.http = http or requests.Session()
        self._token: Optional[str] = None
        self._token_until = 0.0
        self._cache: Optional[tuple[float, dict]] = None
        self._layout: Optional[tuple[float, list]] = None
        self._history: Optional[tuple[float, dict]] = None
        self._lock = threading.Lock()
        self._fetch_lock = threading.Lock()
        self._refreshing = False
        self._things: Optional[tuple[float, list]] = None
        self.last_error: Optional[str] = None
        self._history_lock = threading.Lock()
        self._series_cache: dict = {}

    # ------------------------------------------------------------------ settings
    def _saved(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def settings(self) -> dict:
        saved = self._saved()
        return {
            "clientId": saved.get("clientId") or os.environ.get("AUREOCLEAN_ARDUINO_CLIENT_ID", "").strip(),
            "clientSecret": saved.get("clientSecret") or os.environ.get("AUREOCLEAN_ARDUINO_CLIENT_SECRET", "").strip(),
            "org": saved.get("org") or os.environ.get("AUREOCLEAN_ARDUINO_ORG", "").strip(),
            "janitorsControl": bool(saved.get("janitorsControl", False)),
        }

    @property
    def configured(self) -> bool:
        s = self.settings()
        return bool(s["clientId"] and s["clientSecret"])

    def save(self, client_id: str, client_secret: str, org: str = "") -> None:
        client_id, client_secret, org = (client_id or "").strip(), (client_secret or "").strip(), (org or "").strip()
        if len(client_id) < 8 or len(client_secret) < 8:
            raise ArduinoError("Paste both the Client ID and the Client Secret from Arduino Cloud → API keys.")
        self.file.parent.mkdir(parents=True, exist_ok=True)
        keep = {"janitorsControl": bool(self._saved().get("janitorsControl", False))}
        self.file.write_text(json.dumps({"clientId": client_id, "clientSecret": client_secret, "org": org, **keep}, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass
        self._token, self._cache, self._layout, self._history, self._things = None, None, None, None, None

    def set_options(self, janitors_control: bool) -> None:
        data = self._saved()
        if not data:
            raise ArduinoError("Connect Arduino Cloud in the app first.")
        data["janitorsControl"] = bool(janitors_control)
        self.file.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.file.unlink()
        except OSError:
            pass
        self._token, self._cache, self._layout, self._history, self._things = None, None, None, None, None

    # ------------------------------------------------------------------ API
    def _headers(self) -> dict:
        org = self.settings()["org"]
        return {"Authorization": f"Bearer {self._get_token()}", **({"X-Organization": org} if org else {})}

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_until:
            return self._token
        s = self.settings()
        if not (s["clientId"] and s["clientSecret"]):
            raise ArduinoError("Arduino Cloud isn't connected yet.")
        try:
            resp = self.http.post(TOKEN_URL, data={
                "grant_type": "client_credentials", "client_id": s["clientId"], "client_secret": s["clientSecret"],
                "audience": f"{API}/iot"}, headers={"X-Organization": s["org"]} if s["org"] else None, timeout=15)
        except requests.RequestException as exc:
            raise ArduinoError("Can't reach Arduino Cloud. Check the Pi's internet.") from exc
        if resp.status_code in (400, 401, 403):
            raise ArduinoError("Arduino Cloud refused the API key. Check the Client ID and Client Secret "
                               "(make a new key at app.arduino.cc/api-keys if you lost the secret).")
        if not resp.ok:
            raise ArduinoError(f"Arduino Cloud didn't answer properly ({resp.status_code}). Try again shortly.")
        data = resp.json()
        self._token = data.get("access_token")
        if not self._token:
            raise ArduinoError("Arduino Cloud didn't give an access token.")
        self._token_until = time.monotonic() + max(30, int(data.get("expires_in", 300)) - 30)
        return self._token

    def _get(self, path: str, params: Optional[dict] = None):
        try:
            resp = self.http.get(f"{API}{path}", headers=self._headers(), params=params, timeout=15)
        except requests.RequestException as exc:
            raise ArduinoError("Can't reach Arduino Cloud. Check the Pi's internet.") from exc
        if resp.status_code == 401:
            self._token = None
            raise ArduinoError("Arduino Cloud refused the API key. Check it under Home → Live sensors.")
        if resp.status_code == 403:
            raise ArduinoError("This API key isn't allowed to read the things. If they're in an organization space, "
                               "add its Organization ID when connecting.")
        if resp.status_code == 429:
            raise ArduinoError("Arduino Cloud is busy (too many requests). The values update again in a moment.")
        if not resp.ok:
            raise ArduinoError(f"Arduino Cloud error ({resp.status_code}).")
        return resp.json()

    def publish(self, thing_id: str, prop_id: str, value) -> None:
        """Send a new value to a variable (a switch, slider … on the dashboard)."""
        path = f"/iot/v2/things/{thing_id}/properties/{prop_id}/publish"
        try:
            resp = self.http.put(f"{API}{path}", headers=self._headers(), json={"value": value}, timeout=15)
        except requests.RequestException as exc:
            raise ArduinoError("Can't reach Arduino Cloud. Check the Pi's internet.") from exc
        if resp.status_code == 401:
            self._token = None
            raise ArduinoError("Arduino Cloud refused the API key. Check it under Live sensors → Arduino settings.")
        if resp.status_code == 403:
            raise ArduinoError("This API key isn't allowed to change that value.")
        if resp.status_code == 429:
            raise ArduinoError("Arduino Cloud is busy (too many requests). Try again in a moment.")
        if resp.status_code >= 400:
            raise ArduinoError(f"Arduino Cloud didn't accept the value ({resp.status_code}).")
        # Show the new value at once on every screen (the device confirms it on the next reading).
        with self._lock:
            if self._cache:
                for t in self._cache[1]["things"]:
                    for v in t["properties"]:
                        if v["id"] == prop_id and t["id"] == thing_id:
                            v["value"] = value
                            v["updatedAt"] = datetime.now(timezone.utc).isoformat()
        with self._history_lock:
            self._history = None if self._history and prop_id in self._history[1].get("series", {}) else self._history

    def series(self, thing_id: str, prop_id: str, hours: int) -> list:
        """One variable's values for the last 1/6/24 hours (the detail chart), [[unix time, value], …]."""
        hours = hours if hours in (1, 6, 24, 72) else 6
        key = (thing_id, prop_id, hours)
        hit = self._series_cache.get(key)
        if hit and time.monotonic() - hit[0] < 60:
            return hit[1]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        raw = self._get(f"/iot/v2/things/{thing_id}/properties/{prop_id}/timeseries", {
            "from": (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z"),
            "to": now.isoformat().replace("+00:00", "Z"), "interval": max(30, int(hours * 3600 / 120))}) or {}
        points = []
        for d in raw.get("data") or []:
            try:
                points.append([round(datetime.fromisoformat(str(d["time"]).replace("Z", "+00:00")).timestamp()), float(d["value"])])
            except (KeyError, TypeError, ValueError):
                continue
        points.sort()
        if len(self._series_cache) > 50:
            self._series_cache.clear()
        self._series_cache[key] = (time.monotonic(), points)
        return points

    @staticmethod
    def _value(p: dict, thing: dict) -> dict:
        name = p.get("variable_name") or p.get("name") or ""
        return {
            "id": p.get("id"), "thingId": thing.get("id"), "thing": thing.get("name") or "",
            "name": name, "label": nice_name(name), "type": p.get("type") or "",
            "value": p.get("last_value"),
            "unit": UNITS.get((p.get("type") or "").upper(), "") or unit_from_name(name),
            "updatedAt": p.get("value_updated_at") or p.get("last_value_updated_at") or p.get("updated_at"),
            "writable": (p.get("permission") or "").upper() == "READ_WRITE",
        }

    def _dashboards(self) -> list:
        """Arduino dashboards with their pages and widgets (kept for a minute).

        The v3 API knows multi-page dashboards (each widget has a page_id); older
        accounts/APIs without it fall back to v2 (one page per dashboard)."""
        if self._layout and time.monotonic() - self._layout[0] < LAYOUT_SECONDS:
            return self._layout[1]
        boards, version = None, "v3"
        for version in ("v3", "v2"):
            try:
                boards = self._get(f"/iot/{version}/dashboards") or []
                break
            except ArduinoError as exc:
                if "Can't reach" in str(exc) or "busy" in str(exc):
                    raise
                if version == "v2":
                    if "isn't allowed" in str(exc):
                        boards = []      # key can read things but not dashboards: one page per thing
                        break
                    raise
        full = []
        for b in boards or []:
            if not isinstance(b, dict):
                continue
            if "widgets" not in b and b.get("id"):
                b = self._get(f"/iot/{version}/dashboards/{b['id']}") or b
            full.append(b)
        self._layout = (time.monotonic(), full)
        return full

    @staticmethod
    def _widget(w: dict, by_id: dict, by_name: dict) -> dict:
        values = []
        for lv in w.get("variables") or []:
            if not isinstance(lv, dict):
                continue
            live = by_id.get(lv.get("id")) or by_name.get((lv.get("thing_id"), lv.get("variable_name")))
            if live is None:   # not in the things list (e.g. shared from another account)
                live = ArduinoCloud._value({**lv, "last_value": lv.get("last_value")},
                                           {"id": lv.get("thing_id"), "name": lv.get("thing_name")})
            values.append(live)
        name = (w.get("name") or "").strip()
        if not name or any(name == v["name"] for v in values):   # Arduino names widgets after the variable
            name = values[0]["label"] if values else nice_name(w.get("type") or "")
        return {
            "id": w.get("id"), "type": re.sub(r"[^a-z]", "", (w.get("type") or "").lower()),  # "Push Button" → pushbutton
            "name": name,
            "x": w.get("x", 0), "y": w.get("y", 0), "w": w.get("width", 1), "h": w.get("height", 1),
            "options": _simple_options(w.get("options")), "values": values,
        }

    @staticmethod
    def _pages(boards: list, things: list) -> list:
        """One entry per Arduino dashboard page, in Arduino's order:
        {"id", "name" (page name), "dashboard", "dashboardId", "icon", "widgets"}."""
        by_id = {v["id"]: v for t in things for v in t["properties"]}
        by_name = {(v["thingId"], v["name"]): v for t in things for v in t["properties"]}
        pages = []
        for b in boards:   # same order as Arduino Cloud lists them
            board_name = b.get("name") or "Dashboard"
            widgets = sorted(b.get("widgets") or [], key=lambda w: (w.get("y", 0), w.get("x", 0)))
            board_pages = sorted((p for p in b.get("pages") or [] if isinstance(p, dict) and p.get("id")),
                                 key=lambda p: (p.get("position", 0), p.get("name") or ""))
            if not board_pages:          # single-page dashboard
                pages.append({"id": b.get("id"), "name": board_name, "dashboard": board_name, "dashboardId": b.get("id"),
                              "icon": None, "widgets": [ArduinoCloud._widget(w, by_id, by_name) for w in widgets]})
                continue
            known = {p["id"] for p in board_pages}
            first = board_pages[0]["id"]
            for i, p in enumerate(board_pages):
                mine = [w for w in widgets if (w.get("page_id") if w.get("page_id") in known else first) == p["id"]]
                pages.append({"id": f"{b.get('id')}/{p['id']}", "name": p.get("name") or f"Page {i + 1}",
                              "dashboard": board_name, "dashboardId": b.get("id"), "icon": p.get("icon"),
                              "widgets": [ArduinoCloud._widget(w, by_id, by_name) for w in mine]})
        return pages

    def readings(self) -> dict:
        """The readings, with newer values from the Firebase Realtime Database put in (if connected)."""
        data = self._readings()
        feed = getattr(self, "feed", None)
        return feed.overlay(data) if feed is not None else data

    def _readings(self) -> dict:
        """{"things": [{"id", "name", "device", "properties": [...]}],
            "pages": [{"id", "name", "widgets": [{"id", "type", "name", "options", "values": [...]}]}],
            "fetchedAt"}.

        Never makes a screen wait when there are recent values: answers at once with the last values
        and refreshes them in the background (one fetch at a time, things read in parallel).

        "pages" are the pages of the Arduino dashboards (a single-page dashboard is one page named after
        the dashboard). Without any dashboard, there is one page per thing."""
        hit = self._cache
        if hit:
            age = time.monotonic() - hit[0]
            if age < CACHE_SECONDS:
                return hit[1]
            if age < STALE_OK_SECONDS:
                self._refresh_in_background()
                return hit[1]
        return self._fetch_now()

    def _refresh_in_background(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True

        def work():
            try:
                self._fetch_now()
            except Exception as exc:  # noqa: BLE001 - the next caller sees it once the values are too old
                self.last_error = str(exc)
            finally:
                self._refreshing = False
        threading.Thread(target=work, daemon=True, name="arduino-refresh").start()

    def _fetch_now(self) -> dict:
        with self._fetch_lock:
            hit = self._cache
            if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
                return hit[1]
            result = self._fetch()
            with self._lock:
                self._cache = (time.monotonic(), result)
            self.last_error = None
            return result

    def _thing_list(self) -> list:
        if self._things and time.monotonic() - self._things[0] < THINGS_SECONDS:
            return self._things[1]
        things = [t for t in (self._get("/iot/v2/things") or []) if isinstance(t, dict)]
        self._things = (time.monotonic(), things)
        return things

    def _fetch(self) -> dict:
        raw = self._thing_list()
        with ThreadPoolExecutor(max_workers=4) as pool:
            props = list(pool.map(lambda t: self._get(f"/iot/v2/things/{t['id']}/properties") or [], raw))
        things = [{
            "id": t.get("id"), "name": t.get("name") or "Thing",
            "device": t.get("device_name") or (t.get("device") or {}).get("name"),
            "properties": sorted((self._value(p, t) for p in ps), key=lambda x: x["label"].lower()),
        } for t, ps in zip(raw, props)]
        pages = self._pages(self._dashboards(), things)
        if not pages:
            pages = [{"id": f"thing-{t['id']}", "name": t["name"], "dashboard": "", "dashboardId": None, "fromThing": True,
                      "widgets": [{"id": v["id"], "type": "", "name": v["label"], "x": i % 4, "y": i // 4, "w": 1, "h": 1,
                                   "options": {}, "values": [v]} for i, v in enumerate(t["properties"])]}
                     for t in things if t["properties"]]
        return {"things": things, "pages": pages, "fetchedAt": time.time()}

    def history(self) -> dict:
        """Recent values of the number variables shown on the pages, for the small charts:
        {"series": {property id: [[unix time, value], …]}, "hours": 6}."""
        with self._history_lock:
            if self._history and time.monotonic() - self._history[0] < HISTORY_SECONDS:
                return self._history[1]
            data = self.readings()
            wanted, seen = [], set()
            charts_first = sorted((w for p in data["pages"] for w in p["widgets"]), key=lambda w: "chart" not in w["type"])
            for w in charts_first:
                for v in w["values"]:
                    if v["id"] and v["thingId"] and _is_number(v["value"]) and v["id"] not in seen:
                        seen.add(v["id"])
                        wanted.append(v)
            wanted = wanted[:HISTORY_MAX_SERIES]
            now = datetime.now(timezone.utc).replace(microsecond=0)
            params = {"from": (now - timedelta(hours=HISTORY_HOURS)).isoformat().replace("+00:00", "Z"),
                      "to": now.isoformat().replace("+00:00", "Z"),
                      "interval": int(HISTORY_HOURS * 3600 / HISTORY_POINTS)}

            def one(v):
                try:
                    raw = self._get(f"/iot/v2/things/{v['thingId']}/properties/{v['id']}/timeseries", params) or {}
                except ArduinoError:
                    return v["id"], []
                points = []
                for d in raw.get("data") or []:
                    try:
                        t = datetime.fromisoformat(str(d["time"]).replace("Z", "+00:00")).timestamp()
                        points.append([round(t), float(d["value"])])
                    except (KeyError, TypeError, ValueError):
                        continue
                return v["id"], sorted(points)[-HISTORY_POINTS:]

            with ThreadPoolExecutor(max_workers=4) as pool:
                series = {pid: pts for pid, pts in pool.map(one, wanted) if pts}
            result = {"series": series, "hours": HISTORY_HOURS}
            self._history = (time.monotonic(), result)
            return result
