"""The AureoClean web app: the screens shown on the Pi and the Wi-Fi website.

* The pages themselves live in ``webapp/`` and are served by the same local
  service as the API (http://127.0.0.1:8765/ on the Pi).
* "Website on Wi-Fi" (System → Kiosk & web) starts a second listener on the
  Pi's network address so phones and laptops on the same Wi-Fi can open it.
  Only private network addresses (home/school Wi-Fi) are let in; the website is
  never reachable from the internet unless someone forwards the port on purpose.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

log = logging.getLogger("aureoclean.web")

WEBAPP_DIR = Path(__file__).resolve().parent.parent / "webapp"
DEFAULT_PORT = 8080

# Strict browser rules for the web app: only our own files, nothing from other sites.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; font-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cache-Control": "no-cache",
}

TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".svg": "image/svg+xml", ".png": "image/png", ".woff": "font/woff", ".woff2": "font/woff2",
         ".json": "application/json", ".txt": "text/plain; charset=utf-8", ".webmanifest": "application/manifest+json"}


def static_file(rel: str) -> Optional[Path]:
    """A file inside webapp/, or None (never anything outside it)."""
    path = (WEBAPP_DIR / rel).resolve()
    try:
        path.relative_to(WEBAPP_DIR.resolve())
    except ValueError:
        return None
    return path if path.is_file() and path.suffix in TYPES else None


def is_private(host: str) -> bool:
    """Same-network addresses: home/school Wi-Fi, the Pi itself."""
    try:
        ip = ipaddress.ip_address((host or "").split("%")[0])
    except ValueError:
        return host == "testclient"
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # Anything that isn't a public internet address: home/school ranges, the 100.64.x.x
    # range some school and carrier networks use, link-local, the Pi itself.
    return not ip.is_global or ip.is_loopback or ip.is_link_local


class RateLimit:
    """At most ``limit`` requests per ``window`` seconds per key (e.g. sign-in attempts per phone)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


def lan_addresses() -> list[str]:
    """The Pi's addresses on the local network (best effort, no internet needed)."""
    found = []
    try:  # the address used to reach the network (no packet is actually sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.168.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    return [a for a in dict.fromkeys(found) if not a.startswith("127.")]


class WebAccess:
    """Turns the Wi-Fi website on and off, and remembers the choice."""

    def __init__(self, app, settings_file: Path, port: Optional[int] = None):
        self.app = app
        self.file = settings_file
        saved = self._load()
        self.port = int(port or saved.get("port") or DEFAULT_PORT)
        self.enabled = bool(saved.get("enabled"))
        self.error: Optional[str] = None
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps({"enabled": self.enabled, "port": self.port}, indent=2))

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._server and self._server.started)

    def start(self) -> None:
        import uvicorn

        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.error = None
            config = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning",
                                    proxy_headers=False, server_header=False)
            server = uvicorn.Server(config)
            server.install_signal_handlers = lambda: None  # the main server handles Ctrl+C

            def serve():
                try:
                    server.run()
                except SystemExit:  # uvicorn exits like this when the port is taken
                    self.error = f"Port {self.port} is already in use on this Pi."
                except Exception as exc:  # noqa: BLE001
                    self.error = f"Couldn't start the website: {exc}"
                if not server.started and not self.error:
                    self.error = f"Couldn't open port {self.port}."
                if self.error:
                    log.warning("Wi-Fi website: %s", self.error)

            self._server = server
            self._thread = threading.Thread(target=serve, name="wifi-website", daemon=True)
            self._thread.start()
        for _ in range(30):  # wait up to 3 s for it to come up (or fail)
            if server.started or not self._thread.is_alive():
                break
            time.sleep(0.1)

    def stop(self) -> None:
        with self._lock:
            if self._server is not None:
                self._server.should_exit = True
            thread = self._thread
        if thread:
            thread.join(5)
        self._server = self._thread = None

    def set_enabled(self, on: bool) -> dict:
        self.enabled = bool(on)
        self._save()
        if self.enabled:
            self.start()
        else:
            self.stop()
            self.error = None
        return self.status()

    def status(self) -> dict:
        # The number address first: many Android phones can't open ".local" names.
        host = socket.gethostname()
        urls = [f"http://{a}:{self.port}" for a in lan_addresses()] + [f"http://{host}.local:{self.port}"]
        return {"enabled": self.enabled, "running": self.running, "port": self.port, "urls": urls,
                "error": self.error}

    def self_test(self) -> dict:
        """Open the website from the Pi itself, through its Wi-Fi address. If this works but a
        phone can't connect, the Wi-Fi network is blocking devices from reaching each other."""
        import urllib.request

        results = []
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for addr in lan_addresses() or ["127.0.0.1"]:
            url = f"http://{addr}:{self.port}/health"
            try:
                with opener.open(url, timeout=4) as resp:
                    results.append({"address": addr, "ok": resp.status == 200, "detail": f"answered ({resp.status})"})
            except Exception as exc:  # noqa: BLE001
                results.append({"address": addr, "ok": False, "detail": type(exc).__name__})
        ok = any(r["ok"] for r in results)
        if not self.running:
            verdict = "The Wi-Fi website isn't running. Turn it off and on again; if it stays off, restart the Pi."
        elif ok:
            verdict = ("The website works on this Pi's network address. If a phone still can't open it: make sure "
                       "the phone is on the SAME Wi-Fi as the Pi (not mobile data), and type the number address "
                       "exactly. If it still fails, this Wi-Fi blocks devices from reaching each other (common on "
                       "school and public Wi-Fi): use a phone hotspot or a router you control.")
        else:
            verdict = ("The Pi can't reach its own website through the network: a firewall may be blocking port "
                       f"{self.port}. If you installed one, run: sudo ufw allow {self.port}/tcp")
        return {"ok": ok and self.running, "results": results, "verdict": verdict, "addresses": lan_addresses()}


def qr_svg(text: str) -> Optional[str]:
    """A QR code for the website address (needs segno, installed on the Pi)."""
    try:
        import io

        import segno
    except ImportError:
        return None
    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", scale=6, border=2, dark="#14336E", xmldecl=False)
    return out.getvalue().decode()
