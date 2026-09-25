#!/bin/bash
# Manage the HeatGuard server LaunchAgent (macOS).
#   scripts/install_service.sh install | uninstall | status | logs
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.heatguard.server"
SRC="$ROOT/deploy/$LABEL.plist"
DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/heatguard"
LOG="$LOG_DIR/server.log"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
HTTP_PORT="${PORT:-8000}"

usage() {
  echo "usage: $(basename "$0") install|uninstall|status|logs" >&2
  exit 2
}

loaded() {
  launchctl print "$SERVICE" >/dev/null 2>&1
}

cmd_install() {
  [ -f "$SRC" ] || { echo "missing $SRC" >&2; exit 1; }
  [ -x "$ROOT/.venv/bin/uvicorn" ] || { echo "missing $ROOT/.venv/bin/uvicorn (create the venv first)" >&2; exit 1; }
  plutil -lint "$SRC" >/dev/null
  mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
  chmod +x "$ROOT/scripts/run.sh"
  if loaded; then
    echo "stopping the running service"
    launchctl bootout "$SERVICE" 2>/dev/null || true
    sleep 1
  fi
  cp "$SRC" "$DST"
  launchctl bootstrap "$DOMAIN" "$DST"
  launchctl enable "$SERVICE" 2>/dev/null || true
  echo "installed $LABEL (starts at login, restarts if it exits)"
  echo "logs: $LOG"
  echo "dashboard: http://localhost:$HTTP_PORT"
}

cmd_uninstall() {
  if loaded; then
    launchctl bootout "$SERVICE" 2>/dev/null || true
    echo "stopped $LABEL"
  fi
  if [ -f "$DST" ]; then
    rm "$DST"
    echo "removed $DST"
  fi
  echo "uninstalled (logs kept in $LOG_DIR)"
}

cmd_status() {
  if loaded; then
    # Only the lifecycle fields; the full dump is not needed here.
    launchctl print "$SERVICE" | grep -E '^\s*(state|pid|last exit code|runs) =' || true
  else
    echo "$LABEL is not loaded"
  fi
  code="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$HTTP_PORT/api/v1/state" || true)"
  echo "http://127.0.0.1:$HTTP_PORT/api/v1/state -> ${code:-no answer}"
}

cmd_logs() {
  [ -f "$LOG" ] || { echo "no log yet at $LOG" >&2; exit 1; }
  tail -n 200 -f "$LOG"
}

case "${1:-}" in
  install) cmd_install ;;
  uninstall) cmd_uninstall ;;
  status) cmd_status ;;
  logs) cmd_logs ;;
  *) usage ;;
esac
