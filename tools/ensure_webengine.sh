#!/usr/bin/env bash
# Make sure the app's screens can open: PySide6 with Qt WebEngine inside .venv.
# Used by setup_pi.sh and update.sh. Exit 0 = OK, 1 = still missing (the app would
# fall back to its old classic screens).
#
# 1) pip's PySide6-Addons for the installed PySide6 version, else
# 2) Raspberry Pi OS's own packages (Trixie has python3-pyside6.* with WebEngine),
#    with .venv switched to "see system packages" and pip's PySide6 removed.
cd "$(dirname "$0")/.."
PY=.venv/bin/python
ok() { "$PY" -c "import PySide6.QtWebEngineWidgets" 2>/dev/null; }
ok && { echo "   OK: Qt WebEngine is installed (the new screens will open)."; exit 0; }

VER="$("$PY" -c 'import importlib.metadata as m; print(m.version("PySide6"))' 2>/dev/null)"
if [ -n "$VER" ]; then
  .venv/bin/pip install -q --prefer-binary "PySide6-Addons==$VER" >/dev/null 2>&1 || true
fi
ok && { echo "   OK: Qt WebEngine installed."; exit 0; }

if apt-cache show python3-pyside6.qtwebenginewidgets >/dev/null 2>&1; then
  echo "   Using Raspberry Pi OS's own PySide6 + Qt WebEngine packages…"
  sudo apt-get install -y python3-pyside6.qtwebenginewidgets python3-pyside6.qtwebenginecore \
       python3-pyside6.qtwidgets python3-pyside6.qtgui python3-pyside6.qtcore python3-pyside6.qtnetwork \
       python3-pyside6.qtprintsupport >/dev/null || true
  .venv/bin/pip uninstall -y PySide6 PySide6-Essentials PySide6-Addons shiboken6 >/dev/null 2>&1 || true
  python3 -m venv --system-site-packages --upgrade .venv >/dev/null
  touch .venv/.system-pyside6
fi
ok && { echo "   OK: Qt WebEngine from Raspberry Pi OS."; exit 0; }

echo "!! Qt WebEngine is still missing, so AureoClean can only open its OLD classic screens."
"$PY" -c "import PySide6.QtWebEngineWidgets" 2>&1 | tail -2
exit 1
