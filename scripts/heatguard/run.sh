#!/bin/bash
# Start the HeatGuard gateway server (dashboard, device gateway, escalation).
#
# Keys are loaded at runtime, never echoed. Precedence, highest first:
#   1. variables already exported in the environment (systemd EnvironmentFile, shell)
#   2. the project's .env            (DEVIN=..., OPENAI=...)
#   3. $HOME/secrets/secrets.env (openai_key=...), or $HEATGUARD_SECRETS_FILE
# Only an allowlist of keys is taken from those files; everything else is ignored.
# The files are parsed as KEY=VALUE lines, never sourced.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_FILE="${HEATGUARD_SECRETS_FILE:-$HOME/secrets/secrets.env}"
LOADED=""

# Map a file key to the env var the server reads; empty output = not allowed.
target_name() {
  case "$1" in
    DEVIN|DEVIN_API_KEY) echo DEVIN_API_KEY ;;
    OPENAI|OPENAI_API_KEY|openai_key) echo OPENAI_API_KEY ;;
    TWILLIO_SID|TWILIO_SID) echo TWILIO_ACCOUNT_SID ;;
    TWILLIO_KEY|TWILIO_KEY) echo TWILIO_AUTH_TOKEN ;;
    DEVIN_*|OPENAI_*|WHATSAPP_*|TWILIO_*|META_WA_*|HEATGUARD_*|CALLMEBOT_APIKEY|PORT) echo "$1" ;;
    *) echo "" ;;
  esac
}

load_file() {
  local file="$1" tag="$2" line key val name q
  [ -r "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"          # trim leading whitespace
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    key="${key%"${key##*[![:space:]]}"}"             # trim trailing whitespace
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    name="$(target_name "$key")"
    [ -n "$name" ] || continue
    val="${val#"${val%%[![:space:]]*}"}"
    q="${val:0:1}"
    if [ "$q" = '"' ] || [ "$q" = "'" ]; then          # quoted: take up to the closing quote
      val="${val:1}"; val="${val%%"$q"*}"
    else                                               # unquoted: drop a trailing " # comment"
      val="${val%%[[:space:]]#*}"
      val="${val%"${val##*[![:space:]]}"}"
    fi
    [ -n "$val" ] || continue
    if [ -z "${!name:-}" ]; then                       # never override what is already set
      export "$name=$val"
      LOADED="$LOADED $name($tag)"
    fi
  done < "$file"
}

# Short names may also arrive via the environment (e.g. a systemd EnvironmentFile).
if [ -z "${DEVIN_API_KEY:-}" ] && [ -n "${DEVIN:-}" ]; then export DEVIN_API_KEY="$DEVIN"; LOADED="$LOADED DEVIN_API_KEY(env)"; fi
if [ -z "${OPENAI_API_KEY:-}" ] && [ -n "${OPENAI:-}" ]; then export OPENAI_API_KEY="$OPENAI"; LOADED="$LOADED OPENAI_API_KEY(env)"; fi

load_file "$ROOT/.env" ".env"
load_file "$SECRETS_FILE" "secrets"

# Names only, never values.
echo "heatguard: keys loaded:${LOADED:- none}" >&2
[ -n "${OPENAI_API_KEY:-}" ] || echo "heatguard: OPENAI_API_KEY not set, voice assistant will be offline" >&2
[ -n "${DEVIN_API_KEY:-}" ] || echo "heatguard: DEVIN_API_KEY not set, incident analysis uses the local analyser" >&2

export PYTHONUNBUFFERED=1
cd "$ROOT/server"
exec ../.venv/bin/uvicorn app:app --host 0.0.0.0 --port "${PORT:-8000}" --timeout-graceful-shutdown 3
