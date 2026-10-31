"""Connect to Firebase from the app: settings written, key moved out of Downloads, checks."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.firebase_setup import FirebaseSetup, SetupError, write_env

KEY = {"type": "service_account", "project_id": "aureoclean-b7c2b", "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n",
       "client_email": "firebase-adminsdk@aureoclean-b7c2b.iam.gserviceaccount.com"}
WEB = "AIza" + "x" * 35


class Http:
    def __init__(self, msg="EMAIL_NOT_FOUND"):
        self.msg = msg

    def post(self, url, params=None, json=None, timeout=None):
        msg = self.msg

        class R:
            status_code = 400

            def json(self):
                return {"error": {"message": msg}}
        return R()


class Setup(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = self.home / "cfg"
        self.dl = self.home / "Downloads"
        self.dl.mkdir()
        self.env = self.cfg / "aureoclean.env"
        self.cfg.mkdir()
        self.env.write_text("# AureoClean\nAUREOCLEAN_MODE=local\n# AUREOCLEAN_FIREBASE_WEB_API_KEY=\nAUREOCLEAN_SMS_MODE=modem\n")
        p = mock.patch.dict(os.environ, {"AUREOCLEAN_ENV_FILE": str(self.env)})
        p.start()
        self.addCleanup(p.stop)
        settings = type("S", (), {"mode": "local"})()
        self.setup = FirebaseSetup(settings, self.cfg, http=Http(), downloads=self.dl)

    def test_key_found_in_downloads_is_moved_and_settings_written(self):
        (self.dl / "aureoclean-b7c2b-firebase-adminsdk-abc.json").write_text(json.dumps(KEY))
        (self.dl / "notes.json").write_text("{}")
        st = self.setup.status()
        self.assertEqual(st["inDownloads"], ["aureoclean-b7c2b-firebase-adminsdk-abc.json"])
        self.assertFalse(st["hasKeyFile"])
        r = self.setup.connect(WEB, "aureoclean-b7c2b-default-rtdb.asia-southeast1.firebasedatabase.app", "",
                               "aureoclean-b7c2b-firebase-adminsdk-abc.json")
        self.assertEqual(r["projectId"], "aureoclean-b7c2b")
        target = self.cfg / "service-account.json"
        self.assertEqual(json.loads(target.read_text())["client_email"], KEY["client_email"])
        self.assertEqual(oct(target.stat().st_mode & 0o777), "0o600")
        self.assertFalse((self.dl / "aureoclean-b7c2b-firebase-adminsdk-abc.json").exists())   # not left in Downloads
        env = self.env.read_text()
        self.assertIn("AUREOCLEAN_MODE=firebase", env)
        self.assertIn(f"AUREOCLEAN_FIREBASE_WEB_API_KEY={WEB}", env)
        self.assertIn("AUREOCLEAN_FIREBASE_DATABASE_URL=https://aureoclean-b7c2b-default-rtdb.asia-southeast1.firebasedatabase.app", env)
        self.assertIn("AUREOCLEAN_SMS_MODE=modem", env)            # other settings kept
        self.assertEqual(env.count("AUREOCLEAN_FIREBASE_WEB_API_KEY"), 1)
        self.assertEqual(os.environ["AUREOCLEAN_MODE"], "firebase")

    def test_bad_inputs(self):
        with self.assertRaises(SetupError):
            self.setup.connect("not-a-key", "", json.dumps(KEY), "")
        with self.assertRaises(SetupError):
            self.setup.connect(WEB, "", json.dumps({"type": "authorized_user"}), "")
        with self.assertRaises(SetupError):
            self.setup.connect(WEB, "https://evil.example.com", json.dumps(KEY), "")
        with self.assertRaises(SetupError):
            self.setup.connect(WEB, "", "", "")                      # no key anywhere
        self.setup.http = Http("API key not valid. Please pass a valid API key.")
        with self.assertRaises(SetupError):
            self.setup.connect(WEB, "", json.dumps(KEY), "")
        self.assertIn("AUREOCLEAN_MODE=local", self.env.read_text())    # nothing written on failure

    def test_write_env_replaces_and_appends(self):
        write_env(self.env, {"AUREOCLEAN_MODE": "firebase", "NEW_KEY": "1"})
        lines = self.env.read_text().splitlines()
        self.assertIn("AUREOCLEAN_MODE=firebase", lines)
        self.assertIn("NEW_KEY=1", lines)
        self.assertIn("# AureoClean", lines)
