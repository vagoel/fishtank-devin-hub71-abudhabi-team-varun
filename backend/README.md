# Telemetry API

High-throughput time-series backend for devices streaming **accelerometer**,
**gyroscope** and **temperature** data. Built with FastAPI + asyncpg on plain Postgres
(Supabase free tier compatible), it accepts the `sticks3.telemetry.v1` device frames (or
large columnar sample arrays), persists them with bulk `COPY`, and serves analytics over
the last *N* points from an in-memory ring buffer.

```
 device ──POST /v1/ingest/frames──▶ FastAPI ──▶ per-device ring buffers (numpy) ──▶ GET /v1/analytics/…
                                       │
                                       ├─▶ async batch writer ──COPY──▶ Postgres / Supabase
                                       └─▶ device registry (boot/sequence/config) ──▶ devices table
```

## Design

| Concern | Approach |
| --- | --- |
| Volume | Ingest handlers validate, sort, enqueue and return `202` in a few ms. A background task flushes the queue every `FLUSH_INTERVAL_MS` (or when `FLUSH_MAX_ROWS` accumulate) using Postgres `COPY`, ~10x faster than `INSERT`. Locally this sustains >250k rows/s. |
| Backpressure | If the queue exceeds `QUEUE_MAX_ROWS` (DB slow/down) the API returns `503` + `Retry-After` instead of growing unbounded; an envelope is accepted atomically or not at all. Failed flushes retry with exponential backoff and never drop rows. |
| Analytics speed | The newest `RING_BUFFER_SIZE` samples per device are kept in fixed numpy circular buffers; stats over 1 000 points take ~2 ms and never touch the DB. Requests for more points than are buffered (or after a restart) fall back to an index-backed SQL query transparently (`source` in the response tells you which). |
| Storage | Three narrow append-only tables (`temperature_readings`, `gyro_readings`, `accel_readings`) with `(device_id, ts desc)` indexes; `real` columns keep rows small. A `devices` table holds the latest boot/sequence/configuration per device. `purge_readings(interval)` + pg_cron handle retention. No extensions required. |
| Device clock | Frames carry `read_time_us` (microseconds since boot). Each `boot_id` is anchored to server time once, so samples keep the device's exact spacing across requests; `sequence` gaps, reboots and clock resyncs are counted per device. |
| Payload size | Batch frames into arrays (`POST /v1/ingest/frames` with `[frame, frame, ...]`). Columnar batches (`values: [...]`, `samples: [[x,y,z],...]`) with a `start_ts` + `interval_ms` grid are also available for gateways. |

## Run locally

```bash
docker compose up -d db                 # Postgres on :5432
pip install -e ".[dev]"
uvicorn telemetry.main:app --reload     # http://localhost:8000/docs, /admin dashboard
pytest                                  # needs the DB
python scripts/simulate_device.py --devices 3 --hz 100   # fake sticks3 devices for /admin
python scripts/loadtest.py --devices 20 --seconds 10 --batch 500 --hz 200
```

Or everything in containers: `docker compose up --build`.

## Admin dashboard

`GET /admin` serves a single-page dashboard (vanilla JS + Chart.js, no build step) that
polls the API every second:

- KPIs: rows/s, active devices, rows written, queue depth vs. ceiling, COPY flush latency,
  frames received / dropped / out-of-order / reboots.
- **Rows ingested per second, stacked by device** over the last 2 minutes.
- Device table (live + idle from the DB): rows/s, per-sensor totals, last ingest, boot id,
  sequence, dropped/out-of-order/reboot counters, frame. Click a row to select it.
- Time series for the selected device: die temperature, gyro x/y/z, accel x/y/z with the
  analytics summary under each chart; choose 200–5000 points and memory/database source.

It is backed by `GET /v1/admin/overview`, a memory-only snapshot (writer stats, per-device
ingest totals and per-second history, frame state) that is cheap to poll.

## Use Supabase

1. Create a free project, open **Connect** (top bar) → **Transaction pooler**.
2. Copy that URI (port 6543; the direct `db.<ref>.supabase.co` host is IPv6-only and
   unreachable from most IPv4 networks) into `.env`, URL-encoding special characters in
   the password (`,` → `%2C`, `@` → `%40`, ...):
   `DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres`
3. Start the API. Tables and indexes are created on boot (`DB_AUTO_MIGRATE=true`), or run
   `telemetry/schema.sql` in the SQL editor yourself.

Prepared statements are disabled by default (`DB_STATEMENT_CACHE_SIZE=0`) so the pooler
works; keep `DB_POOL_MAX` small (free tier allows ~15 pooled connections).

Verified against a free-tier project (Postgres 17, `aws-0-ap-northeast-1` pooler): the
full test suite passes and 15k rows from 5 000 frames land in ~1.5 s (3 `COPY` flushes of
~500 ms each from a remote box).

## API

### Device frames (`sticks3.telemetry.v1`)

`POST /v1/ingest/frames` accepts one frame or an array of frames exactly as the device
produces them and returns `202 {"accepted": rows, "queued_rows": m, "frames": n}`:

```json
{
  "schema": "sticks3.telemetry.v1",
  "device_id": "sticks3-a1b2c3",
  "boot_id": "74f2c291",
  "sequence": 1842,
  "read_time_us": 10534921,
  "frame": "display_landscape_usb_right_rh",
  "fresh": {"accelerometer": true, "gyroscope": true, "temperature": true},
  "imu": {
    "acceleration_g": {"x": 0.012, "y": -0.031, "z": 0.998},
    "angular_velocity_dps": {"x": 0.18, "y": -0.06, "z": 12.45},
    "die_temperature_c": 31.74
  },
  "configuration": {
    "accelerometer_range_g": 8, "accelerometer_odr_hz": 100,
    "gyroscope_range_dps": 2000, "gyroscope_odr_hz": 200,
    "library": "M5Unified", "library_version": "0.2.22"
  }
}
```

- `acceleration_g` → `accel_readings`, `angular_velocity_dps` → `gyro_readings`,
  `die_temperature_c` → `temperature_readings`. A sensor whose `fresh` flag is `false` is
  skipped (its value may then be omitted); a fresh sensor without a value is a `422`.
- `schema` must be `sticks3.telemetry.v1`; unknown top-level/IMU keys are rejected, while
  `configuration` accepts extra firmware fields.
- `read_time_us` is mapped to wall-clock time per `boot_id` (see *Device clock* above);
  `sequence` is used to count dropped and out-of-order frames.
- `frame`, `configuration`, `boot_id` and the last `sequence` are kept per device and
  exposed by `GET /v1/devices/{device_id}` (from memory, or the `devices` table after a
  restart).

### Columnar ingest (all return `202 {"accepted": n, "queued_rows": m}`)

```http
POST /v1/ingest/temperature
{"device_id": "dev-1", "start_ts": "2026-01-01T00:00:00Z", "interval_ms": 100,
 "values": [21.5, 21.6, 21.7]}

POST /v1/ingest/gyro
{"device_id": "dev-1", "timestamps": [1767225600000, 1767225600020],
 "samples": [[0.01, -0.02, 0.98], [0.02, -0.01, 0.97]]}

POST /v1/ingest/accel                # same shape as gyro, values in g

POST /v1/ingest                      # all sensors, several batches, one request
{"temperature": [...], "gyro": [...], "accel": [...]}
```

Timestamps accept ISO-8601 or epoch seconds/milliseconds. If omitted entirely, samples
are stamped with the server time. Out-of-order samples are sorted per batch.

### Analytics over the last N points

```http
GET /v1/analytics/temperature/{device_id}?last=500
→ count, window{from,to,duration_s,sample_rate_hz}, latest,
  stats{mean,std,min,max,median,p05,p95,rms,ewma},
  trend{slope_per_min,delta}, anomalies{count,timestamps,values}, source

GET /v1/analytics/gyro/{device_id}?last=500
→ axes{x,y,z: stats}, magnitude stats, peak, rotation{x,y,z} (integrated angle),
  motion{active_fraction,active_seconds,is_moving}, source

GET /v1/analytics/accel/{device_id}?last=500
→ axes{x,y,z: stats}, magnitude stats,
  gravity{x,y,z,magnitude,pitch_deg,roll_deg,dominant_axis},
  vibration{rms_g,peak_g,peak_ts}, source
```

`source=memory|database|auto` forces where the window is read from.

### Other

```http
GET /v1/readings/temperature/{device_id}?last=100   # raw points
GET /v1/readings/gyro/{device_id}?last=100
GET /v1/readings/accel/{device_id}?last=100
GET /v1/devices                                      # device list + last seen + frame state
GET /v1/devices/{device_id}                          # boot_id, sequence, gaps, configuration
GET /health                                          # DB connectivity + writer stats
GET /v1/stats                                        # writer/buffer/config snapshot
GET /v1/admin/overview                               # live per-device ingest snapshot (dashboard)
GET /admin                                           # admin dashboard (HTML)
```

## Configuration

See `.env.example`. Defaults are tuned for a single free-tier Postgres: 200 ms flush
interval, 10k-row flushes, 500k-row queue ceiling, 20k buffered points per device.

## HeatGuard (same app)

`heatguard/` is mounted into this app (`telemetry/main.py`): the HeatGuard dashboard at `/`, its
API and SSE under `/api/v1/*`, the wearable WebSocket `/v1/device/ws`
([contract](../docs/heatguard/device-ws.md)), incidents under `/v1/incidents`
([contract](../docs/heatguard/incidents.md)) and Twilio call webhooks under `/twilio/*`.
Frames posted to `/v1/ingest/frames` also feed HeatGuard's live view. HeatGuard settings come
from env vars or `backend/.env` (see `heatguard/.env.example`); deploy notes in
[`deploy/CLOUD_RUN.md`](deploy/CLOUD_RUN.md). Tests keep HeatGuard offline (`tests/conftest.py`).
