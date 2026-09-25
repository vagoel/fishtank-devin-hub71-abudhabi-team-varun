import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse

from heatguard import incidents as heatguard_incidents  # HeatGuard: incidents API

from .buffer import BufferStore
from .config import settings
from .db import create_pool
from .devices import DeviceRegistry
from .models import (
    AccelBatch,
    FramePayload,
    GyroBatch,
    IngestEnvelope,
    IngestResponse,
    TelemetryFrame,
    TemperatureBatch,
)
from .service import ACC, GYR, KINDS, TEMP, TelemetryService
from .writer import BatchWriter, QueueFull

logging.basicConfig(
    level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s"
)

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await create_pool()
    writer = BatchWriter(
        pool,
        flush_interval_ms=settings.flush_interval_ms,
        flush_max_rows=settings.flush_max_rows,
        queue_max_rows=settings.queue_max_rows,
    )
    writer.start()
    registry = DeviceRegistry()
    registry_task = asyncio.create_task(
        registry.run(pool, settings.device_upsert_interval_ms / 1000), name="device-registry"
    )
    app.state.service = TelemetryService(
        pool, writer, BufferStore(settings.ring_buffer_size), registry
    )
    try:
        yield
    finally:
        registry_task.cancel()
        with suppress(asyncio.CancelledError):
            await registry_task
        await writer.stop()
        await pool.close()


app = FastAPI(
    title="Telemetry API",
    version="0.1.0",
    description="High-throughput ingestion and analytics for IMU (accelerometer, gyroscope) "
    "and temperature streams.",
    lifespan=lifespan,
)


def get_service(request: Request) -> TelemetryService:
    return request.app.state.service


Service = Depends(get_service)
LastN = Query(100, ge=1, le=settings.max_analytics_points, description="Number of newest points")
Source = Query("auto", description="Serve from memory, database or auto (memory when warm)")
Frames = Body(..., description="A sticks3.telemetry.v1 frame or an array of frames")


def _accepted(svc: TelemetryService, n: int) -> IngestResponse:
    return IngestResponse(accepted=n, queued_rows=svc.writer.pending_rows)


def _queue_full(exc: QueueFull) -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc), headers={"Retry-After": "1"}
    )


# -- ingestion ----------------------------------------------------------------------------


@app.post("/v1/ingest/temperature", status_code=202, response_model=IngestResponse, tags=["ingest"])
async def ingest_temperature(batch: TemperatureBatch, svc: TelemetryService = Service):
    try:
        return _accepted(svc, svc.ingest_temperature(batch))
    except QueueFull as exc:
        raise _queue_full(exc) from exc


@app.post("/v1/ingest/gyro", status_code=202, response_model=IngestResponse, tags=["ingest"])
async def ingest_gyro(batch: GyroBatch, svc: TelemetryService = Service):
    try:
        return _accepted(svc, svc.ingest_gyro(batch))
    except QueueFull as exc:
        raise _queue_full(exc) from exc


@app.post("/v1/ingest/accel", status_code=202, response_model=IngestResponse, tags=["ingest"])
async def ingest_accel(batch: AccelBatch, svc: TelemetryService = Service):
    try:
        return _accepted(svc, svc.ingest_accel(batch))
    except QueueFull as exc:
        raise _queue_full(exc) from exc


@app.post("/v1/ingest", status_code=202, response_model=IngestResponse, tags=["ingest"])
async def ingest(envelope: IngestEnvelope, svc: TelemetryService = Service):
    """Mixed columnar payload: several temperature/gyro/accel batches in one request."""
    if envelope.is_empty():
        raise HTTPException(422, "empty envelope")
    n = 0
    try:
        svc.writer.ensure_capacity(envelope.total_samples())
        for t in envelope.temperature:
            n += svc.ingest_temperature(t)
        for g in envelope.gyro:
            n += svc.ingest_gyro(g)
        for a in envelope.accel:
            n += svc.ingest_accel(a)
    except QueueFull as exc:
        raise _queue_full(exc) from exc
    return _accepted(svc, n)


@app.post("/v1/ingest/frames", status_code=202, response_model=IngestResponse, tags=["ingest"])
async def ingest_frames(payload: FramePayload = Frames, svc: TelemetryService = Service):
    """Device contract `sticks3.telemetry.v1`: one frame or an array of frames.

    Each frame carries one accelerometer, gyroscope and die-temperature sample; only
    sensors flagged in `fresh` are stored. Frames are stamped from the device clock
    (`read_time_us`) aligned to server time per `boot_id`, and sequence gaps are tracked
    per device (see `GET /v1/devices/{device_id}`).
    """
    frames = [payload] if isinstance(payload, TelemetryFrame) else payload
    if not frames:
        raise HTTPException(422, "empty frame array")
    if len(frames) > settings.max_batch_size:
        raise HTTPException(422, f"too many frames (max {settings.max_batch_size})")
    try:
        n = svc.ingest_frames(frames)
    except QueueFull as exc:
        raise _queue_full(exc) from exc
    return IngestResponse(accepted=n, queued_rows=svc.writer.pending_rows, frames=len(frames))


# -- analytics ----------------------------------------------------------------------------

SourceT = Literal["auto", "memory", "database"]


@app.get("/v1/analytics/temperature/{device_id}", tags=["analytics"])
async def temperature_analytics(
    device_id: str, last: int = LastN, source: SourceT = Source, svc: TelemetryService = Service
):
    return await svc.analytics(TEMP, device_id, last, source)


@app.get("/v1/analytics/gyro/{device_id}", tags=["analytics"])
async def gyro_analytics(
    device_id: str, last: int = LastN, source: SourceT = Source, svc: TelemetryService = Service
):
    return await svc.analytics(GYR, device_id, last, source)


@app.get("/v1/analytics/accel/{device_id}", tags=["analytics"])
async def accel_analytics(
    device_id: str, last: int = LastN, source: SourceT = Source, svc: TelemetryService = Service
):
    return await svc.analytics(ACC, device_id, last, source)


@app.get("/v1/readings/temperature/{device_id}", tags=["readings"])
async def temperature_readings(
    device_id: str, last: int = LastN, source: SourceT = Source, svc: TelemetryService = Service
):
    ts, vals, src = await svc.last_points(TEMP, device_id, last, source)
    return {
        "device_id": device_id,
        "source": src,
        "count": len(ts),
        "ts": ts.tolist(),
        "values": vals[:, 0].tolist(),
    }


@app.get("/v1/readings/gyro/{device_id}", tags=["readings"])
async def gyro_readings(
    device_id: str, last: int = LastN, source: SourceT = Source, svc: TelemetryService = Service
):
    ts, vals, src = await svc.last_points(GYR, device_id, last, source)
    return {
        "device_id": device_id,
        "source": src,
        "count": len(ts),
        "ts": ts.tolist(),
        "samples": vals.tolist(),
    }


@app.get("/v1/readings/accel/{device_id}", tags=["readings"])
async def accel_readings(
    device_id: str, last: int = LastN, source: SourceT = Source, svc: TelemetryService = Service
):
    ts, vals, src = await svc.last_points(ACC, device_id, last, source)
    return {
        "device_id": device_id,
        "source": src,
        "count": len(ts),
        "ts": ts.tolist(),
        "samples": vals.tolist(),
    }


@app.get("/v1/devices", tags=["devices"])
async def devices(svc: TelemetryService = Service):
    return await svc.devices()


@app.get("/v1/devices/{device_id}", tags=["devices"])
async def device(device_id: str, svc: TelemetryService = Service):
    """Latest frame metadata: boot_id, sequence, configuration, dropped-frame counters."""
    info = await svc.device(device_id)
    if info is None:
        raise HTTPException(404, "unknown device")
    return info


# -- admin --------------------------------------------------------------------------------


@app.get("/admin", include_in_schema=False)
async def admin_dashboard():
    return FileResponse(STATIC_DIR / "admin.html", media_type="text/html")


@app.get("/v1/admin/overview", tags=["admin"])
async def admin_overview(svc: TelemetryService = Service):
    """Live, memory-only view for the dashboard: writer health, rows ingested per device
    (totals and a per-second history over the last ~2 minutes) and device frame state.
    Devices only appear here once they have sent data since the API started; use
    `GET /v1/devices` for everything known in the database.
    """
    return svc.overview()


# -- ops ----------------------------------------------------------------------------------


@app.get("/health", tags=["ops"])
async def health(svc: TelemetryService = Service):
    db_ok = await svc.db_ok()
    body = {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "writer": svc.writer.snapshot(),
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)


@app.get("/v1/stats", tags=["ops"])
async def stats(svc: TelemetryService = Service):
    return {
        "writer": svc.writer.snapshot(),
        "buffers": {
            "capacity_per_device": svc.buffers.capacity,
            **{f"{k}_devices": svc.buffers.devices(k) for k in KINDS},
        },
        "frames": svc.registry.snapshot(),
        "config": {
            "flush_interval_ms": settings.flush_interval_ms,
            "flush_max_rows": settings.flush_max_rows,
            "queue_max_rows": settings.queue_max_rows,
            "max_batch_size": settings.max_batch_size,
        },
    }


# -- HeatGuard ----------------------------------------------------------------------------
# Incidents API (heatguard.incident.v1, docs/heatguard/incidents.md), stored in `incidents`.
app.include_router(heatguard_incidents.router)
