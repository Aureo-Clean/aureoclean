"""Kiosk mode on/off: the autostart entry that opens AureoClean full-screen at boot."""
from __future__ import annotations

from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
AUTOSTART = Path.home() / ".config" / "autostart" / "aureoclean.desktop"


def is_enabled() -> bool:
    try:
        return "kiosk.sh" in AUTOSTART.read_text()
    except OSError:
        return False


def enable() -> None:
    AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
    AUTOSTART.write_text(f"""[Desktop Entry]
Type=Application
Name=AureoClean (kiosk)
Comment=Opens AureoClean full-screen and keeps it open
Exec="{APP_DIR / 'tools' / 'kiosk.sh'}"
Path={APP_DIR}
Icon={APP_DIR / 'desktop' / 'assets' / 'icon-256.png'}
Terminal=false
X-GNOME-Autostart-enabled=true
""")


def disable() -> None:
    """Stop opening full-screen at boot. The background service keeps running."""
    if is_enabled():
        AUTOSTART.unlink()
