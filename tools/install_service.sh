#!/usr/bin/env bash
# Runs the AureoClean service (texts, SMS replies, website reports, alerts) in the
# background from boot, and restarts it by itself if it ever crashes. The app
# window then only shows things; closing it doesn't stop alerts.
#
#   ./tools/install_service.sh              # background service
#   ./tools/install_service.sh --autostart  # also open the app (normal window) at boot
#   ./tools/install_service.sh --remove     # back to how it was
#
# Kiosk mode (full screen, idle lock) is a switch inside the app: Tools → Kiosk mode.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="aureoclean-backend.service"
AUTOSTART="$HOME/.config/autostart/aureoclean.desktop"

if [ "${1:-}" = "--remove" ]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT" "$AUTOSTART"
  systemctl --user daemon-reload 2>/dev/null || true
  echo "Background service and open-at-boot removed. Open AureoClean from the desktop icon."
  exit 0
fi

[ -x "$APP_DIR/.venv/bin/python" ] || { echo "Run ./setup_pi.sh first."; exit 1; }
mkdir -p "$APP_DIR/logs" "$UNIT_DIR"

pkill -f "backend.main" 2>/dev/null || true   # an old copy started by the icon
cat > "$UNIT_DIR/$UNIT" <<UNIT
[Unit]
Description=AureoClean service (SMS, website imports, alerts)
After=network-online.target
StartLimitIntervalSec=0

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m backend.main
Restart=always
RestartSec=5
StandardOutput=append:$APP_DIR/logs/backend.log
StandardError=append:$APP_DIR/logs/backend.log

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" 2>/dev/null || true
sleep 3
if systemctl --user is-active --quiet "$UNIT"; then
  echo "Background service running."
else
  echo "The service didn't start. Last lines of logs/backend.log:"
  tail -n 15 "$APP_DIR/logs/backend.log" || true
fi

if [ "${1:-}" = "--autostart" ]; then
  "$APP_DIR/tools/install_launcher.sh" --autostart >/dev/null
  sed -i 's/--fullscreen/--maximized/' "$AUTOSTART"
  echo "AureoClean will open (normal window) when the Pi starts."
fi
