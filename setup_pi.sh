#!/usr/bin/env bash
# One-time setup on the Raspberry Pi (Raspberry Pi OS 64-bit, Bookworm or newer).
set -euo pipefail
cd "$(dirname "$0")"

CONFIG_DIR="$HOME/.config/aureoclean"
ENV_FILE="$CONFIG_DIR/aureoclean.env"
KEY_FILE="$CONFIG_DIR/service-account.json"

echo "== AureoClean setup =="

if [ "$(uname -m)" != "aarch64" ] && [ "$(uname -m)" != "x86_64" ]; then
  echo "Warning: $(uname -m) detected. PySide6 needs a 64-bit OS (aarch64) on the Raspberry Pi."
fi

echo "-- Installing system packages (needs sudo)"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip libxcb-cursor0 libxkbcommon-x11-0 libegl1

echo "-- Creating Python virtual environment in .venv"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
sha256sum requirements.txt | cut -d' ' -f1 > .venv/.requirements-hash

echo "-- Google Chrome (opens Arduino Cloud / Firebase links)"
./tools/install_chrome.sh || true

echo "-- In-app dashboard viewer (Qt WebEngine)"
PYSIDE_VER="$(.venv/bin/python -c 'import importlib.metadata as m; print(m.version("PySide6"))')"
if .venv/bin/pip install "PySide6-Addons[webengine]==$PYSIDE_VER" >/dev/null 2>&1 \
   && .venv/bin/python -c "import PySide6.QtWebEngineWidgets" 2>/dev/null; then
  echo "   Installed: the Arduino dashboard shows inside AureoClean."
else
  echo "   WARNING: couldn't install it. The Dashboard tab needs it - tell your developer."
fi

echo "-- Preparing $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"
if [ ! -f "$ENV_FILE" ]; then
  cp aureoclean.env.example "$ENV_FILE"
  echo "   Created $ENV_FILE - open it and fill in the Web API key."
fi
chmod 600 "$ENV_FILE"

if [ -f "$KEY_FILE" ]; then
  chmod 600 "$KEY_FILE"
  echo "   Service-account key found at $KEY_FILE (permissions set to owner-only)."
else
  echo "   No service-account key at $KEY_FILE yet."
  echo "   Download it from Firebase Console > Project settings > Service accounts, and move it there."
fi

# Allow this user to talk to the SMS modem on the serial port.
if ! id -nG "$USER" | grep -qw dialout; then
  sudo usermod -aG dialout "$USER"
  echo "   Added $USER to the 'dialout' group (serial port access). Takes effect after a reboot."
fi

mkdir -p logs
chmod +x run_local.sh tools/*.sh

cat <<EOF

Setup finished. Next:
  1. Edit $ENV_FILE  (Web API key, mode)
  2. Create the first admin:   .venv/bin/python tools/create_admin.py
     (or, for an email that already has a Firebase account:
                               .venv/bin/python tools/create_admin.py --link-existing)
  3. Start the app:            ./run_local.sh
  4. Desktop icons:            ./tools/install_launcher.sh
     (adds "AureoClean" and "Update AureoClean")
EOF
