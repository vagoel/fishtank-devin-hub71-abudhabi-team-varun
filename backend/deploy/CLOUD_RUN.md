# Deploying the backend (telemetry + HeatGuard) to Cloud Run

One service, `telemetry-backend` (project `fishtank-hackathon`, region `asia-northeast1`),
serves the telemetry API, the HeatGuard dashboard (`/`), its API and SSE (`/api/v1/*`), the
device WebSocket (`/v1/device/ws`), incidents (`/v1/incidents`) and the Twilio phone-call
webhooks (`/twilio/*`). All HeatGuard state is in memory, so run exactly one instance.

## Deploy (source build)

```bash
gcloud run deploy telemetry-backend --source backend \
  --region asia-northeast1 --project fishtank-hackathon \
  --build-service-account=projects/fishtank-hackathon/serviceAccounts/backend-builder@fishtank-hackathon.iam.gserviceaccount.com
```

`backend/.gcloudignore` keeps `.venv/` and any `.env` out of the upload. The image listens on
`$PORT` (Cloud Run sets it; container port 8000).

## Service settings (already set on the service)

| setting | value | why |
|---|---|---|
| min / max instances | 1 / 1 | alerts, live workers, sockets live in memory |
| CPU | always allocated (`--no-cpu-throttling`) | ticker, weather, Devin polling, voice relay run between requests |
| request timeout | 3600 s | device WebSocket, dashboard SSE, Twilio media stream |
| startup CPU boost | on | |

Equivalent flags if the service is ever recreated:
`--min-instances 1 --max-instances 1 --no-cpu-throttling --timeout 3600 --cpu-boost --port 8000`.

## Environment

Already set on the service: `DATABASE_URL` (secret), `OPENAI_API_KEY`, `DEVIN_API_KEY`,
`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `WHATSAPP_TO`, `CALL_TO`, `TWILIO_FROM_NUMBER`,
`HEATGUARD_PUBLIC_URL` (= the service URL; Twilio's webhooks and media stream need it, without
it calls fall back to Twilio `<Say>`).

Optional:

| variable | default | effect |
|---|---|---|
| `HEATGUARD_DEVICE_TOKEN` | unset | when set, `/v1/device/ws` needs `?token=` or `Authorization: Bearer`; unset = open |
| `HEATGUARD_AUTO_WHATSAPP` | `1` | `0` = no automatic WhatsApp/phone escalation |
| `HEATGUARD_CALLS` | `1` | `0` = never place a phone call (dry run) |
| `HEATGUARD_AUTO_DEVIN` | `1` | `0` = no automatic Devin sessions |
| `WHATSAPP_PROVIDER` | auto | `dryrun` forces a dry run; a Twilio trial account is a dry run anyway |
| `LIVE_CALL_API_TOKEN` | unset | enables `POST /v1/calls` / `GET /v1/calls/{id}` (X-Api-Key) |
| `HEATGUARD_LAN` | `0` | LAN-only UDP gateway, beacon, USB serial, TCP voice: leave off on Cloud Run |

## Check after a deploy

```bash
URL=https://telemetry-backend-501582454609.asia-northeast1.run.app
curl -s $URL/health
curl -s $URL/api/v1/state | head -c 300
curl -s "$URL/v1/incidents?limit=3"
```
Open `$URL/` for the dashboard and `$URL/admin` for the telemetry admin page.

## After every deploy: move the wearables to the new revision

A wearable keeps its WebSocket (or kept-alive HTTPS connection) on the revision it
connected to, and Cloud Run keeps that old instance running while the connection is
open, up to the 3600 s request timeout. Until it reconnects, its data still reaches
Postgres but the new revision's live dashboard, alerts and voice don't see it.

1. Delete the superseded revisions (images stay in Artifact Registry):
   `gcloud run revisions list --service telemetry-backend --region asia-northeast1`
   then `gcloud run revisions delete <old-revision> --region asia-northeast1 --quiet`.
2. Power-cycle the band (side button) so it opens a fresh connection. It reconnects on
   its own within a few seconds and shows up with `transport: ws` in `/api/v1/state`.
