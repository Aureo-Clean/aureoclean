#!/usr/bin/env bash
# One-click AureoClean update (used by the "Update AureoClean" desktop icon).
#
# Where the new version comes from:
#   * If this folder is a GitHub copy (git clone): downloads the latest changes.
#   * Otherwise: uses the newest aureoclean_app*.zip in ~/Downloads.
#
# It never touches your settings (~/.config/aureoclean), accounts, the
# service-account key, the .venv or the logs. Python packages are only
# reinstalled when requirements.txt actually changed.
set -uo pipefail

APP_DIR="${AUREO_APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
# This script may replace itself while updating, so run from a temporary copy.
if [ -z "${AUREO_UPDATE_COPY:-}" ]; then
  COPY="$(mktemp)"
  cp "$0" "$COPY"
  AUREO_UPDATE_COPY=1 AUREO_APP_DIR="$APP_DIR" exec bash "$COPY" "$@"
fi
trap 'rm -f "$0"' EXIT
cd "$APP_DIR"
PY=.venv/bin/python

say()  { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m%s\033[0m\n' "$*"; read -rp "Press Enter to close…" _; exit 1; }

say "Closing AureoClean"
pkill -f "desktop.app" 2>/dev/null
pkill -f "backend.main" 2>/dev/null
sleep 1

if [ -d .git ]; then
  say "Downloading the latest version from GitHub"
  git fetch --quiet origin || fail "Couldn't reach GitHub. Check the internet connection."
  BEFORE="$(git rev-parse HEAD)"
  git reset --quiet --hard "origin/$(git rev-parse --abbrev-ref HEAD)" || fail "Update failed."
  if [ "$BEFORE" = "$(git rev-parse HEAD)" ]; then
    echo "Already up to date."
  else
    git --no-pager log --oneline "$BEFORE..HEAD" | sed 's/^/  new: /'
  fi
else
  ZIP="$(ls -t "$HOME"/Downloads/aureoclean_app*.zip 2>/dev/null | head -n 1)"
  [ -n "$ZIP" ] || fail "No aureoclean_app.zip found in Downloads. Download the new version first."
  say "Installing $(basename "$ZIP") ($(date -r "$ZIP" '+%b %d %H:%M'))"
  TMP="$(mktemp -d)"
  unzip -q -o "$ZIP" -d "$TMP" || fail "The zip file is damaged. Download it again."
  SRC="$TMP/aureoclean"
  [ -d "$SRC/backend" ] || fail "That zip doesn't look like AureoClean."
  # Copy the new code over the old, leaving .venv, logs and settings alone.
  cp -a "$SRC"/. "$APP_DIR"/
  rm -rf "$TMP"
fi
chmod +x run_local.sh setup_pi.sh tools/*.sh 2>/dev/null

# Reinstall Python packages only when the list changed.
REQ_HASH="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ ! -x "$PY" ]; then
  fail "The app was never set up on this Pi. Run ./setup_pi.sh once first."
elif [ "$(cat .venv/.requirements-hash 2>/dev/null)" != "$REQ_HASH" ]; then
  say "Installing new Python packages (only needed this time)"
  .venv/bin/pip install -q -r requirements.txt || fail "Installing packages failed. Check the internet connection."
  echo "$REQ_HASH" > .venv/.requirements-hash
fi

# The dashboard is shown inside the app: make sure its web viewer is installed.
if ! "$PY" -c "import PySide6.QtWebEngineWidgets" 2>/dev/null; then
  say "Installing the in-app dashboard viewer (only needed this time)"
  VER="$("$PY" -c 'import importlib.metadata as m; print(m.version("PySide6"))' 2>/dev/null)"
  if [ -z "$VER" ] || ! .venv/bin/pip install -q "PySide6-Addons[webengine]==$VER"; then
    echo "Couldn't install it automatically; the Dashboard tab will explain what to do."
  fi
fi

# Keep the desktop icons up to date (quietly).
./tools/install_launcher.sh >/dev/null 2>&1 || true

say "Done — starting AureoClean"
setsid ./run_local.sh --maximized >/dev/null 2>&1 < /dev/null &
sleep 2
