#!/usr/bin/env bash
# Installs Google Chrome on the Raspberry Pi (64-bit Raspberry Pi OS).
# AureoClean then opens Arduino Cloud / Firebase links in Chrome.
# (The dashboard itself is shown inside AureoClean and doesn't need Chrome.)
set -uo pipefail

if command -v google-chrome-stable >/dev/null || command -v google-chrome >/dev/null; then
  echo "Google Chrome is already installed."
  exit 0
fi

ARCH="$(dpkg --print-architecture)"
URL="https://dl.google.com/linux/direct/google-chrome-stable_current_${ARCH}.deb"
TMP="$(mktemp --suffix=.deb)"

echo "Downloading Google Chrome for $ARCH…"
if wget -q --show-progress -O "$TMP" "$URL" && [ -s "$TMP" ]; then
  echo "Installing (needs your Pi password)…"
  sudo apt-get install -y "$TMP" && echo "Done. Google Chrome is installed."
else
  echo
  echo "Couldn't download Chrome automatically."
  echo "Open https://www.google.com/chrome/ in the browser, download the 64-bit ARM .deb,"
  echo "then run:  sudo apt install ~/Downloads/google-chrome-stable_*_arm64.deb"
fi
rm -f "$TMP"
