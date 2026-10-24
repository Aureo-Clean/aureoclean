#!/usr/bin/env bash
# Kiosk mode: keeps AureoClean open full-screen on the 7" display.
#   * If the app crashes or is closed by anything else, it opens again.
#   * It stays closed only when an admin uses Tools → "Close AureoClean".
#   * While "Update AureoClean" runs, it waits and then opens the new version.
# Started at boot by ~/.config/autostart/aureoclean.desktop (tools/install_kiosk.sh).
set -uo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
mkdir -p logs
LOG=logs/kiosk.log
quick_fails=0

# Only one kiosk loop at a time.
exec 9>"$APP_DIR/logs/.kiosk.lock"
if ! flock -n 9; then
  echo "Kiosk mode is already running." >&2
  exit 0
fi

while true; do
  while [ -e "$APP_DIR/.updating" ]; do sleep 2; done
  started=$(date +%s)
  echo "$(date '+%F %T') opening AureoClean" >>"$LOG"
  ./run_local.sh --kiosk >>logs/desktop.log 2>&1
  code=$?
  if [ "$code" -eq 0 ] && [ ! -e "$APP_DIR/.updating" ]; then
    echo "$(date '+%F %T') closed by an admin; kiosk stopped" >>"$LOG"
    exit 0
  fi
  echo "$(date '+%F %T') app stopped (exit code $code); reopening" >>"$LOG"
  # If it keeps crashing straight away, wait longer so the Pi isn't stuck in a tight loop.
  if [ $(( $(date +%s) - started )) -lt 30 ]; then quick_fails=$((quick_fails + 1)); else quick_fails=0; fi
  if [ "$quick_fails" -ge 5 ]; then sleep 30; else sleep 3; fi
done
