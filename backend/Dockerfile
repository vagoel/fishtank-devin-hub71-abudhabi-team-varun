FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY telemetry ./telemetry
# HeatGuard (dashboard, device WebSocket, incidents, voice, phone calls) runs in the same app
COPY heatguard ./heatguard
RUN pip install --no-cache-dir .

EXPOSE 8000
# Cloud Run passes $PORT; 8000 otherwise. Short graceful shutdown: devices and dashboards reconnect.
CMD ["sh", "-c", "uvicorn telemetry.main:app --host 0.0.0.0 --port ${PORT:-8000} --timeout-graceful-shutdown 3"]
