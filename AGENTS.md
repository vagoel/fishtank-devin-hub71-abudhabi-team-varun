# Project workflow

## Scope and architecture

- React 19 / Vite 8 JavaScript frontend is an npm workspace. Install dependencies from the repository root with `--workspace frontend` for frontend packages; the root package-lock.json is authoritative. The older frontend/package-lock.json belongs to the original scaffold and is not used for workspace installs.
- React, React DOM, and Vite are pinned in the root tooling dependencies/overrides and frontend manifest to keep npm's auto-installed peer dependencies aligned. Multiple React copies cause invalid-hook-call failures in component tests.
- If npm 10 fails with the workspace `edgesOut` resolution error, use `npx --yes npm@11.19.1 install`. Do not bypass peer dependency validation or alter security settings.
- Python API, hardware firmware, and Devin analysis logic belong to the other team members. The dashboard consumes their results; it does not implement those systems.
- Dashboard mode defaults to explicitly labeled fictional demo data. `VITE_DATA_MODE=api` selects the HTTP adapter. API failures must never silently load demo records.
- Incident severity and review status are separate. Acknowledgement and simulated dispatch do not resolve an incident. Dispatch requires an explicit operator confirmation and is disabled in API mode until a real authorized integration is separately agreed.
- Demo temperature readings are fictional band surface readings, not core body temperature. The live incident contract supplies no current vitals or device connectivity. Live coordinates are incident locations, not continuous worker tracking. Do not invent medical measurements, live heartbeat status, or agent analysis.

## Commands

Run from the repository root:

- `npm install` — install the workspace dependencies.
- `npm run dev:frontend` — run the dashboard in the mode selected by the frontend environment; demo mode does not require Python.
- `npm run dev:demo --workspace frontend` — force standalone demo even when VITE_DATA_MODE selects api or INCIDENT_API_TARGET is invalid.
- `npm run dev:api --workspace frontend` — force the live incident adapter.
- `npm run build:demo --workspace frontend` — produce a guaranteed-demo static build, regardless of API-mode environment settings.
- `npm run dev --workspace frontend -- --host 127.0.0.1 --port 5175 --strictPort` — use the explicit local preview/test port.
- `npm run lint --workspace frontend`
- `npm run test --workspace frontend -- --run`
- `npm run test:e2e --workspace frontend` — Playwright; uses installed Google Chrome and a local server on port 5175. Browser screenshots/traces are written to frontend/browser-artifacts.
- `npm run build --workspace frontend`
- `npm run preview --workspace frontend -- --host 127.0.0.1 --port 5176 --strictPort` — serve the production build.
- `npm run dev` — original combined frontend/Python development command.

Vitest tests live in src/**/*.test.js(x) and dev/**/*.test.js. Browser tests live in e2e and are not collected by Vitest. Tests mock OpenAI and must not start paid voice sessions.

## GPT-Live-1

- The selected voice model is exactly `gpt-live-1`, using the Live API, not the Realtime API. No automatic fallback to another model.
- For local setup, create an ignored frontend/.env using frontend/.env.example as a template and set unprefixed `OPENAI_API_KEY`. Restart Vite after editing environment variables. Never read, log, commit, or expose that value in browser code or a VITE_* variable.
- The Vite development-only handler at `POST /__dev/voice/session` accepts `{sdp}`, creates a server-authenticated `/v1/live/sessions` WebRTC session, and returns `{session: {id}, transport: {type: 'webrtc', sdp}}`.
- Production needs an authenticated server-side equivalent. Set nonsecret `VITE_VOICE_SESSION_URL` to that endpoint (default in production: `/api/voice/session`). Vite preview/static hosting does not provide the local session handler.
- Wait for `session.started` before appending context. Use Live thinking/commentary/instructions append events, independent input/output transcripts, and graceful `session.close` / `session.closed`. Do not use Realtime voice-turn triggers.
- Client delegation supplies read-only current incident context. Future backend reasoning integration remains owned by the API teammate. Voice never authorizes dispatch or incident mutations.
- Enable voice is an explicit operator action. Session duration is billable even when the microphone is muted. Local browser speech is only an explicitly labeled optional fallback, not GPT-Live.

## Dashboard API adapter contract

The live adapter in src/services/dashboardApi.js consumes only `GET /v1/incidents`; it does not call the old proposed dashboard/events/review routes or the telemetry endpoints.

- Set `VITE_DATA_MODE=api` in the local frontend environment and restart Vite for live data. Set `VITE_DATA_MODE=demo` for the independent fictional demonstration. The explicit `dev:demo` / `build:demo` commands override API-mode settings, while `dev:api` forces live mode. Vite embeds these settings in static builds, so deployment mode changes require rebuilding.
- Default browser base: `VITE_API_BASE_URL=/api`. Vite proxies GET `/api/v1/incidents` to `INCIDENT_API_TARGET`, defaulting to `https://telemetry-backend-501582454609.asia-northeast1.run.app`. Local proxy writes are rejected. The public/_redirects rule provides the equivalent incident proxy on the next Netlify deployment; another static host needs a same-origin proxy or backend CORS support.
- The feed accepts arrays, `{incidents: [...]}`, or a single `heatguard.incident.v1` object. Unrecognized or paginated envelopes fail explicitly instead of silently dropping reports.
- Preserve incident_id, device_id, person name/trade/crew, type, status, severity, occurred_at, location lat/lon/label, details, and source. The deployed service also uses source `api` and supplies received_at, updated_at, alert_id, escalated, boot_id, read_time_us, and optional worker/zone metadata.
- Poll every 2000ms by default, configurable with `VITE_POLL_INTERVAL_MS`. Polls never overlap and back off on failures. Reports are retained in browser memory and merged by ID; unchanged responses do not repeat alerts. Older server updated_at values do not overwrite newer records. A report disappearing from a response is not treated as a resolution.
- Preserve source outcomes exactly: suspected/no_response/acknowledged remain active; worker_ok/cancelled/resolved are terminal. Informational severity is supported. Local revisions and observation timestamps are UI bookkeeping, not backend versions or a durable audit log.
- Read names and locations from each incident. Missing identity or coordinates use clearly labeled demo assignments, as requested for the hackathon. Malformed coordinates fail validation rather than silently moving a reported incident. API-mode mapping accommodates coordinates outside the initial Abu Dhabi camera bounds.
- Do not derive current vitals, device online status, building footprints, or a Devin assessment from incident records. Backend escalation targets are displayed as reported metadata, not confirmed delivery or dashboard-initiated actions.
- Live review/dispatch controls remain read-only because only the GET contract is integrated. Demo actions remain fully interactive and simulated. No tests may ingest events, update real incidents, trigger calls, or start paid voice sessions.
- `E2E_MODE=api npm run test:e2e --workspace frontend` runs live-contract browser fixtures on port 5177. The default browser suite runs the standalone demo on port 5175. Set PLAYWRIGHT_BASE_URL to use a separately running server; never point mutating test actions at backend services.

## Mapping

- MapLibre uses OpenFreeMap tiles and required OpenFreeMap/OpenMapTiles/OpenStreetMap attribution. Public map data needs connectivity and has no uptime guarantee.
- Three.js shares MapLibre's WebGL canvas for the four illustrative landmarks. Do not add an independent camera/render loop or leak shared WebGL resources on remount.
- MapLibre 6 uses named module exports and a separate worker. Import the worker with Vite's `?worker&url` so its dependencies are bundled. A plain `?url` works in development but leaves its dependency imports unresolved in a production build.
- To test a running production preview, set `PLAYWRIGHT_BASE_URL=http://127.0.0.1:5176` when running the browser suite. The client-bundle check also verifies that a build made with the fake `AMAN_BUILD_SECRET_SENTINEL` server key does not expose it or the session handler to the browser.
- The style's `distance` filters exclude base-map building polygons near custom landmarks. `within` does not evaluate polygon features and is not suitable for this masking.
- Keep an accessible site/incident list usable when map tiles or WebGL fail. All construction records and dispatch routes in demo mode are fictional.

## Netlify frontend deployment

- The dashboard site is `aman-dashboard-varun` in team `vagoel`, project ID `ea41404a-bbe6-40f5-833a-5ae015515ef9`, at https://aman-dashboard-varun.netlify.app. Do not deploy this dashboard over an unrelated existing site.
- Deploy only the user-approved committed revision from an isolated worktree when the active checkout contains ongoing work. Upload only `frontend/dist`; do not upload backend code, environment files, or credentials.
- Production was deployed from frontend revision `fa92146` on 2026-09-25 as deploy `6ab67d697697225517f593c0`. It uses API mode, browser base `/api`, and a 2000ms polling delay. All seven variables from frontend/.env were imported into Netlify and compared against production build values without printing secrets. Vite embeds public settings during build; changing Netlify variables requires rebuilding and redeploying.
- Netlify CLI 23.0.0 env commands accept `NETLIFY_SITE_ID` in the process environment and `--filter frontend`. Native `env:import` prints values by default, including in JSON mode: capture its output in memory and print only key names, equality results, and known-public settings. Do not run it with raw terminal output or use `--replace-existing`. Import only when the user authorizes syncing the local file; matching remote variables are recreated while unrelated variables are retained.
- After building, a manual deployment can use `npx --yes netlify-cli@23.0.0 deploy --site ea41404a-bbe6-40f5-833a-5ae015515ef9 --filter frontend --dir /absolute/path/to/frontend/dist --prod --no-build`. This CLI version supports Node 22.12; do not combine `--context` with `--no-build`. CLI authorization is separate from MCP authorization.
- Verify hosted API-mode UI with `E2E_MODE=api PLAYWRIGHT_BASE_URL=https://aman-dashboard-varun.netlify.app npm run test:e2e --workspace frontend`. This suite intercepts incident responses and must not mutate the backend or start paid voice sessions. Also verify actual read-only GET polling separately; production returned HTTP 200 on three successive polls after this deployment.
- A previous standalone demo remains at https://6ab66fbbc0041008d26a8e51--aman-dashboard-varun.netlify.app (frontend revision 1419398). Its demo mode and scenario injection were verified with all `/api/` requests blocked. Use it as an immediate presentation fallback, not as a claim that it contains newer integration changes.
- This remains a static frontend deployment, not a Python backend or live voice deployment. The imported OpenAI key stays in Netlify environment settings; a protected production voice-session endpoint is still required, and VITE_VOICE_SESSION_URL is currently unset. Git-based automatic deployment has not been configured.
