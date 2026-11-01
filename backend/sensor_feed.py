"""Live sensor values from the Firebase Realtime Database, written there directly by each ESP32.

Layout (set by the board firmware; the app only reads it, never writes under /sites):

    /sites/STI_Caloocan/restrooms/restroom_01/devices/<deviceId>/
        meta     {type, name, thing, board, uid}
        status   {lastSeen (server ms), uptimeS, rssiDbm, firmware}       every 30 s
        live     {<groups>, updatedAt (server ms)}                         within 2 s of a change
        samples  {s0..s9}  one per minute, ring buffer; each = live groups + t (server ms)

Device ids: urinal_01, toilet_01, room_01, deodorizer_01. Each one matches an Arduino Cloud Thing
(Urinal01, Toilet01, Room01, Deodorizer01). The live fields are mapped back to the Thing's variable
names (FIELDS below), so Firebase and Arduino Cloud values line up one-to-one.

Rules from the firmware side:
* a field the board has no real value for yet is LEFT OUT (never 0) → the screen shows "—";
* online = status/lastSeen less than 90 s old;
* samples: sort by t, ignore any older than 11 minutes (left over from before a restart).

How the app uses it: Arduino Cloud is the main source for the live screen; a Firebase value that is
newer replaces it, and when Arduino Cloud can't be reached the screen is built from Firebase alone.
The Pi reads with the Firebase Admin SDK (service account), which isn't limited by the database rules.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("aureoclean.sensor_feed")
DEFAULT_PATH = "sites/STI_Caloocan/restrooms/restroom_01/devices"
ONLINE_SECONDS = 90
SAMPLE_MAX_AGE = 11 * 60

# deviceId → the Arduino Cloud Thing name (used when meta/thing isn't set)
THINGS = {"urinal_01": "Urinal01", "toilet_01": "Toilet01", "room_01": "Room01", "deodorizer_01": "Deodorizer01"}

_AIR = {"temperatureC": "temperature", "humidityPct": "humidity", "pressureHpa": "pressure", "gasKOhm": "gasResistance",
        "iaq": "iaq", "iaqAccuracy": "iaqAccuracy", "gasBaselineKOhm": "gasBaseline", "sensorStatus": "sensorStatus"}
_PRESENCE = {"inUse": "cubicleOccupied", "distanceCm": "tofDistanceCm"}
# (group, field) → Arduino Cloud variable name, per kind of device
FIELDS = {
    "urinal_01": {"air": _AIR, "presence": _PRESENCE,
                  "usage": {"totalUsers": "totalToFUsers", "usersSinceFlush": "currentToFUsers",
                            "totalFlushes": "totalFlushes", "flushThreshold": "flushThreshold"}},
    "toilet_01": {"air": _AIR, "presence": _PRESENCE, "usage": {"totalUses": "totalUses", "totalFlushes": "totalFlushes"}},
    "room_01": {"people": {"inRoom": "peopleInRoom", "entriesToday": "entriesToday", "occupied": "roomOccupied",
                           "doorDistanceCm": "doorDistanceCm"},
                "cleaning": {"needsCleaning": "needsCleaning", "visitsSinceCleaning": "visitsSinceCleaning",
                             "cleaningThreshold": "cleaningThreshold"}},
    "deodorizer_01": {"spray": {"active": "sprayActive", "spraysToday": "spraysToday", "durationSec": "sprayDurationSec",
                                "stopped": "sprayStopped"},
                      "safety": {"eStop": "eStopPressed"}},
}
UNITS = {"temperature": "°C", "humidity": "%", "pressure": "hPa", "gasResistance": "kΩ", "gasBaseline": "kΩ",
         "tofDistanceCm": "cm", "doorDistanceCm": "cm", "sprayDurationSec": "s"}


def _kind(device_id: str) -> str:
    """urinal_02 → urinal_01's field map (same firmware)."""
    base = device_id.rsplit("_", 1)[0]
    return next((k for k in FIELDS if k.rsplit("_", 1)[0] == base), device_id)


def _ms(v) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return v / 1000 if v > 1e11 else float(v)
    return None


def _flatten(device_id: str, groups: dict) -> dict:
    """live/sample groups → {Arduino variable name: value}; unknown fields keep their own name."""
    fields = FIELDS.get(_kind(device_id), {})
    out = {}
    for group, body in (groups or {}).items():
        if not isinstance(body, dict):
            continue
        names = fields.get(group, {})
        for field, value in body.items():
            if isinstance(value, (dict, list)) or value is None:
                continue
            out[names.get(field, field)] = value
    return out


class SensorFeed:
    def __init__(self, app_getter: Callable[[], object], path: str = DEFAULT_PATH, clock: Callable[[], float] = time.time):
        self._app_getter = app_getter
        self.path = path.strip("/") or DEFAULT_PATH
        self.clock = clock
        self.tree: dict = {}
        self.connected = False
        self.last_event: Optional[float] = None
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._devices: dict[str, dict] = {}
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ the data
    def apply(self, event_type: str, path: str, data) -> None:
        """A Realtime Database event ("put" or "patch" at a path below the watched one)."""
        with self._lock:
            parts = [p for p in (path or "/").split("/") if p]
            if not parts:
                if event_type == "patch" and isinstance(data, dict):
                    self.tree.update(data)
                else:
                    self.tree = data if isinstance(data, dict) else {}
            else:
                node = self.tree
                for p in parts[:-1]:
                    if not isinstance(node.get(p), dict):
                        node[p] = {}
                    node = node[p]
                if event_type == "patch" and isinstance(data, dict) and isinstance(node.get(parts[-1]), dict):
                    node[parts[-1]].update(data)
                elif data is None:
                    node.pop(parts[-1], None)
                else:
                    node[parts[-1]] = data
            self._devices = self._read(self.tree)
            self.last_event = self.clock()

    def _read(self, tree: dict) -> dict:
        out = {}
        for dev_id, node in (tree or {}).items():
            if not isinstance(node, dict):
                continue
            meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
            status = node.get("status") if isinstance(node.get("status"), dict) else {}
            live = node.get("live") if isinstance(node.get("live"), dict) else {}
            out[dev_id] = {
                "id": dev_id, "thing": meta.get("thing") or THINGS.get(dev_id) or dev_id, "name": meta.get("name") or "",
                "values": _flatten(dev_id, {k: v for k, v in live.items() if k != "updatedAt"}),
                "updatedAt": _ms(live.get("updatedAt")), "lastSeen": _ms(status.get("lastSeen")),
                "rssi": status.get("rssiDbm"), "firmware": status.get("firmware"),
                "samplesRaw": node.get("samples") if isinstance(node.get("samples"), dict) else {},
            }
        return out

    def online(self, dev: dict) -> bool:
        return bool(dev.get("lastSeen")) and self.clock() - dev["lastSeen"] < ONLINE_SECONDS

    def device(self, thing_id: str = "", thing_name: str = "") -> Optional[dict]:
        """The device for an Arduino Thing, by Thing name (meta/thing) or device id."""
        keys = {str(thing_id).lower(), str(thing_name).lower()}
        with self._lock:
            for dev in self._devices.values():
                if dev["thing"].lower() in keys or dev["id"].lower() in keys:
                    return dev
        return None

    def lookup(self, thing_id: str, thing_name: str) -> dict[str, tuple]:
        """{variable name (lower case): (value, unix time)} for a Thing."""
        dev = self.device(thing_id, thing_name)
        if not dev:
            return {}
        at = dev["updatedAt"] or 0.0
        return {k.lower(): (v, at) for k, v in dev["values"].items()}

    def samples(self, thing_id: str = "", thing_name: str = "") -> list[dict]:
        """The last ~10 minutes, oldest first: [{"t": unix time, variable: value, …}]."""
        dev = self.device(thing_id, thing_name)
        if not dev:
            return []
        now, rows = self.clock(), []
        for s in dev["samplesRaw"].values():
            t = _ms(s.get("t")) if isinstance(s, dict) else None
            if t is None or now - t > SAMPLE_MAX_AGE:
                continue
            rows.append({"t": t, **_flatten(dev["id"], {k: v for k, v in s.items() if k != "t"})})
        return sorted(rows, key=lambda r: r["t"])

    def overlay(self, data: dict) -> dict:
        """Put newer Firebase values into the Arduino readings (in place; same objects the pages use)."""
        if not self._devices:
            return data
        for t in data.get("things", []):
            dev = self.device(t.get("id", ""), t.get("name", ""))
            if not dev:
                continue
            if dev["lastSeen"]:
                t["lastSeen"] = dev["lastSeen"]
            fresh = {k.lower(): v for k, v in dev["values"].items()}
            at = dev["updatedAt"] or 0
            for v in t["properties"]:
                key = str(v.get("name", "")).lower()
                if key not in fresh or not at:
                    continue
                mine = _iso_ts(v.get("updatedAt")) or 0
                if at >= mine:
                    v["value"] = fresh[key]
                    v["updatedAt"] = datetime.fromtimestamp(at, timezone.utc).isoformat()
                    v["via"] = "firebase"
        return data

    def as_readings(self, known: Optional[list] = None) -> dict:
        """The live screen from Firebase alone (Arduino Cloud unreachable). `known` = the last Thing list
        from Arduino ({id, name}), so units keep their Arduino ids (and their learned history)."""
        ids = {str(t.get("name", "")).lower(): t.get("id") for t in known or []}
        things = []
        with self._lock:
            devices = list(self._devices.values())
        for dev in devices:
            stamp = datetime.fromtimestamp(dev["updatedAt"], timezone.utc).isoformat() if dev["updatedAt"] else None
            tid = ids.get(dev["thing"].lower()) or dev["id"]
            props = [{"id": f"{dev['id']}.{k}", "thingId": tid, "thing": dev["thing"], "name": k, "label": _nice(k),
                      "type": "", "value": v, "unit": UNITS.get(k, ""), "updatedAt": stamp, "writable": False, "via": "firebase"}
                     for k, v in dev["values"].items()]
            things.append({"id": tid, "name": dev["thing"], "device": dev["id"], "lastSeen": dev["lastSeen"],
                           "properties": sorted(props, key=lambda x: x["label"].lower())})
        pages = [{"id": f"thing-{t['id']}", "name": t["name"], "dashboard": "", "dashboardId": None, "fromThing": True,
                  "widgets": [{"id": v["id"], "type": "", "name": v["label"], "x": i % 4, "y": i // 4, "w": 1, "h": 1,
                               "options": {}, "values": [v]} for i, v in enumerate(t["properties"])]}
                 for t in things if t["properties"]]
        return {"things": things, "pages": pages, "fetchedAt": self.clock(), "source": "firebase"}

    # ------------------------------------------------------------------ connection
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="sensor-feed")
        self._thread.start()

    def _run(self) -> None:
        while True:
            app = None
            try:
                app = self._app_getter()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)[:200]
            if app is None:
                time.sleep(15)
                continue
            try:
                from firebase_admin import db
                ref = db.reference(self.path, app=app)
                self.apply("put", "/", ref.get() or {})
                self.connected, self.error = True, None
                log.info("Listening to Firebase /%s (%s)", self.path, ", ".join(sorted(self._devices)) or "no devices yet")
                reg = ref.listen(self._event)
                while True:                      # the listener runs in its own thread; restart it if it stops
                    time.sleep(10)
                    if getattr(reg, "_thread", None) is not None and not reg._thread.is_alive():
                        raise RuntimeError("listener stopped")
            except Exception as exc:  # noqa: BLE001 - e.g. no databaseURL, no internet: retry
                self.connected = False
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("Realtime Database sensor feed: %s (retrying)", self.error)
                time.sleep(15)

    def _event(self, event) -> None:
        try:
            self.apply(event.event_type, event.path, event.data)
        except Exception:  # noqa: BLE001
            log.warning("Bad sensor event at %s", getattr(event, "path", "?"), exc_info=True)

    def status(self) -> dict:
        with self._lock:
            devices = list(self._devices.values())
        return {"connected": self.connected, "path": self.path, "error": self.error, "lastEvent": self.last_event,
                "things": sorted(d["thing"] for d in devices),
                "devices": [{"id": d["id"], "thing": d["thing"], "online": self.online(d), "lastSeen": d["lastSeen"],
                             "rssi": d["rssi"], "firmware": d["firmware"]} for d in sorted(devices, key=lambda d: d["id"])]}


def _iso_ts(v) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return _ms(v)
    if isinstance(v, str) and v:
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            return None
    return None


def _nice(name: str) -> str:
    try:
        from .arduino import nice_name
        return nice_name(name)
    except Exception:  # noqa: BLE001
        return name
