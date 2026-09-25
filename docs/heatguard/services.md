# HeatGuard internal contracts

Layout:

```
device/main.py              MicroPython firmware (StickS3)
server/app.py               FastAPI app + Core (state, heat engine, alerts, SSE)      -- owner: core
server/heat.py              WBGT + ACGIH work/rest + UAE midday break                 -- done
server/sim.py               simulated fleet                                           -- done
server/devin.py             Devin v3 incident analysis + local fallback               -- done
server/gateway.py           UDP :47800 gateway, beacon :47801, USB serial fallback    -- done
server/services/assistant.py  OpenAI Realtime voice relay, TCP :47802                -- owner: assistant agent
server/services/whatsapp.py   WhatsApp escalation with location                       -- owner: whatsapp agent
server/static/index.html    dashboard                                                 -- owner: dashboard agent
deploy/, scripts/, README.md                                                          -- owner: whatsapp agent
```

Python: `.venv/bin/python` (3.13). Installed: fastapi, uvicorn, httpx, pyserial, mpremote, websockets 17. No numpy.

Ports: HTTP 8000, device UDP 47800, discovery beacon UDP 47801 (broadcast), voice TCP 47802.

---

## 1. Discovery beacon (server → LAN broadcast, UDP 47801, every 2 s)

```json
{"hg": 1, "udp": 47800, "http": 8000, "voice": 47802}
```
The device takes the sender IP as the server IP.

## 2. Device ↔ server telemetry (UDP 47800)

Each datagram is one JSON object, optionally prefixed with `@`. Same messages as the USB serial protocol in `docs/api.md` §2:

- `{"k":"hello","id":"stick-7ce8b1e3","fw":"heatguard-0.4","ip":"10.20.23.50","params":{...}}` every 10 s
- `{"k":"s","id":...,"t0":<device ms>,"dt":20,"d":[[ax,ay,az,gx,gy,gz] x5]}` 10/s
- `{"k":"st","id":...,"temp":..,"bat":..,"chg":..,"act":..,"wl":..,"am":..,"gm":..,"trem_hz":..,"trem_amp":..,"still_s":..,"phase":..,"ui":..,"free":..,"rssi":-60}` 1/s
- `{"k":"ev","id":...,"seq":17,"type":"fall","t":<device ms>,"detail":{...}}`: resent every 1 s until acked

Server → device (UDP datagram to the address the device sent from): the same `{"cmd": ...}` objects as in api.md §2, plus:

- `{"cmd":"evack","seq":17}`: stop resending event 17
- `{"cmd":"clear"}`: return the screen to normal (alert resolved by supervisor)
- `{"cmd":"say","text":"..."}`: show assistant text on screen (optional)

## 3. Voice (push-to-talk), TCP 47802

One TCP connection per question. The worker holds BtnB, speaks, and releases.

Framing, both directions: `type (1 byte ASCII) | length (2 bytes big-endian) | payload`.

Device → server:
1. First frame `H` (header): JSON `{"id":"stick-7ce8b1e3","rate":24000,"fmt":"pcm16"}`
2. `A` frames: raw PCM16 little-endian mono at 24 kHz, ≤ 4800 bytes each, streamed while the button is held
3. `E` frame (length 0): end of utterance (button released)

Server → device:
- `T` frames: UTF-8 text to show on screen (short: what the assistant is saying, ≤ 120 bytes per frame; a new `T` replaces the old one)
- `A` frames: PCM16 LE mono 24 kHz reply audio, ≤ 4800 bytes each, streamed as it arrives
- `E` frame: reply finished. Server closes the socket after it.
- `X` frame: error text (e.g. "assistant offline"). Server closes after it.

24 kHz is used end-to-end because it is the OpenAI Realtime API's native PCM rate, so nothing gets resampled.

## 4. Core interface used by services (implemented in server/app.py)

```python
class Core:
    loop: asyncio.AbstractEventLoop

    def worker_context(self, device_id: str) -> dict:
        """{'worker': Worker, 'site': Site, 'open_alerts': [Alert...], 'known': bool}
        Worker/Site/Alert shapes as in docs/api.md."""

    def publish_voice(self, entry: dict) -> None:
        """Push one voice-log line to the dashboard (SSE event 'voice', upsert by id).
        entry = {'id': 'V-<turn>-worker'|'V-<turn>-assistant'|'V-<turn>-tool', 'turn_id': str,
                 'worker_id': str, 'worker_name': str, 'role': 'worker'|'assistant'|'tool',
                 'text': str, 'final': bool, 'ts': float}"""

    async def assistant_tool(self, device_id: str, name: str, args: dict) -> dict:
        """Tools the voice assistant may call. Returns a JSON-able result for the model.
        name in:
          'report_symptoms'  {'symptoms': str, 'severity': 'mild'|'moderate'|'severe'}
                             -> creates a warning alert (critical if severe) and starts a cool-down
          'request_help'     {'reason': str}
                             -> creates a critical alert, sends WhatsApp escalation
          'start_cool_down'  {'minutes': int}
                             -> puts the worker on a cool-down now
          'get_heat_status'  {}
                             -> current WBGT, category, phase, minutes worked/left, rest remaining"""

    async def notify_alert(self, alert_id: str, reason: str = "") -> list[dict]:
        """Send WhatsApp escalation for an alert (used by core itself + /alerts/{id}/notify)."""
```

## 5. WhatsApp service (server/services/whatsapp.py)

```python
class WhatsApp:
    def __init__(self): ...           # reads env, see below
    provider: str                      # 'dryrun' | 'twilio' | 'meta' | 'callmebot'
    enabled: bool                      # True when provider != 'dryrun' and credentials are present
    recipients_masked: list[str]       # e.g. ['+9715•••••123']

    def describe(self) -> dict:        # {'provider','enabled','dry_run','recipients':[masked]}

    def compose(self, alert: dict, worker: dict | None, site: dict) -> tuple[str, dict]:
        """Returns (text, location). location = {'lat','lon','label','maps_url'}"""

    async def send_incident(self, alert: dict, worker: dict | None, site: dict) -> list[dict]:
        """One notification per recipient:
        {'id': 'N-000001', 'channel': 'whatsapp', 'provider': str, 'to': masked,
         'status': 'sent'|'failed'|'dry_run', 'alert_id': str, 'text': str,
         'location': {...}, 'ts': float, 'error': str|None, 'provider_id': str|None}
        Never raises; failures come back as status 'failed'."""
```

Env (all optional; missing = dry run):
`WHATSAPP_PROVIDER`, `WHATSAPP_TO` (comma-separated E.164),
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM` (e.g. `whatsapp:+14155238886`),
`META_WA_TOKEN`, `META_WA_PHONE_ID`, `META_WA_TEMPLATE` (optional approved template name),
`CALLMEBOT_APIKEY` (single recipient),
`HEATGUARD_PUBLIC_URL` (dashboard link to include, optional).

Location: the StickS3 has no GPS. Location = site coordinates (`site.lat/lon`) + worker zone/crew as the label + a Google Maps link `https://maps.google.com/?q=<lat>,<lon>`. For Meta, also send a native `location` message after the text.

## 6. Additions to the dashboard API (docs/api.md)

`GET /api/v1/state` also returns:
```json
"voice": [VoiceEntry, ...last 50],
"notifications": [Notification, ...last 50],
"config": {..., "assistant": {"enabled": false, "model": "gpt-realtime", "reason": "OPENAI_API_KEY missing"},
                "whatsapp": {"provider": "dryrun", "enabled": false, "dry_run": true, "recipients": []}}
```
SSE events: `voice` (VoiceEntry, upsert by id), `notify` (Notification, upsert by id).
`POST /api/v1/alerts/{id}/notify` → `{"ok": true, "notifications": [...]}` sends a WhatsApp escalation now.
Alert objects gain `"notified": [notification ids]`.
