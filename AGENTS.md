# Project workflow

## Scope and architecture

- React 19 / Vite 8 JavaScript frontend is an npm workspace. Install dependencies from the repository root with `--workspace frontend` for frontend packages; the root package-lock.json is authoritative. The older frontend/package-lock.json belongs to the original scaffold and is not used for workspace installs.
- React, React DOM, and Vite are pinned in the root tooling dependencies/overrides and frontend manifest to keep npm's auto-installed peer dependencies aligned. Multiple React copies cause invalid-hook-call failures in component tests.
- If npm 10 fails with the workspace `edgesOut` resolution error, use `npx --yes npm@11.19.1 install`. Do not bypass peer dependency validation or alter security settings.
- Python API, hardware firmware, and Devin analysis logic belong to the other team members. The dashboard consumes their results; it does not implement those systems.
- Dashboard mode defaults to explicitly labeled fictional demo data. `VITE_DATA_MODE=api` selects the HTTP adapter. API failures must never silently load demo records.
- Incident severity and review status are separate. Acknowledgement and simulated dispatch do not resolve an incident. Dispatch requires an explicit operator confirmation and is disabled in API mode until a real authorized integration is separately agreed.
- Temperature readings are band surface readings, not core body temperature. Worker locations are assigned site locations, not live GPS. Do not invent unsupported medical measurements or agent analysis.

## Commands

Run from the repository root:

- `npm install` — install the workspace dependencies.
- `npm run dev:frontend` — run the dashboard; demo mode does not require Python.
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

Proposed routes are isolated in src/services/dashboardApi.js and must be aligned with the teammate's actual API:

- `GET /api/dashboard` returns `{sites, workers, incidents, dispatches, cursor, serverTime}`.
- `GET /api/events?cursor=...` returns `{incidents, cursor, hasMore}` and may include updated full sites/workers/dispatches arrays. Incident changes are merged by stable ID and increasing integer revision. Paginated updates are committed only when all pages succeed.
- `POST /api/incidents/:id/review` accepts `{revision, status, reason?}` and returns a full validated dashboard snapshot. Return 409 for a conflicting revision.
- Site records require id, name, coordinates `[longitude, latitude]`, lastSeen, and online/offline connectivity. Optional footprints are closed coordinate rings.
- Worker records require id, name, siteId, bandId, lastSeen, connectivity; role, temperature and readings drive the worker panel. Measurements must be numbers or explicitly absent.
- Incident records require id, revision, siteId, workerId, type, severity (`warning`/`critical`), status, createdAt, and a timeline of `{at, label}`. Analysis has pending/ready/failed state; ready analysis includes summary and recommendation. Evidence includes label, value, and source.
- Dispatch records include id, incidentId, siteId, selected services, createdAt, and whether simulated. Service identifiers for the demo are medical, safety, and rescue. No real dispatch endpoint is called.
- The poll interval defaults to 2000ms. The UI keeps last valid data on errors and marks it stale. Unknown/offline data must not be rendered as healthy.

## Mapping

- MapLibre uses OpenFreeMap tiles and required OpenFreeMap/OpenMapTiles/OpenStreetMap attribution. Public map data needs connectivity and has no uptime guarantee.
- Three.js shares MapLibre's WebGL canvas for the four illustrative landmarks. Do not add an independent camera/render loop or leak shared WebGL resources on remount.
- MapLibre 6 uses named module exports and a separate worker. Import the worker with Vite's `?worker&url` so its dependencies are bundled. A plain `?url` works in development but leaves its dependency imports unresolved in a production build.
- To test a running production preview, set `PLAYWRIGHT_BASE_URL=http://127.0.0.1:5176` when running the browser suite. The client-bundle check also verifies that a build made with the fake `AMAN_BUILD_SECRET_SENTINEL` server key does not expose it or the session handler to the browser.
- The style's `distance` filters exclude base-map building polygons near custom landmarks. `within` does not evaluate polygon features and is not suitable for this masking.
- Keep an accessible site/incident list usable when map tiles or WebGL fail. All construction records and dispatch routes in demo mode are fictional.
