# StickS3 ↔ backend: device WebSocket

One backend (Cloud Run) serves everything. The wearable holds **one** WebSocket to it and
uses it for telemetry, events, commands and push-to-talk voice. Cloud Run only speaks
HTTP(S)/WebSocket on one port, so there is no UDP, no LAN discovery and no raw TCP.

```
wss://telemetry-backend-501582454609.asia-northeast1.run.app/v1/device/ws?device_id=<id>&token=<t>
```

- `device_id` — same id as in the telemetry frames, e.g. `sticks3-7ce8b1`.
- `token` — shared device token. Required when the backend has `HEATGUARD_DEVICE_TOKEN`
  set; if that env var is empty the socket is open (demo). Also accepted as
  `Authorization: Bearer <t>`. Wrong token → close code 4401 before accept.
- Local development uses plain `ws://<host>:<port>/v1/device/ws?...`.
- Device firmware reads the URL from NVS `uiflow/hg_url` and the token from `uiflow/hg_token`,
  falling back to the Cloud Run URL above and no token.

## Framing rules

- Text messages are UTF-8 JSON. Binary messages carry voice.
- The device masks its frames as RFC 6455 requires (a zero masking key is acceptable to
  the server; it keeps the ESP32 from XOR-ing every audio byte).
- The server (uvicorn) sends a WebSocket **ping every 20 s**; the device must answer with a
  pong carrying the same payload or the server closes the socket after 20 s.
- Cloud Run ends any request at its timeout (deploy with 3600 s). The device reconnects with
  backoff 1, 2, 4, … 30 s and sends `hello` first after every connect.
- The server never assumes one message per event: the device may batch.

## Device → server, text

### 1. Telemetry: a JSON **array** of `sticks3.telemetry.v1` frames

Exactly the contract of `POST /v1/ingest/frames` (`backend/telemetry/models.py`,
`TelemetryFrame`, `extra="forbid"`). 1–50 frames per message; the firmware sends 10 frames
(200 ms at 50 Hz). Example element:

```json
{"schema":"sticks3.telemetry.v1","device_id":"sticks3-7ce8b1","boot_id":"9f3a01c2","sequence":1842,
 "read_time_us":10534921,"frame":"display_portrait_usb_down_rh",
 "fresh":{"accelerometer":true,"gyroscope":true,"temperature":false},
 "imu":{"acceleration_g":{"x":0.012,"y":-0.031,"z":0.998},"angular_velocity_dps":{"x":0.18,"y":-0.06,"z":12.45}}}
```

- `boot_id`: 8 hex chars, random per boot. `sequence`: 0,1,2,… per boot, one per IMU sample.
- `read_time_us`: **monotonic, never wraps** within a boot (the firmware accumulates
  `ticks_diff`; MicroPython's raw `ticks_us` wraps every ~17.9 min).
- `fresh.temperature` is true on one frame per second, with `imu.die_temperature_c` = ESP32
  die temperature (`esp32.mcu_temperature()`, whole degrees, as a float). When false, the key
  is omitted.
- `configuration` is present on the first frame after boot and then every 60 s:
  `{"accelerometer_range_g":8,"accelerometer_odr_hz":50,"gyroscope_range_dps":2000,
  "gyroscope_odr_hz":50,"library":"M5Unified (UiFlow2 MicroPython)","library_version":"<fw>"}`.
- Server: validates every frame with `TelemetryFrame`, hands the batch to the same
  `telemetry.service` path as `POST /v1/ingest/frames` (Postgres + ring buffer + analytics),
  then feeds HeatGuard's real-time pipeline (live chart, fall trace windows, wrist-temperature
  tiers). A batch that fails validation is dropped and answered with
  `{"cmd":"error","what":"frames","detail":"<first pydantic error>"}`. Backpressure (queue full)
  drops the batch and answers `{"cmd":"slow","retry_ms":1000}`.

### 2. HeatGuard messages: a JSON **object** with `"k"`

Same shapes as the old UDP protocol (`docs/heatguard/services.md` §2), minus raw IMU:

- `{"k":"hello","id":"sticks3-7ce8b1","fw":"heatguard-0.5","ip":"172.20.10.3","params":{...detector tunables...}}`
- `{"k":"st","id":...,"bat":80,"chg":1,"act":0.02,"wl":"light","am":1.0,"gm":0.4,"trem_hz":0.0,"trem_amp":0.0,"still_s":3,"phase":"work","ui":"normal","free":7000,"rssi":-60}` — 1 Hz.
  (Temperature now arrives in the frames; a `"temp"` here is still accepted.)
- `{"k":"ev","id":...,"seq":100017,"type":"fall","t":<ms>,"detail":{...}}` — `t` is in the
  same clock as the frames: `read_time_us // 1000` at the event. Resent every 1 s until
  `{"cmd":"evack","seq":n}` arrives (the socket may drop between send and ack).

## Server → device, text

JSON objects with `"cmd"` — unchanged from `docs/heatguard/api.md` §2 plus services.md §2:
`plan`, `rest`, `resume`, `buzz`, `msg`, `ack`, `cfg`, `hello`, `evack`, `clear`, `say`,
and the two new ones above: `error`, `slow`.

## Voice (push-to-talk), binary, both directions

Each binary WebSocket message is **one voice frame**: first byte = type (ASCII), rest = payload.
No length prefix (the WebSocket frame delimits it). Same frame types as the old TCP protocol:

| dir | type | payload |
|---|---|---|
| device → server | `H` | JSON `{"rate":24000,"fmt":"pcm16"}` — starts a question; cancels any reply in progress |
| device → server | `A` | PCM16 LE mono 24 kHz, ≤ 4800 bytes |
| device → server | `E` | empty — button released |
| server → device | `T` | UTF-8 text to show (≤ 120 bytes) |
| server → device | `A` | PCM16 LE mono 24 kHz reply audio, ≤ 4800 bytes |
| server → device | `E` | empty — reply finished |
| server → device | `X` | UTF-8 error text — session over |

One question at a time per device. The server runs the same GPT-Live assistant as before
(`backend/heatguard/services/assistant.py`), fed from this socket instead of TCP :47802.

## HTTP stays available

`POST /v1/ingest/frames` keeps working for any device or the team simulator
(`backend/scripts/simulate_device.py`). Frames that arrive over HTTP also reach HeatGuard's
real-time pipeline; those devices just cannot receive commands or use voice.
