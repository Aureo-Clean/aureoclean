#!/usr/bin/env bash
# Kept so older "open at boot" entries still work: it just opens AureoClean in a
# normal window. Kiosk mode is now a switch inside the app (Tools → Kiosk mode).
cd "$(dirname "$0")/.." || exit 1
exec ./run_local.sh --maximized
