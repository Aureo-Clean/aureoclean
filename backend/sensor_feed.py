"""Live sensor values from the Firebase Realtime Database (written there by Arduino Cloud).

Arduino Cloud now sends the boards' values straight to Firebase, which is faster than asking the
Arduino API every few seconds. The Pi listens to that part of the Realtime Database and, whenever
a value there is newer than what the Arduino API gave, shows the newer one — on the dashboard and
to the smart alerts. The Arduino API is still used for the dashboard's pages and widgets.

It understands the layouts Arduino Cloud / an ESP32 can produce under AUREOCLEAN_SENSOR_PATH
(default "sensors"):

* clean:      sensors/Urinal01/temperature = 31.2      (or {value, updatedAt}, or sensors/Urinal01/latest/{…})
* webhook:    sensors/<pushId> = {thing_id, values: [{name, value, updated_at}, …]}  (Arduino webhooks)
              or sensors/Urinal01/<pushId> = {values: [...]}

and keeps a tidy summary for people reading the console:

    sensorLatest/Urinal01 = {temperature: 31.2, iaq: 74, …, updatedAt: <ms>}
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("aureoclean.sensor_feed")
SUMMARY_PATH = "sensorLatest"
SUMMARY_EVERY = 5         # seconds between summary writes
POLL_SECONDS = 5          # if live listening isn't possible


def _ts(v) -> Optional[float]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v / 1000 if v > 1e11 else float(v)
    if isinstance(v, str) and v:
        try:
            t = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            return None
    return None


def _is_webhook(node) -> bool:
    return isinstance(node, dict) and isinstance(node.get("values"), list)


class SensorFeed:
    def __init__(self, app_getter: Callable[[], object], path: str = "sensors", clock: Callable[[], float] = time.time):
        self._app_getter = app_getter
        self.path = path.strip("/") or "sensors"
        self.clock = clock
        self.tree: dict = {}
        self.connected = False
        self.last_event: Optional[float] = None
        self.error: Optional[str] = None
        self.format = ""
        self._lock = threading.Lock()
        self._latest: dict[str, dict[str, tuple]] = {}
        self._summary_at = 0.0
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ the data
    def apply(self, event_type: str, path: str, data) -> None:
        """A Realtime Database event ("put" or "patch" at a path below the watched one)."""
        with self._lock:
            parts = [p for p in (path or "/").split("/") if p]
            if not parts:
                if event_type == "patch" and isinstance(data, dict) and isinstance(self.tree, dict):
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
            self._latest = self._normalise(self.tree)
            self.last_event = self.clock()

    def _normalise(self, tree: dict) -> dict:
        out: dict[str, dict[str, tuple]] = {}
        fmt = set()

        def put(thing, name, value, at):
            if thing is None or name is None or isinstance(value, (dict, list)):
                return
            cur = out.setdefault(str(thing).lower(), {}).get(str(name).lower())
            at = at or 0.0
            if cur is None or at >= cur[1]:
                out[str(thing).lower()][str(name).lower()] = (value, at)

        def webhook(node, thing_hint=None):
            fmt.add("webhook")
            thing = node.get("thing_id") or node.get("thingId") or thing_hint
            for v in node["values"]:
                if isinstance(v, dict):
                    put(thing, v.get("name") or v.get("variable_name"), v.get("value"), _ts(v.get("updated_at") or v.get("updatedAt")))
                    if thing_hint and node.get("thing_id"):
                        put(thing_hint, v.get("name"), v.get("value"), _ts(v.get("updated_at")))

        for key, node in (tree or {}).items():
            if _is_webhook(node):
                webhook(node)
                continue
            if not isinstance(node, dict):
                continue
            stamp = _ts(node.get("updatedAt") or node.get("updated_at") or node.get("timestamp"))
            for k2, n2 in node.items():
                if _is_webhook(n2):
                    webhook(n2, key)
                elif k2 == "latest" and isinstance(n2, dict):
                    fmt.add("clean")
                    s2 = _ts(n2.get("updatedAt") or n2.get("updated_at")) or stamp
                    for k3, v3 in n2.items():
                        if k3 not in ("updatedAt", "updated_at"):
                            put(key, k3, v3, s2)
                elif isinstance(n2, dict) and "value" in n2:
                    fmt.add("clean")
                    put(key, k2, n2["value"], _ts(n2.get("updatedAt") or n2.get("updated_at") or n2.get("at")) or stamp)
                elif k2 not in ("updatedAt", "updated_at", "timestamp") and not isinstance(n2, (dict, list)):
                    fmt.add("clean")
                    put(key, k2, n2, stamp)
        self.format = "+".join(sorted(fmt))
        return out

    def lookup(self, thing_id: str, thing_name: str) -> dict[str, tuple]:
        """{variable name (lower case): (value, unix time)} for a thing, by id or name."""
        with self._lock:
            return self._latest.get(str(thing_id).lower()) or self._latest.get(str(thing_name).lower()) or {}

    def overlay(self, data: dict) -> dict:
        """Put newer Firebase values into the Arduino readings (in place; same objects the pages use)."""
        if not self._latest:
            return data
        for t in data.get("things", []):
            fresh = self.lookup(t.get("id", ""), t.get("name", ""))
            if not fresh:
                continue
            for v in t["properties"]:
                hit = fresh.get(str(v.get("name", "")).lower())
                if not hit:
                    continue
                value, at = hit
                mine = _ts(v.get("updatedAt")) or 0
                if at and at >= mine:
                    v["value"] = value
                    v["updatedAt"] = datetime.fromtimestamp(at, timezone.utc).isoformat()
                    v["via"] = "firebase"
        return data

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
                log.info("Listening to Firebase Realtime Database /%s (%s)", self.path, self.format or "empty")
                reg = ref.listen(lambda e, a=app: self._event(e, a))
                while True:                      # the listener runs in its own thread; keep the summary fresh
                    time.sleep(SUMMARY_EVERY)
                    self._write_summary(app)
                    if getattr(reg, "_thread", None) is not None and not reg._thread.is_alive():
                        raise RuntimeError("listener stopped")
            except Exception as exc:  # noqa: BLE001 - e.g. no databaseURL, no internet: retry
                self.connected = False
                self.error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("Realtime Database sensor feed: %s (retrying)", self.error)
                time.sleep(15)

    def _event(self, event, app) -> None:
        try:
            self.apply(event.event_type, event.path, event.data)
        except Exception:  # noqa: BLE001
            log.warning("Bad sensor event at %s", getattr(event, "path", "?"), exc_info=True)

    def _write_summary(self, app) -> None:
        """sensorLatest/<thing> = {variable: value, …, updatedAt} — a tidy view of the newest values."""
        if not self._latest or self.clock() - self._summary_at < SUMMARY_EVERY:
            return
        self._summary_at = self.clock()
        try:
            from firebase_admin import db
            for thing, vals in list(self._latest.items()):
                body = {k: v for k, (v, _at) in vals.items()}
                body["updatedAt"] = int(max((at for _v, at in vals.values()), default=self.clock()) * 1000)
                db.reference(f"{SUMMARY_PATH}/{thing}", app=app).set(body)
        except Exception as exc:  # noqa: BLE001
            log.debug("Couldn't write the sensor summary: %s", exc)

    def status(self) -> dict:
        return {"connected": self.connected, "path": self.path, "format": self.format, "error": self.error,
                "things": sorted(self._latest), "lastEvent": self.last_event}
