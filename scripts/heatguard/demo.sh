#!/usr/bin/env bash
# Starts the whole demo stack in the foreground; Ctrl-C stops everything.
#
#   1. cloudflared quick tunnel  -> live_call/call_service.py on :8011 (Twilio must reach it)
#   2. live_call/call_service.py (GPT-Live phone calls), with a per-run API token
#   3. HeatGuard server on :8000 (dashboard, gateway, escalation), told how to reach 2.
#
# Only the call service is exposed through the tunnel, and its /calls API needs the
# token; the dashboard stays on the local network. The token lives in this process's
# environment only and is never written to disk. Keys come from .env via run.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIVE_PORT="${LIVE_CALL_PORT:-8011}"
LOGDIR="${HEATGUARD_LOG_DIR:-$ROOT/.logs}"
mkdir -p "$LOGDIR"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

CLOUDFLARED="$(command -v cloudflared || echo "$ROOT/live_call/bin/cloudflared")"
"$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$LIVE_PORT" > "$LOGDIR/tunnel.log" 2>&1 &
PIDS+=($!)
URL=""
for _ in $(seq 1 60); do
  URL="$(grep -oE 'https://[-a-z0-9]+\.trycloudflare\.com' "$LOGDIR/tunnel.log" | head -1 || true)"
  [ -n "$URL" ] && break
  sleep 0.5
done
[ -n "$URL" ] || { echo "demo: tunnel did not come up, see $LOGDIR/tunnel.log" >&2; exit 1; }
echo "demo: call service public URL $URL" >&2

export LIVE_CALL_PUBLIC_URL="$URL"
export LIVE_CALL_API_TOKEN="${LIVE_CALL_API_TOKEN:-$(openssl rand -hex 24)}"
export LIVE_CALL_URL="http://127.0.0.1:$LIVE_PORT"
# Trial accounts call from a Twilio-owned number that is not listed as purchased;
# reuse the one this account last called the alert recipient from.
if [ -z "${TWILIO_FROM_NUMBER:-}" ]; then
  TWILIO_FROM_NUMBER="$(cd "$ROOT/server" && ../.venv/bin/python -c '
import app, asyncio
from services.voice_call import VoiceCall
v = VoiceCall(); asyncio.run(v.setup()); print(v.frm or "")' 2>/dev/null | tail -1)"
  export TWILIO_FROM_NUMBER
fi

(cd "$ROOT/live_call" && exec ../.venv/bin/python call_service.py --port "$LIVE_PORT") > "$LOGDIR/live_call.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 30); do curl -s -m 1 "http://127.0.0.1:$LIVE_PORT/health" >/dev/null && break; sleep 0.5; done

"$ROOT/scripts/run.sh" &
SERVER_PID=$!
PIDS+=("$SERVER_PID")
wait "$SERVER_PID"
