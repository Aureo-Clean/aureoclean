"""Connect to Firebase from inside the app (System → Data → Connect to Firebase).

An admin pastes the Web API key and the Realtime Database address, and picks the service-account
key (found automatically in ~/Downloads after "Generate new private key", or uploaded). This then:

* checks them (the key file must be a real service-account key; the Web API key is tried on Firebase),
* moves the key file to ~/.config/aureoclean/service-account.json (owner-only) — it doesn't stay in Downloads,
* writes the settings file (AUREOCLEAN_MODE=firebase …), keeping every other line,
* and restarts the AureoClean service so it starts syncing.

The key file's contents are never sent back to the screen.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import requests

SIGN_IN_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword"
DEFAULT_PROJECT = "aureoclean-b7c2b"


class SetupError(Exception):
    pass


def env_path() -> Path:
    return Path(os.environ.get("AUREOCLEAN_ENV_FILE", "~/.config/aureoclean/aureoclean.env")).expanduser()


def _read_env(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            s = line.strip()
            if s and not s.startswith("#") and "=" in s:
                k, v = s.split("=", 1)
                v = v.strip().strip('"').strip("'")
                if v or k.strip() not in out:
                    out[k.strip()] = v
    except OSError:
        pass
    return out


def write_env(path: Path, values: dict) -> None:
    """Set these KEY=VALUE lines: replace an existing line (also a commented-out one), else append."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    done = set()
    out = []
    for line in lines:
        m = re.match(r"^\s*#?\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in values:
            if m.group(1) in done:
                continue                                   # drop duplicates of a key we set
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            done.add(m.group(1))
        else:
            out.append(line)
    for k, v in values.items():
        if k not in done:
            out.append(f"{k}={v}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def check_service_account(text: str) -> dict:
    try:
        data = json.loads(text)
    except ValueError:
        raise SetupError("That file isn't a Firebase key (not JSON). Use 'Generate new private key' in Firebase.") from None
    if not isinstance(data, dict) or data.get("type") != "service_account" or "private_key" not in data or "client_email" not in data:
        raise SetupError("That file isn't a service-account key. In Firebase: Project settings → Service accounts → "
                         "Generate new private key.")
    return data


def downloads_keys(folder: Optional[Path] = None) -> list[Path]:
    """Service-account keys sitting in ~/Downloads (newest first)."""
    folder = folder or Path.home() / "Downloads"
    found = []
    for p in folder.glob("*.json") if folder.is_dir() else []:
        try:
            if p.stat().st_size < 20000:
                check_service_account(p.read_text(errors="replace"))
                found.append(p)
        except (OSError, SetupError):
            continue
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def check_web_key(key: str, http=None) -> None:
    """Ask Firebase whether this Web API key is valid (a sign-in with no account: a valid key says
    'email not found', an invalid one says 'API key not valid')."""
    http = http or requests
    try:
        r = http.post(SIGN_IN_URL, params={"key": key}, json={"email": "nobody@aureoclean.invalid", "password": "x" * 12,
                                                                 "returnSecureToken": True}, timeout=15)
    except requests.RequestException:
        return                              # offline: can't check now; it's checked again when online
    msg = ""
    try:
        msg = (r.json().get("error") or {}).get("message", "")
    except ValueError:
        pass
    if "API key not valid" in msg or msg.startswith("API_KEY") or r.status_code == 403 and "key" in msg.lower():
        raise SetupError("Firebase says that Web API key isn't valid. Copy it again from Project settings → General.")


class FirebaseSetup:
    def __init__(self, settings, config_dir: Path, http=None, downloads: Optional[Path] = None):
        self.settings = settings
        self.dir = Path(config_dir)
        self.http = http
        self.downloads = downloads

    def status(self) -> dict:
        env = _read_env(env_path())
        key_file = Path(os.path.expanduser(env.get("GOOGLE_APPLICATION_CREDENTIALS") or str(self.dir / "service-account.json")))
        project = env.get("AUREOCLEAN_FIREBASE_PROJECT_ID") or DEFAULT_PROJECT
        return {
            "mode": self.settings.mode, "projectId": project,
            "hasWebKey": bool(env.get("AUREOCLEAN_FIREBASE_WEB_API_KEY")), "hasKeyFile": key_file.is_file(),
            "databaseUrl": env.get("AUREOCLEAN_FIREBASE_DATABASE_URL") or "",
            "databaseGuess": f"https://{project}-default-rtdb.asia-southeast1.firebasedatabase.app",
            "inDownloads": [p.name for p in downloads_keys(self.downloads)],
            "links": {
                "general": f"https://console.firebase.google.com/project/{project}/settings/general",
                "serviceAccounts": f"https://console.firebase.google.com/project/{project}/settings/serviceaccounts/adminsdk",
                "database": f"https://console.firebase.google.com/project/{project}/database",
            },
        }

    def connect(self, web_api_key: str, database_url: str = "", key_json: str = "", key_from_downloads: str = "") -> dict:
        env = _read_env(env_path())
        web_api_key = (web_api_key or "").strip() or env.get("AUREOCLEAN_FIREBASE_WEB_API_KEY", "")
        if not re.fullmatch(r"AIza[0-9A-Za-z_\-]{30,}", web_api_key or ""):
            raise SetupError("Paste the Web API key (it starts with “AIza”). Firebase Console → Project settings → General.")
        target = self.dir / "service-account.json"
        source: Optional[Path] = None
        if key_json:
            sa = check_service_account(key_json)
        elif key_from_downloads:
            folder = self.downloads or Path.home() / "Downloads"
            source = folder / Path(key_from_downloads).name          # no paths, just a name from the list
            try:
                key_json = source.read_text()
            except OSError:
                raise SetupError("That key file isn't in Downloads any more.") from None
            sa = check_service_account(key_json)
        elif target.is_file():
            sa = check_service_account(target.read_text())
        else:
            raise SetupError("Add the service-account key: Firebase → Project settings → Service accounts → "
                             "Generate new private key (it lands in Downloads), then pick it here.")
        project = sa.get("project_id") or env.get("AUREOCLEAN_FIREBASE_PROJECT_ID") or DEFAULT_PROJECT
        db = (database_url or "").strip().strip("/")
        if db and "://" not in db:
            db = "https://" + db
        if db and not re.fullmatch(r"https://[a-z0-9.-]+\.(firebaseio\.com|firebasedatabase\.app)", db):
            raise SetupError("The Realtime Database address looks like https://…firebaseio.com or https://…firebasedatabase.app")
        check_web_key(web_api_key, self.http)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        if key_json:
            tmp = target.with_suffix(".tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key_json)
            os.replace(tmp, target)
            if source is not None:
                try:
                    source.unlink()                                   # don't leave the key lying in Downloads
                except OSError:
                    pass
        values = {"AUREOCLEAN_MODE": "firebase", "AUREOCLEAN_FIREBASE_PROJECT_ID": project,
                  "AUREOCLEAN_FIREBASE_WEB_API_KEY": web_api_key, "GOOGLE_APPLICATION_CREDENTIALS": str(target)}
        if db:
            values["AUREOCLEAN_FIREBASE_DATABASE_URL"] = db
        write_env(env_path(), values)
        os.environ.update(values)                   # the restarted service reads these
        return {"projectId": project, "databaseUrl": db, "message": "Connected settings saved — restarting AureoClean…"}

    @staticmethod
    def restart_soon(delay: float = 1.5) -> None:
        """Start the service again in the same process (same PID, so run_local.sh / systemd don't notice)."""
        def go():
            time.sleep(delay)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
            os.execv(sys.executable, [sys.executable, "-m", "backend.main"])
        threading.Thread(target=go, daemon=True, name="restart").start()
