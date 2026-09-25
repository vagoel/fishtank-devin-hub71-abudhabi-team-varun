# Monorepo

React frontend + Python (FastAPI) backend in a single repo.

## Structure

- `frontend/` — React app (Vite, npm workspace)
- `backend/` — FastAPI app (`app/main.py`), Python venv in `backend/.venv`

## Setup

```bash
npm run setup
```

This installs frontend deps and creates the backend virtualenv with its requirements.

## Development

```bash
npm run dev
```

Runs both servers together:

- Frontend: http://localhost:5173 (Vite proxies `/api` → `localhost:8000`)
- Backend: http://localhost:8000 (docs at `/docs`)

Or individually: `npm run dev:frontend` / `npm run dev:backend`.
