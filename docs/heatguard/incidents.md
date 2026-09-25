# Incidents API — `heatguard.incident.v1`

Anything that happened to a person — a fall, suspected heat stroke, a manual SOS — is an
**incident**. Wearables report them; the backend also raises them itself (e.g. heat
stroke from wrist temperature). Every incident is stored in Postgres, shown on the dashboard
and, when critical, escalated (GPT-Live phone call + WhatsApp).

## `POST /v1/incidents`

Body: one JSON object. `Content-Type: application/json`.

```json
{
  "schema": "heatguard.incident.v1",
  "incident_id": "sticks3-7ce8b1-9f3a01c2-17",
  "device_id": "sticks3-7ce8b1",
  "person": {"name": "Ravi Kumar", "worker_id": "W-0042", "trade": "Steel fixer", "crew": "B-2"},
  "type": "fall",
  "status": "no_response",
  "severity": "critical",
  "occurred_at": "2026-09-25T12:41:07Z",
  "boot_id": "9f3a01c2",
  "read_time_us": 10534921,
  "location": {"lat": 24.8974, "lon": 55.1610, "label": "Tower B · Level 4", "source": "zone"},
  "details": {"impact_g": 7.5, "freefall_ms": 361, "still": 1, "message": "Free-fall then impact, no movement"},
  "source": "device"
}
```

| field | required | notes |
|---|---|---|
| `schema` | yes | literal `heatguard.incident.v1` |
| `device_id` | yes | same id as the telemetry frames |
| `type` | yes | `fall`, `heat_stroke`, `manual_sos`, `tremor`, `inactivity`, `unwell`, `impact`, `other` |
| `incident_id` | no | client id for idempotency; the firmware uses `<device_id>-<boot_id>-<seq>`. Server generates one when missing. |
| `status` | no | `suspected` (default), `no_response`, `worker_ok`, `cancelled`, `acknowledged`, `resolved`. Re-POSTing the same `incident_id` with a new status **updates** it. |
| `severity` | no | `critical` / `warning` / `info`; default by type: `heat_stroke`, `manual_sos` and a `fall` with `no_response` are critical, other falls critical, `tremor`/`inactivity`/`unwell` warning, `impact` info. |
| `person` | no | name etc. If missing, the backend fills it from the worker assigned to that device. |
| `occurred_at` | no | ISO 8601. If missing, derived from `boot_id` + `read_time_us` (same clock mapping as the telemetry frames) or else the receive time. |
| `location` | no | if missing, the backend uses the worker's zone centre on the site. |
| `details` | no | free-form object (impact g, free-fall ms, wrist °C, die °C, what the worker said, ...). |
| `source` | no | `device` (default), `voice`, `server`, `dashboard`, `api`. |

Response `201` (new) or `200` (update/duplicate):

```json
{"incident_id": "sticks3-7ce8b1-9f3a01c2-17", "created": true, "status": "no_response",
 "severity": "critical", "alert_id": "A-000031", "escalated": ["call", "whatsapp"]}
```

`422` on validation errors (unknown `type`, missing `device_id`, extra top-level keys).

**Escalation rule:** a new or updated incident that is `critical` and not `worker_ok`/`cancelled`
triggers the HeatGuard escalation once per incident: GPT-Live phone call to `CALL_TO`, WhatsApp
to `WHATSAPP_TO` (dry-run on a Twilio trial), and Devin analysis when a trace exists.
`worker_ok` / `cancelled` / `resolved` close the linked alert.

## `GET /v1/incidents`

Query: `device_id`, `type`, `status`, `since` (ISO time), `limit` (default 50, max 500).
Newest first. Each item is the stored incident plus `received_at`, `updated_at`, `alert_id`,
`escalated`.

## `GET /v1/incidents/{incident_id}`

One incident, 404 if unknown.

## Backend-raised incidents

HeatGuard's own detections are recorded through the same path with `source: "server"`:
wrist-temperature heat emergency (`heat_stroke`), voice "help" requests (`manual_sos`,
`source: "voice"`), and device events that arrive over the WebSocket (`fall`, `manual_sos`
from the SOS button, `tremor`, ...). So `GET /v1/incidents` is the single history of what
happened to whom.

## Storage

Postgres table `incidents` (created by the backend's schema migration):
`incident_id text primary key, device_id text, person jsonb, type text, status text,
severity text, occurred_at timestamptz, received_at timestamptz, updated_at timestamptz,
location jsonb, details jsonb, source text, alert_id text, escalated jsonb`.
