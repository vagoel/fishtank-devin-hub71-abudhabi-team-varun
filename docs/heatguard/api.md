# HeatGuard API

Base URL: `http://localhost:8000/api/v1`

All timestamps are epoch seconds (float) unless noted. All temperatures are °C.

---

## 1. Device ingest (the one endpoint devices push to)

`POST /api/v1/ingest` — body is one message or a list of messages.

```json
{
  "device_id": "stick-7ce8b1e3",
  "type": "imu | temperature | status | event",
  "ts": 1790000000.12,
  "data": { }
}
```

| type          | data                                                                                         |
|---------------|----------------------------------------------------------------------------------------------|
| `imu`         | `{"t0": <device ms>, "dt": 20, "d": [[ax,ay,az,gx,gy,gz], ...]}` accel in g, gyro in deg/s   |
| `temperature` | `{"temp_c": 44.1, "source": "device"}` or `{"temp_c": 39.5, "rh": 48, "source": "ambient"}`  |
| `status`      | `{"bat": 80, "chg": 1, "act": 0.12, "wl": "light", "trem_hz": 0, "trem_amp": 0, "still_s": 3, "ui": "ok"}` |
| `event`       | `{"type": "fall|fall_ok|fall_noresp|tremor|erratic|inactivity|inactivity_ok|sos|unwell|impact", "detail": {...}}` |

Response: `{"ok": true, "accepted": <n>, "commands": [ ...pending downlink commands for this device... ]}`

Devices on WiFi poll their downlink through this response. Devices on USB get commands over serial.

## 2. Device serial protocol (USB CDC, 115200, JSON lines prefixed with `@`)

Device → host: `@{"k":"hello"|"s"|"st"|"ev", ...}`. `k:"s"` = IMU batch (same shape as `imu` data above), `k:"st"` = status (1 Hz, includes `temp`), `k:"ev"` = event.

Host → device (one JSON object per line):

```json
{"cmd":"plan","wbgt":31.2,"work":30,"rest":30,"elapsed":12.5,"phase":"work","level":2,"label":"High"}
{"cmd":"rest","mins":30,"reason":"Heat limit reached"}
{"cmd":"resume"}
{"cmd":"buzz"}
{"cmd":"msg","text":"Supervisor on the way"}
{"cmd":"ack","type":"fall"}
{"cmd":"cfg","inact_s":240}
```

---

## 3. Dashboard API

### `GET /api/v1/state` → initial snapshot

```json
{
  "site": Site,
  "live": [Worker],
  "fleet": Fleet,
  "alerts": [Alert],
  "config": {"devin_configured": false, "devin_mode": "local", "serial_port": "/dev/cu.usbmodem2101", "serial_connected": true, "fleet_size": 1200, "timewarp": 1, "sim_events": true}
}
```

### `GET /api/v1/stream` → Server-Sent Events

| event    | data                                                                                   | rate          |
|----------|----------------------------------------------------------------------------------------|---------------|
| `imu`    | `{"id": "<worker id>", "samples": [[t_ms, ax, ay, az, gx, gy, gz], ...]}`             | ~10/s         |
| `worker` | `Worker` (live workers only)                                                           | 1/s + changes |
| `fleet`  | `Fleet`                                                                                 | every 2 s     |
| `alert`  | `Alert` (new or updated — upsert by `id`)                                              | on change     |
| `site`   | `Site`                                                                                  | on change     |
| `config` | same shape as `state.config`                                                            | on change     |

### Objects

**Worker**
```json
{
  "id": "LIVE-7ce8b1e3", "name": "Ramesh Kumar", "trade": "Steel fixer", "zone": "Block C", "crew": "C-3",
  "live": true, "acclimatized": true,
  "status": "ok | caution | rest | alert | offline",
  "risk": 0,
  "workload": "rest | light | moderate | heavy",
  "phase": "work | rest | stop",
  "phase_reason": "Heat limit reached",
  "work_elapsed_min": 12.5, "work_budget_min": 30,
  "rest_remaining_min": 0, "rest_required_min": 30,
  "shift_exposure_min": 184.0,
  "device_temp_c": 44.0, "battery": 80,
  "last_event": "fall_ok", "open_alerts": 0,
  "updated": 1790000000.0,
  "motion": {"am": 1.01, "gm": 3.2, "act": 0.04, "trem_hz": 0, "trem_amp": 0, "still_s": 12}
}
```
`motion` is present only for live devices.

**Fleet**
```json
{
  "counts": {"ok": 1100, "caution": 50, "rest": 40, "alert": 3, "offline": 7, "total": 1200},
  "tiles": "oooocoorrao...",
  "ids_prefix": "W-", "id_width": 4
}
```
`tiles` has one character per simulated worker, in order `W-0001`, `W-0002`, ...:
`o`=ok, `c`=caution, `r`=rest, `a`=alert, `x`=offline.

**Site**
```json
{
  "name": "Dubai — Site 7 (demo)", "lat": 25.2, "lon": 55.27,
  "temp_c": 38.4, "rh": 52, "wind_ms": 3.1, "solar_wm2": 640, "apparent_c": 44.0,
  "source": "open-meteo | override | fallback",
  "wbgt_c": 32.1,
  "category": {"level": 3, "label": "Very high", "color": "#f97316"},
  "policy": {
    "acclimatized":   {"light": {"work": 30, "rest": 30}, "moderate": {"work": 15, "rest": 45}, "heavy": {"work": 10, "rest": 50}},
    "unacclimatized": {"light": {"work": 15, "rest": 45}, "moderate": {"work": 0,  "rest": 60}, "heavy": {"work": 0,  "rest": 60}}
  },
  "midday_break": {"active": false, "in_season": false, "window": "12:30–15:00", "season": "15 Jun – 15 Sep"},
  "local_time": "14:05", "updated": 1790000000.0
}
```
`work: 0` means stop work.

**Alert**
```json
{
  "id": "A-000123", "worker_id": "LIVE-7ce8b1e3", "worker_name": "Ramesh Kumar",
  "live": true, "simulated": false,
  "type": "fall | tremor | erratic | inactivity | sos | unwell | heat_limit | heat_critical | rest_violation | offline",
  "severity": "critical | warning | info",
  "title": "Fall detected", "message": "Free-fall 180 ms, impact 3.4 g, no movement after.",
  "ts": 1790000000.0,
  "state": "open | acknowledged | resolved | false_alarm",
  "source": "device | server",
  "has_window": true,
  "devin": null
}
```

`devin` once analysis is requested:
```json
{
  "status": "queued | running | done | error",
  "engine": "devin | local",
  "session_id": "devin-abc", "url": "https://app.devin.ai/sessions/abc",
  "verdict": "true_fall | false_alarm | tremor_or_seizure_like | erratic_movement | heat_illness_risk | needs_human_review",
  "confidence": 0.82,
  "summary": "…", "evidence": ["…"], "recommended_actions": ["…"],
  "suggested_threshold_changes": [{"parameter": "FALL_IMPACT_G", "current": 1.8, "proposed": 2.2, "rationale": "…"}],
  "error": null
}
```

### Actions

| method & path                                  | body                                                        |
|------------------------------------------------|-------------------------------------------------------------|
| `GET  /api/v1/workers/{id}`                    | → `Worker`                                                  |
| `POST /api/v1/workers/{id}/command`            | `{"cmd":"rest","mins":15}` / `{"cmd":"resume"}` / `{"cmd":"buzz"}` / `{"cmd":"msg","text":"…"}` |
| `GET  /api/v1/alerts?limit=100`                | → `[Alert]`                                                 |
| `POST /api/v1/alerts/{id}/ack`                 | —                                                           |
| `POST /api/v1/alerts/{id}/resolve`             | —                                                           |
| `POST /api/v1/alerts/{id}/false_alarm`         | —                                                           |
| `POST /api/v1/alerts/{id}/devin`               | — starts Devin (or local) analysis                          |
| `GET  /api/v1/alerts/{id}/window`              | → `{"samples": [[t_ms, ax, ay, az, gx, gy, gz], ...], "event_t_ms": 12345}` |
| `POST /api/v1/site/override`                   | `{"temp_c": 46, "rh": 45, "sun": true}`                     |
| `DELETE /api/v1/site/override`                 | back to live weather                                        |
| `POST /api/v1/demo`                            | `{"timewarp": 1 | 30 | 60, "sim_events": true}`             |
