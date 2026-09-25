#!/bin/bash
# Copy the firmware to the wearable and reset it.
#   scripts/flash.sh [port]      default port: first /dev/cu.usbmodem*
# A running HeatGuard server holds the serial port, so ask it to let go first
# and hand the port back afterwards (best effort; fine if no server is running).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API="${HG_API:-http://127.0.0.1:${PORT:-8000}}/api/v1/bridge"
FW="$ROOT/device/main.py"

DEV="${1:-${HG_PORT:-}}"
if [ -z "$DEV" ]; then
  for p in /dev/cu.usbmodem*; do
    [ -e "$p" ] && { DEV="$p"; break; }
  done
fi
[ -n "$DEV" ] || { echo "No wearable on /dev/cu.usbmodem*. Plug it in or pass the port." >&2; exit 1; }
[ -f "$FW" ] || { echo "missing $FW" >&2; exit 1; }

MP="$ROOT/.venv/bin/mpremote"
[ -x "$MP" ] || MP="$(command -v mpremote || true)"
[ -n "$MP" ] || { echo "mpremote not found (.venv/bin/pip install mpremote)" >&2; exit 1; }

bridge() {
  curl -fsS -m 3 -X POST -H 'Content-Type: application/json' -d "{\"enabled\": $1}" "$API" >/dev/null 2>&1
}

RELEASED=0
if bridge false; then
  RELEASED=1
  echo "Server released the serial port."
  sleep 1
fi
restore() {
  if [ "$RELEASED" = 1 ]; then
    sleep 3   # the board re-enumerates after the reset
    bridge true && echo "Server serial bridge re-enabled." || true
  fi
}
trap restore EXIT

echo "Flashing $FW -> $DEV:/flash/main.py"
"$MP" connect "$DEV" cp "$FW" :/flash/main.py + reset
echo "Done. The wearable is restarting."
