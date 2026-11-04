"""Wi-Fi website: who may connect, rate limits, file serving stays inside webapp/."""
import tempfile
import unittest
from pathlib import Path

from backend import web


class WhoMayConnect(unittest.TestCase):
    def test_same_network_only(self):
        for ok in ("192.168.1.23", "10.0.0.5", "172.16.4.2", "100.72.1.9", "127.0.0.1", "::1", "fe80::1%wlan0",
                   "::ffff:192.168.0.9", "fd00::5"):
            self.assertTrue(web.is_private(ok), ok)
        for bad in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888", "", "not-an-ip"):
            self.assertFalse(web.is_private(bad), bad)


class Limits(unittest.TestCase):
    def test_rate_limit(self):
        rl = web.RateLimit(3, 60)
        self.assertEqual([rl.allow("phone-a") for _ in range(4)], [True, True, True, False])
        self.assertTrue(rl.allow("phone-b"))  # per device


class Files(unittest.TestCase):
    def test_only_files_inside_webapp(self):
        self.assertIsNotNone(web.static_file("index.html"))
        self.assertIsNotNone(web.static_file("assets/js/main.js"))
        for bad in ("../backend/service.py", "assets/../../README.md", "/etc/passwd", "assets/missing.js"):
            self.assertIsNone(web.static_file(bad), bad)

    def test_every_script_is_served_with_a_known_type(self):
        for path in (web.WEBAPP_DIR / "assets").rglob("*"):
            if path.is_file() and path.name != "OFL.txt":
                self.assertIn(path.suffix, web.TYPES, path)

    def test_strict_browser_rules(self):
        csp = web.SECURITY_HEADERS["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_no_inline_html_injection_in_the_app(self):
        # The web app builds pages with textContent only, so nothing typed by a user can run.
        for js in (web.WEBAPP_DIR / "assets" / "js").glob("*.js"):
            text = js.read_text()
            self.assertNotIn("innerHTML", text, js.name)
            self.assertNotIn("insertAdjacentHTML", text, js.name)
            self.assertNotIn("eval(", text, js.name)


class Switch(unittest.TestCase):
    def test_setting_is_remembered(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "web.json"
            access = web.WebAccess(app=None, settings_file=f, port=18080)
            access.start = lambda: None  # don't open a real port in tests
            st = access.set_enabled(True)
            self.assertTrue(st["enabled"])
            self.assertTrue(all(u.endswith(":18080") for u in st["urls"]))
            again = web.WebAccess(app=None, settings_file=f)
            self.assertTrue(again.enabled)
            self.assertEqual(again.port, 18080)



class Https(unittest.TestCase):
    def test_certificate_is_kept_and_remade_when_the_address_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            cert, key, fp = web.ensure_cert(d, ["192.168.1.20", "aureoclean.local"])
            self.assertEqual(oct(key.stat().st_mode & 0o777), "0o600")
            self.assertEqual(web.ensure_cert(d, ["192.168.1.20", "aureoclean.local"])[2], fp)   # same one again
            self.assertNotEqual(web.ensure_cert(d, ["10.0.0.7", "aureoclean.local"])[2], fp)     # new Wi-Fi address
            self.assertEqual(len(fp.split(":")), 32)

    def test_serves_https_and_sends_http_on_to_it(self):
        import ssl
        import time
        import urllib.error
        import urllib.request

        async def tiny(scope, receive, send):
            if scope["type"] == "lifespan":
                while True:
                    m = await receive()
                    if m["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif m["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok"})

        with tempfile.TemporaryDirectory() as tmp:
            access = web.WebAccess(app=tiny, settings_file=Path(tmp) / "web.json", port=18443)
            access.redirect_port = 18081
            access.start()
            self.addCleanup(access.stop)
            self.assertTrue(access.running, access.error)
            ctx = ssl.create_default_context()
            ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
            with opener.open("https://127.0.0.1:18443/health", timeout=5) as r:
                self.assertEqual(r.read(), b"ok")
            with self.assertRaises(Exception):          # plain http on the https port doesn't work
                opener.open("http://127.0.0.1:18443/health", timeout=5).read()

            class NoFollow(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **k):
                    return None
            plain = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoFollow())
            for _ in range(20):
                try:
                    plain.open("http://127.0.0.1:18081/report?r=2F-M", timeout=5)
                except urllib.error.HTTPError as exc:
                    self.assertEqual(exc.code, 308)
                    self.assertEqual(exc.headers["Location"], "https://127.0.0.1:18443/report?r=2F-M")
                    break
                except OSError:
                    time.sleep(0.2)
            else:
                self.fail("redirect listener didn't answer")
            self.assertTrue(access.status()["urls"][0].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
