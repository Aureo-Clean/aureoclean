#!/usr/bin/env bash
# Turns the Pi into an AureoClean kiosk:
#   1. The AureoClean service (backend: SMS, website imports, alerts) runs in the
#      background from boot and restarts by itself if it ever crashes.
#   2. The app opens full-screen at boot and reopens if it closes (tools/kiosk.sh).
#   3. The screen no longer goes blank (optional; asks for your Pi password).
#
#   ./tools/install_kiosk.sh            # set it all up
#   ./tools/install_kiosk.sh --remove   # back to normal (icon only)
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="aureoclean-backend.service"
AUTOSTART="$HOME/.config/autostart/aureoclean.desktop"

if [ "${1:-}" = "--remove" ]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT" "$AUTOSTART"
  systemctl --user daemon-reload 2>/dev/null || true
  pkill -f "tools/kiosk.sh" 2>/dev/null || true
  echo "Kiosk mode removed. Open AureoClean from the desktop icon as before."
  exit 0
fi

[ -x "$APP_DIR/.venv/bin/python" ] || { echo "Run ./setup_pi.sh first."; exit 1; }
chmod +x "$APP_DIR/tools/kiosk.sh" "$APP_DIR/run_local.sh"
mkdir -p "$APP_DIR/logs" "$UNIT_DIR" "$(dirname "$AUTOSTART")"

echo "== 1/3 Background service (restarts itself if it crashes)"
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
# Keep the service running even before anyone signs in to the Pi desktop.
loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER" 2>/dev/null \
  || echo "   (Couldn't turn on 'linger': the service will start when the desktop signs in. That's fine with auto-login.)"
sleep 3
if systemctl --user is-active --quiet "$UNIT"; then
  echo "   Service running."
else
  echo "   The service didn't start. Last lines of logs/backend.log:"
  tail -n 15 "$APP_DIR/logs/backend.log" || true
fi

echo "== 2/3 Open full-screen at boot"
cat > "$AUTOSTART" <<ENTRY
[Desktop Entry]
Type=Application
Name=AureoClean (kiosk)
Comment=Opens AureoClean full-screen and keeps it open
Exec="$APP_DIR/tools/kiosk.sh"
Path=$APP_DIR
Icon=$APP_DIR/desktop/assets/icon-256.png
Terminal=false
X-GNOME-Autostart-enabled=true
ENTRY
echo "   Added $AUTOSTART"

echo "== 3/3 Keep the screen on"
if command -v raspi-config >/dev/null; then
  if sudo raspi-config nonint do_blanking 1; then
    echo "   Screen blanking turned off (takes effect after a restart)."
  else
    echo "   Skipped. To do it later: sudo raspi-config → Display Options → Screen Blanking → No."
  fi
else
  echo "   raspi-config not found; turn off screen blanking in the display settings."
fi

cat <<EOF

Done. Restart the Pi to try it:  sudo reboot
 * AureoClean opens full-screen by itself (make sure the Pi signs in to the desktop
   automatically: sudo raspi-config → System Options → Boot / Auto Login → Desktop Autologin).
 * The screen locks after 5 minutes without a touch. Set a PIN in Account → Screen-lock PIN.
   Change the minutes with AUREOCLEAN_IDLE_LOCK_MINUTES in ~/.config/aureoclean/aureoclean.env.
 * To close the app on purpose: Tools → "Close AureoClean".
 * To undo all of this: ./tools/install_kiosk.sh --remove
EOF
