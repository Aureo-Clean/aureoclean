"""Wi-Fi website: who may connect, rate limits, file serving stays inside webapp/."""
import tempfile
import unittest
from pathlib import Path

from backend import web


class WhoMayConnect(unittest.TestCase):
    def test_same_network_only(self):
        for ok in ("192.168.1.23", "10.0.0.5", "172.16.4.2", "127.0.0.1", "::1", "fe80::1%wlan0", "::ffff:192.168.0.9"):
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


if __name__ == "__main__":
    unittest.main()
