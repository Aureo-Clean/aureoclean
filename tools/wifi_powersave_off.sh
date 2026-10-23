#!/usr/bin/env bash
# Turns off Wi-Fi power saving on the Raspberry Pi (run once; needs your password).
# With power saving on, the Wi-Fi can doze and background programs (like the
# AureoClean backend) lose the internet until something like a browser wakes it.
set -euo pipefail

CONF=/etc/NetworkManager/conf.d/aureoclean-wifi-powersave-off.conf
if command -v nmcli >/dev/null; then
  printf '[connection]\n# 2 = disable Wi-Fi power saving\nwifi.powersave = 2\n' | sudo tee "$CONF" >/dev/null
  sudo systemctl restart NetworkManager
  echo "Wi-Fi power saving is now OFF (kept after reboots)."
else
  sudo iw dev wlan0 set power_save off
  echo "Wi-Fi power saving turned off until the next reboot (NetworkManager not found)."
fi
iw dev wlan0 get power_save 2>/dev/null || true
