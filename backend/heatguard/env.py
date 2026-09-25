"""HeatGuard's one settings loader.

Reads `HEATGUARD_ENV_FILE` if set, else `backend/.env`, else the repo-root `.env`, into
os.environ. Real environment variables always win (Cloud Run sets everything as env vars).
Short key names are mapped to the ones the code reads. Never writes files, never logs values.
"""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BACKEND_DIR.parent

ALIASES = {
    "DEVIN": "DEVIN_API_KEY",
    "OPENAI": "OPENAI_API_KEY",
    "TWILLIO_SID": "TWILIO_ACCOUNT_SID",
    "TWILIO_SID": "TWILIO_ACCOUNT_SID",
    "TWILLIO_KEY": "TWILIO_AUTH_TOKEN",
    "TWILIO_KEY": "TWILIO_AUTH_TOKEN",
}


def env_file() -> Path | None:
    explicit = os.environ.get("HEATGUARD_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    for p in (BACKEND_DIR / ".env", REPO_ROOT / ".env"):
        if p.is_file():
            return p
    return None


def load_env() -> Path | None:
    """Load the settings file (if any) without overriding real env vars. Returns its path."""
    p = env_file()
    if p is not None and p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().removeprefix("export ").strip()
            os.environ.setdefault(ALIASES.get(k, k), v.strip().strip('"').strip("'"))
    else:
        p = None
    # Aliases set as real env vars count too.
    for short, full in ALIASES.items():
        if os.environ.get(short) and not os.environ.get(full):
            os.environ[full] = os.environ[short]
    return p
