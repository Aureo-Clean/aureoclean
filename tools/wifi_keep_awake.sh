#!/usr/bin/env bash
# Stops the Raspberry Pi's Wi-Fi from going into power-saving mode, which can
# make the first request after a quiet period fail ("Cannot reach Firebase").
# Run once; it stays set for this Wi-Fi network.
set -uo pipefail

if ! command -v nmcli >/dev/null; then
  echo "NetworkManager (nmcli) not found. Run instead:  sudo iw wlan0 set power_save off"
  exit 1
fi
CON="$(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2 ~ /wireless/ {print $1; exit}')"
if [ -z "$CON" ]; then
  echo "No active Wi-Fi connection found. Connect to Wi-Fi first."
  exit 1
fi
echo "Turning off Wi-Fi power saving for \"$CON\" (needs your Pi password)…"
sudo nmcli connection modify "$CON" 802-11-wireless.powersave 2 \
  && sudo nmcli connection up "$CON" >/dev/null \
  && echo "Done. Wi-Fi will stay awake on \"$CON\"."
