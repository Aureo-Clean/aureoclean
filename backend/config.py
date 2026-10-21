"""Runtime configuration, read from environment variables.

Two storage modes are supported:

* ``firebase`` - Firebase Authentication + Cloud Firestore (the real deployment).
* ``local``    - a SQLite file on the Raspberry Pi. Useful for development,
                 demos and offline testing. Passwords are still hashed (scrypt).

If ``AUREOCLEAN_MODE`` is not set, firebase mode is used when a Firebase
project ID is configured, otherwise local mode.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    mode: str
    host: str
    port: int
    data_dir: Path
    collection_prefix: str
    firebase_project_id: str
    firebase_web_api_key: str
    credentials_path: str
    session_hours: int
    activation_code_hours: int
    admin_invite_hours: int
    max_activation_attempts: int
    max_login_failures: int
    login_lockout_minutes: int
    sms_mode: str = "log"
    sms_port: str = "/dev/serial0"
    sms_baud: int = 9600
    log_dir: Path = Path("logs")
    dashboard_url: str = ""
    arduino_cloud_url: str = "https://app.arduino.cc/"


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


DEFAULT_ENV_FILE = "~/.config/aureoclean/aureoclean.env"


def load_env_file(path: str | None = None) -> None:
    """Read KEY=VALUE lines from the config file into the environment.

    Values already set in the environment win, so you can still override
    anything from the terminal."""
    env_path = Path(path or os.environ.get("AUREOCLEAN_ENV_FILE", DEFAULT_ENV_FILE)).expanduser()
    if not env_path.is_file():
        return
    values: dict[str, str] = {}
    for line in env_path.read_text(errors="replace").splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'").strip()
        # If a setting appears twice, a filled-in line wins over an empty one.
        if value or key not in values:
            values[key] = os.path.expanduser(value) if value.startswith("~") else value
    for key, value in values.items():
        if not os.environ.get(key):
            os.environ[key] = value


def _web_url(raw: str) -> str:
    """Accept links pasted without https:// and strip stray spaces/quotes."""
    url = (raw or "").strip().strip('"').strip("'").strip()
    if url and "://" not in url:
        url = "https://" + url
    return url


def load_settings() -> Settings:
    load_env_file()
    project_id = os.environ.get("AUREOCLEAN_FIREBASE_PROJECT_ID", "").strip()
    mode = os.environ.get("AUREOCLEAN_MODE", "").strip().lower()
    if mode not in {"firebase", "local"}:
        mode = "firebase" if project_id else "local"

    data_dir = Path(
        os.environ.get("AUREOCLEAN_DATA_DIR", "~/.local/share/aureoclean")
    ).expanduser()

    return Settings(
        mode=mode,
        # The API must only ever listen on the loopback interface.
        host="127.0.0.1",
        port=_int("AUREOCLEAN_PORT", 8765),
        data_dir=data_dir,
        collection_prefix=os.environ.get("AUREOCLEAN_COLLECTION_PREFIX", "aureo").strip() or "aureo",
        firebase_project_id=project_id,
        firebase_web_api_key=os.environ.get("AUREOCLEAN_FIREBASE_WEB_API_KEY", "").strip(),
        credentials_path=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip(),
        session_hours=_int("AUREOCLEAN_SESSION_HOURS", 8),
        activation_code_hours=_int("AUREOCLEAN_ACTIVATION_HOURS", 48),
        admin_invite_hours=_int("AUREOCLEAN_INVITE_HOURS", 24),
        max_activation_attempts=_int("AUREOCLEAN_MAX_ACTIVATION_ATTEMPTS", 5),
        max_login_failures=_int("AUREOCLEAN_MAX_LOGIN_FAILURES", 5),
        login_lockout_minutes=_int("AUREOCLEAN_LOGIN_LOCKOUT_MINUTES", 15),
        sms_mode="modem" if os.environ.get("AUREOCLEAN_SMS_MODE", "").strip().lower() == "modem" else "log",
        sms_port=os.environ.get("AUREOCLEAN_SMS_PORT", "").strip() or "/dev/serial0",
        sms_baud=_int("AUREOCLEAN_SMS_BAUD", 9600),
        log_dir=Path(__file__).resolve().parent.parent / "logs",
        dashboard_url=_web_url(os.environ.get("AUREOCLEAN_DASHBOARD_URL", "")),
        arduino_cloud_url=os.environ.get("AUREOCLEAN_ARDUINO_CLOUD_URL", "").strip() or "https://app.arduino.cc/",
    )
