"""Internet watchdog.

Every couple of minutes it fetches Google's tiny "generate_204" page. This
  * keeps the Pi's Wi-Fi awake (its power saving can stop background
    programs from reaching the internet until a browser wakes it up),
  * detects a Wi-Fi network that wants a sign-in page (captive portal): those
    answer with a web page instead of the empty "204" reply,
  * lets the app show "online / offline / Wi-Fi sign-in needed" instead of a
    vague error at sign-in time.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import requests

log = logging.getLogger("aureoclean.network")

CHECK_URL = "http://connectivitycheck.gstatic.com/generate_204"
ONLINE, OFFLINE, PORTAL, UNKNOWN = "online", "offline", "portal", "unknown"
MESSAGES = {
    ONLINE: "Internet connected",
    OFFLINE: "No internet — check the Wi-Fi",
    PORTAL: "The Wi-Fi needs a sign-in page — open Chrome to log in to the network",
    UNKNOWN: "Checking internet…",
}


def check_once(session: Optional[requests.Session] = None, timeout: float = 8) -> str:
    http = session or requests
    try:
        resp = http.get(CHECK_URL, timeout=timeout, allow_redirects=False)
    except requests.RequestException as exc:
        log.info("Internet check failed: %s", type(exc).__name__)
        return OFFLINE
    return ONLINE if resp.status_code == 204 else PORTAL


class NetworkWatch:
    def __init__(self, interval: float = 120):
        self.interval = interval
        self.state = UNKNOWN
        self.checked_at: Optional[float] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def check_now(self) -> str:
        state = check_once()
        with self._lock:
            if state != self.state:
                log.info("Internet: %s", MESSAGES[state])
            self.state, self.checked_at = state, time.time()
        return state

    def status(self) -> dict:
        with self._lock:
            return {"internet": self.state, "message": MESSAGES[self.state]}

    def start(self) -> None:
        def loop():
            while not self._stop.is_set():
                self.check_now()
                # Check again sooner while there's a problem.
                self._stop.wait(self.interval if self.state == ONLINE else 20)

        threading.Thread(target=loop, name="network-watch", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
