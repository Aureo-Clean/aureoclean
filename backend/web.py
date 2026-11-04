"""The AureoClean web app: the screens shown on the Pi and the Wi-Fi website.

* The pages themselves live in ``webapp/`` and are served by the same local
  service as the API (http://127.0.0.1:8765/ on the Pi).
* "Website on Wi-Fi" (System → Kiosk & web) starts a second listener on the
  Pi's network address so phones and laptops on the same Wi-Fi can open it.
  It is HTTPS (encrypted) with a certificate the Pi makes for itself
  (~/.config/aureoclean/web-cert.pem); http://…:8080 only redirects there.
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
DEFAULT_PORT = 8443        # HTTPS (encrypted)
REDIRECT_PORT = 8080       # plain http: only sends phones on to the https address

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


def ensure_cert(folder: Path, hosts: list[str]) -> tuple[Path, Path, str]:
    """The Wi-Fi website's certificate (self-signed, kept so phones only have to accept it once).
    Made again when the Pi's address changed. Returns (cert, key, SHA-256 fingerprint)."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    folder.mkdir(parents=True, exist_ok=True)
    cert_file, key_file = folder / "web-cert.pem", folder / "web-key.pem"
    ips = sorted({h for h in hosts if _is_ip(h)} | {"127.0.0.1"})
    names = sorted({h for h in hosts if not _is_ip(h)} | {"localhost"})
    if cert_file.exists() and key_file.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_file.read_bytes())
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have = {str(a) for a in san.get_values_for_type(x509.IPAddress)} | set(san.get_values_for_type(x509.DNSName))
            fresh = cert.not_valid_after_utc - dt.timedelta(days=30) > dt.datetime.now(dt.timezone.utc)
            if fresh and set(ips) <= have and set(names) <= have:
                return cert_file, key_file, _fingerprint(cert.public_bytes(serialization.Encoding.DER))
        except Exception:  # noqa: BLE001 - unreadable: make a new one
            pass
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AureoClean (this Pi)"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AureoClean · STI College Caloocan")])
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    import os
    fd = os.open(str(key_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    os.chmod(key_file, 0o600)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    log.info("Wi-Fi website: new HTTPS certificate for %s", ", ".join(names + ips))
    return cert_file, key_file, _fingerprint(cert.public_bytes(serialization.Encoding.DER))


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def _fingerprint(der: bytes) -> str:
    import hashlib
    h = hashlib.sha256(der).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def redirect_app(https_port: int):
    """A tiny plain-http listener: every request is sent on to the same page over https."""
    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1").split(":")[0] or "localhost"
        if not all(c.isalnum() or c in ".-" for c in host):
            host = "localhost"
        path = scope.get("path") or "/"
        query = scope.get("query_string") or b""
        target = f"https://{host}:{https_port}{path}" + (f"?{query.decode('latin-1')}" if query else "")
        await send({"type": "http.response.start", "status": 308,
                    "headers": [(b"location", target.encode("latin-1")), (b"content-length", b"0"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})
    return app


class WebAccess:
    """Turns the Wi-Fi website on and off, and remembers the choice."""

    def __init__(self, app, settings_file: Path, port: Optional[int] = None):
        self.app = app
        self.file = settings_file
        saved = self._load()
        saved_port = int(saved.get("port") or 0)
        if saved_port == REDIRECT_PORT:     # older versions served plain http on 8080
            saved_port = 0
        self.port = int(port or saved_port or DEFAULT_PORT)
        self.redirect_port = REDIRECT_PORT
        self.fingerprint = ""
        self._redirect = None
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
            try:
                cert, key, self.fingerprint = ensure_cert(self.file.parent, lan_addresses() + [f"{socket.gethostname()}.local"])
            except Exception as exc:  # noqa: BLE001
                self.error = f"Couldn't make the HTTPS certificate: {exc}"
                log.warning("Wi-Fi website: %s", self.error)
                return
            config = uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning",
                                    proxy_headers=False, server_header=False,
                                    ssl_certfile=str(cert), ssl_keyfile=str(key))
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
            # http://…:8080 (old links, typed addresses) → https
            rconf = uvicorn.Config(redirect_app(self.port), host="0.0.0.0", port=self.redirect_port, log_level="warning",
                                   proxy_headers=False, server_header=False, lifespan="off")
            rserver = uvicorn.Server(rconf)
            rserver.install_signal_handlers = lambda: None
            self._redirect = rserver

            def serve_redirect():
                try:
                    rserver.run()
                except (SystemExit, Exception):  # noqa: BLE001 - optional: the https address still works
                    log.info("Wi-Fi website: the http→https redirect on port %s isn't available", self.redirect_port)
            threading.Thread(target=serve_redirect, name="wifi-redirect", daemon=True).start()
        for _ in range(30):  # wait up to 3 s for it to come up (or fail)
            if server.started or not self._thread.is_alive():
                break
            time.sleep(0.1)

    def stop(self) -> None:
        with self._lock:
            if self._server is not None:
                self._server.should_exit = True
            if self._redirect is not None:
                self._redirect.should_exit = True
                self._redirect = None
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
        urls = [f"https://{a}:{self.port}" for a in lan_addresses()] + [f"https://{host}.local:{self.port}"]
        return {"enabled": self.enabled, "running": self.running, "port": self.port, "urls": urls,
                "error": self.error, "secure": True, "fingerprint": self.fingerprint}

    def self_test(self) -> dict:
        """Open the website from the Pi itself, through its Wi-Fi address. If this works but a
        phone can't connect, the Wi-Fi network is blocking devices from reaching each other."""
        import urllib.request

        import ssl

        results = []
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE          # our own self-signed certificate: only checking it answers
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
        for addr in lan_addresses() or ["127.0.0.1"]:
            url = f"https://{addr}:{self.port}/health"
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
