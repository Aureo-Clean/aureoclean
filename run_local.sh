#!/usr/bin/env bash
# Starts the local API (127.0.0.1 only) and then the desktop app.
# Extra arguments go to the desktop app, e.g.  ./run_local.sh --fullscreen
set -uo pipefail
cd "$(dirname "$0")"

PY=.venv/bin/python
PORT="${AUREOCLEAN_PORT:-8765}"
mkdir -p logs

if [ ! -x "$PY" ]; then
  echo "Virtual environment missing. Run ./setup_pi.sh first."
  exit 1
fi

health() {
  "$PY" - "$PORT" <<'EOF' >/dev/null 2>&1
import sys, urllib.request
urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f"http://127.0.0.1:{sys.argv[1]}/health", timeout=2)
EOF
}

# Kiosk mode: the service runs under systemd (tools/install_kiosk.sh). Wait for it
# instead of starting a second copy.
UNIT=aureoclean-backend.service
if ! health && systemctl --user is-enabled --quiet "$UNIT" 2>/dev/null; then
  echo "Waiting for the AureoClean service…"
  systemctl --user start "$UNIT" 2>/dev/null
  for _ in $(seq 1 60); do
    if health; then break; fi
    sleep 0.5
  done
fi

BACKEND_PID=""
if health; then
  echo "AureoClean service already running on port $PORT."
else
  echo "Starting AureoClean service (log: logs/backend.log)…"
  "$PY" -m backend.main >>logs/backend.log 2>&1 &
  BACKEND_PID=$!
  trap '[ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null' EXIT INT TERM

  for _ in $(seq 1 40); do
    if health; then break; fi
    if ! kill -0 "$BACKEND_PID" 2>/dev/null; then break; fi
    sleep 0.5
  done
  if ! health; then
    # Started from the desktop icon there is no terminal, so the app shows this instead.
    echo "The service failed to start. Last lines of logs/backend.log:"
    tail -n 25 logs/backend.log
    export AUREOCLEAN_STARTUP_ERROR="$(grep -v '^\s*$' logs/backend.log | tail -n 3)"
  fi
fi

echo "Opening the desktop app…"
"$PY" -m desktop.app "$@"
