"""Live sensor values from Arduino IoT Cloud, read with an API key.

The app shows these as simple tiles (Home, Live sensors), so nobody has to sign
in to Arduino Cloud: the Pi asks Arduino for the latest value of every variable.

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
from pathlib import Path
from typing import Optional

import requests

API = "https://api2.arduino.cc"
TOKEN_URL = f"{API}/iot/v1/clients/token"
CACHE_SECONDS = 8          # several screens asking at once share one answer
UNITS = {                  # Arduino Cloud variable types → unit shown after the number
    "TEMPERATURE_C": "°C", "TEMPERATURE": "°C", "TEMPERATURE_F": "°F", "HUMIDITY": "%", "RELATIVE_HUMIDITY": "%",
    "PERCENTAGE": "%", "LENGTH_C": "cm", "LENGTH_M": "m", "LENGTH_MM": "mm", "VOLUME_L": "L", "VOLUME_ML": "mL",
    "LUMINANCE": "lx", "ILLUMINANCE": "lx", "PRESSURE": "hPa", "DISTANCE": "cm", "VOLTAGE": "V", "CURRENT": "A",
    "POWER": "W", "ENERGY": "kWh", "FLOW_RATE": "L/min", "CONCENTRATION": "ppm", "GAS": "ppm", "SOUND": "dB",
}


class ArduinoError(Exception):
    pass


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
        self._lock = threading.Lock()

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
        self.file.write_text(json.dumps({"clientId": client_id, "clientSecret": client_secret, "org": org}, indent=2))
        try:
            os.chmod(self.file, 0o600)
        except OSError:
            pass
        self._token, self._cache = None, None

    def clear(self) -> None:
        try:
            self.file.unlink()
        except OSError:
            pass
        self._token, self._cache = None, None

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

    def _get(self, path: str):
        try:
            resp = self.http.get(f"{API}{path}", headers=self._headers(), timeout=15)
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

    def readings(self) -> dict:
        """{"things": [{"id", "name", "device", "properties": [{"name", "label", "type", "value", "unit",
        "updatedAt", "writable"}]}], "fetchedAt"} — shared for a few seconds between screens."""
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < CACHE_SECONDS:
                return self._cache[1]
            things = []
            for t in self._get("/iot/v2/things") or []:
                props = self._get(f"/iot/v2/things/{t['id']}/properties") or []
                things.append({
                    "id": t.get("id"), "name": t.get("name") or "Thing",
                    "device": (t.get("device_name") or (t.get("device") or {}).get("name")) if isinstance(t, dict) else None,
                    "properties": sorted(({
                        "name": p.get("variable_name") or p.get("name") or "",
                        "label": nice_name(p.get("variable_name") or p.get("name") or ""),
                        "type": p.get("type") or "",
                        "value": p.get("last_value"),
                        "unit": UNITS.get((p.get("type") or "").upper(), ""),
                        "updatedAt": p.get("value_updated_at") or p.get("updated_at"),
                        "writable": (p.get("permission") or "").upper() == "READ_WRITE",
                    } for p in props), key=lambda x: x["label"].lower()),
                })
            result = {"things": things, "fetchedAt": time.time()}
            self._cache = (time.monotonic(), result)
            return result
