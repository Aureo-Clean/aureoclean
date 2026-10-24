#!/usr/bin/env bash
# Adds an AureoClean icon to the Raspberry Pi menu and desktop
# (the Linux equivalent of a Windows .exe shortcut).
#
#   ./tools/install_launcher.sh               # menu + desktop icon
#   ./tools/install_launcher.sh --autostart   # also open AureoClean when the Pi starts
#   ./tools/install_launcher.sh --remove      # remove everything this script added
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NAME="aureoclean.desktop"
UPDATE_NAME="aureoclean-update.desktop"
MENU_DIR="$HOME/.local/share/applications"
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
AUTOSTART_DIR="$HOME/.config/autostart"

if [ "${1:-}" = "--remove" ]; then
  rm -f "$MENU_DIR/$NAME" "$DESKTOP_DIR/$NAME" "$AUTOSTART_DIR/$NAME" "$MENU_DIR/$UPDATE_NAME" "$DESKTOP_DIR/$UPDATE_NAME"
  echo "AureoClean icons removed."
  exit 0
fi

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  echo "Run ./setup_pi.sh first."
  exit 1
fi

# A personal logo kept only on this Pi (~/.config/aureoclean/logo.png) replaces the icon.
ICON="$APP_DIR/desktop/assets/icon-256.png"
[ -f "$HOME/.config/aureoclean/logo.png" ] && ICON="$HOME/.config/aureoclean/logo.png"

ENTRY="[Desktop Entry]
Type=Application
Name=AureoClean
Comment=Restroom hygiene monitoring
Exec=\"$APP_DIR/run_local.sh\" --maximized
Path=$APP_DIR
Icon=$ICON
Terminal=false
Categories=Utility;
StartupNotify=true
"

# Opens a terminal window so you can watch the update happen.
UPDATE_ENTRY="[Desktop Entry]
Type=Application
Name=Update AureoClean
Comment=Install the newest AureoClean version
Exec=\"$APP_DIR/tools/update.sh\"
Path=$APP_DIR
Icon=system-software-update
Terminal=true
Categories=Utility;
"

install_entry() {  # $1 = file name, $2 = contents
  mkdir -p "$MENU_DIR"
  printf '%s' "$2" > "$MENU_DIR/$1"
  chmod +x "$MENU_DIR/$1"
  if [ -d "$DESKTOP_DIR" ]; then
    printf '%s' "$2" > "$DESKTOP_DIR/$1"
    chmod +x "$DESKTOP_DIR/$1"
    gio set "$DESKTOP_DIR/$1" metadata::trusted true 2>/dev/null || true
  fi
}

install_entry "$NAME" "$ENTRY"
install_entry "$UPDATE_NAME" "$UPDATE_ENTRY"

# Ask the desktop to redraw so a changed icon shows straight away.
command -v pcmanfm >/dev/null && pcmanfm --reconfigure >/dev/null 2>&1 || true
echo "Added AureoClean and Update AureoClean to the desktop and the menu (under Accessories)."

if [ "${1:-}" = "--autostart" ]; then
  mkdir -p "$AUTOSTART_DIR"
  printf '%s' "$ENTRY" | sed 's/--maximized/--fullscreen/' > "$AUTOSTART_DIR/$NAME"
  echo "AureoClean will open full-screen when the Pi starts."
fi

cat <<EOF

Done. Double-click the AureoClean icon on the desktop.
If the Pi asks "Execute / Open / Cancel", choose Execute.
(To stop that question: File Manager > Edit > Preferences > General >
 tick "Don't ask options on launch executable file".)
EOF
