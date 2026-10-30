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
# PySide6 separately: if pip has no build for this Pi, Raspberry Pi OS's own packages are used below.
grep -v '^PySide6' requirements.txt > /tmp/aureoclean-req.txt
.venv/bin/pip install --prefer-binary "$(grep '^PySide6' requirements.txt)" \
  || echo "   (pip has no PySide6 for this Pi — Raspberry Pi OS's packages will be used.)"
if ! .venv/bin/pip install --prefer-binary -r /tmp/aureoclean-req.txt; then
  echo
  echo "!! Installing the Python packages failed (see the lines above)."
  echo "   Check the internet connection and run ./setup_pi.sh again."
  echo "   If it fails the same way twice, send the last 20 lines to your developer."
  exit 1
fi
sha256sum requirements.txt | cut -d' ' -f1 > .venv/.requirements-hash

echo "-- Google Chrome (opens Arduino Cloud / Firebase links)"
./tools/install_chrome.sh || true

echo "-- The app's screens (Qt WebEngine)"
if ! ./tools/ensure_webengine.sh; then
  echo "   Run ./setup_pi.sh again with internet. If it still fails, send the lines above to your developer."
  exit 1
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
  1. (Optional) Edit $ENV_FILE — it starts in LOCAL mode: everything is
     stored on this Pi, no Firebase needed. SMS works in both modes.
  2. Create the first admin:   .venv/bin/python tools/create_admin.py
  3. Start the app:            ./run_local.sh
  4. Desktop icons:            ./tools/install_launcher.sh
     (adds "AureoClean" and "Update AureoClean")
EOF
