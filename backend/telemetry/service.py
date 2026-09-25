"""Glue between HTTP models, the in-memory buffers, the batch writer and the database."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import asyncpg
import numpy as np

from .analytics import accel_analytics, gyro_analytics, temperature_analytics
from .buffer import BufferStore
from .devices import DeviceRegistry
from .metrics import IngestMeter
from .models import AccelBatch, GyroBatch, TelemetryFrame, TemperatureBatch
from .writer import ACCEL, GYRO, TEMPERATURE, BatchWriter

TEMP = "temperature"
GYR = "gyro"
ACC = "accel"
KINDS = (TEMP, GYR, ACC)

_TABLES = {TEMP: TEMPERATURE, GYR: GYRO, ACC: ACCEL}
_NCOLS = {TEMP: 1, GYR: 3, ACC: 3}
_ANALYTICS = {TEMP: temperature_analytics, GYR: gyro_analytics, ACC: accel_analytics}
_SQL_LAST = {
    TEMP: "select ts, value from temperature_readings "
    "where device_id = $1 order by ts desc limit $2",
    GYR: "select ts, x, y, z from gyro_readings where device_id = $1 order by ts desc limit $2",
    ACC: "select ts, x, y, z from accel_readings where device_id = $1 order by ts desc limit $2",
}


class TelemetryService:
    def __init__(
        self,
        pool: asyncpg.Pool,
        writer: BatchWriter,
        buffers: BufferStore,
        registry: DeviceRegistry | None = None,
        meter: IngestMeter | None = None,
    ):
        self.pool = pool
        self.writer = writer
        self.buffers = buffers
        self.registry = registry or DeviceRegistry()
        self.meter = meter or IngestMeter()

    # -- ingestion -------------------------------------------------------------------

    def ingest_temperature(self, batch: TemperatureBatch) -> int:
        ts = batch.resolve_timestamps()
        vals = np.asarray(batch.values, dtype=np.float32).reshape(-1, 1)
        return self._ingest(TEMP, batch.device_id, ts, vals)

    def ingest_gyro(self, batch: GyroBatch) -> int:
        ts = batch.resolve_timestamps()
        vals = np.asarray(batch.samples, dtype=np.float32).reshape(-1, 3)
        return self._ingest(GYR, batch.device_id, ts, vals)

    def ingest_accel(self, batch: AccelBatch) -> int:
        ts = batch.resolve_timestamps()
        vals = np.asarray(batch.samples, dtype=np.float32).reshape(-1, 3)
        return self._ingest(ACC, batch.device_id, ts, vals)

    def ingest_frames(self, frames: list[TelemetryFrame]) -> int:
        """Fan sticks3 frames out to the per-sensor stores. Returns the number of rows."""
        self.writer.ensure_capacity(sum(f.sample_count() for f in frames))
        stamps = self.registry.observe(frames, time.time())
        groups: dict[tuple[str, str], tuple[list[float], list[tuple]]] = defaultdict(
            lambda: ([], [])
        )
        for f, t in zip(frames, stamps, strict=True):
            if f.fresh.temperature:
                g = groups[(TEMP, f.device_id)]
                g[0].append(t)
                g[1].append((f.imu.die_temperature_c,))
            if f.fresh.gyroscope:
                g = groups[(GYR, f.device_id)]
                g[0].append(t)
                g[1].append(f.imu.angular_velocity_dps.as_tuple())
            if f.fresh.accelerometer:
                g = groups[(ACC, f.device_id)]
                g[0].append(t)
                g[1].append(f.imu.acceleration_g.as_tuple())
        n = 0
        for (kind, device_id), (ts_f, vals) in groups.items():
            ts = [datetime.fromtimestamp(t, tz=timezone.utc) for t in ts_f]
            arr = np.asarray(vals, dtype=np.float32).reshape(-1, _NCOLS[kind])
            n += self._ingest(kind, device_id, ts, arr)
        return n

    def _ingest(self, kind: str, device_id: str, ts: list[datetime], vals: np.ndarray) -> int:
        ts_f = np.fromiter((t.timestamp() for t in ts), dtype=np.float64, count=len(ts))
        order = np.argsort(ts_f, kind="stable")
        if not np.array_equal(order, np.arange(len(order))):
            ts_f = ts_f[order]
            vals = vals[order]
            ts = [ts[i] for i in order]
        rows = [(device_id, t, *v) for t, v in zip(ts, vals.tolist(), strict=True)]
        self.writer.enqueue(_TABLES[kind], rows)
        self.buffers.append(kind, device_id, ts_f, vals, _NCOLS[kind])
        self.meter.record(kind, device_id, len(rows))
        return len(rows)

    # -- reads ------------------------------------------------------------------------

    async def last_points(
        self, kind: str, device_id: str, n: int, source: str = "auto"
    ) -> tuple[np.ndarray, np.ndarray, str]:
        """Return (ts_epoch_s, values, source) for the newest `n` samples, oldest first."""
        if source != "database":
            cached = self.buffers.last(kind, device_id, n)
            if cached is not None and (len(cached[0]) >= n or source == "memory"):
                return cached[0], cached[1], "memory"
            if source == "memory":
                return np.empty(0), np.empty((0, _NCOLS[kind])), "memory"
        rows = await self.pool.fetch(_SQL_LAST[kind], device_id, n)
        rows.reverse()
        ts = np.fromiter((r[0].timestamp() for r in rows), dtype=np.float64, count=len(rows))
        vals = np.array([tuple(r)[1:] for r in rows], dtype=np.float32).reshape(-1, _NCOLS[kind])
        return ts, vals, "database"

    async def analytics(self, kind: str, device_id: str, n: int, source: str = "auto") -> dict:
        ts, vals, src = await self.last_points(kind, device_id, n, source)
        if len(ts) == 0:
            return {"device_id": device_id, "count": 0, "source": src}
        result = _ANALYTICS[kind](ts, vals)
        result["device_id"] = device_id
        result["requested"] = n
        result["source"] = src
        return result

    async def devices(self) -> list[dict]:
        rows = await self.pool.fetch(
            """
            select device_id,
                   max(last_ts) as last_seen,
                   bool_or(kind = 'temperature') as has_temperature,
                   bool_or(kind = 'gyro') as has_gyro,
                   bool_or(kind = 'accel') as has_accel
            from (
                select device_id, 'temperature' as kind, max(ts) as last_ts
                from temperature_readings group by device_id
                union all
                select device_id, 'gyro', max(ts) from gyro_readings group by device_id
                union all
                select device_id, 'accel', max(ts) from accel_readings group by device_id
            ) s
            group by device_id
            order by last_seen desc
            """
        )
        return [
            {
                "device_id": r["device_id"],
                "last_seen": r["last_seen"].astimezone(timezone.utc).isoformat(),
                "has_temperature": r["has_temperature"],
                "has_gyro": r["has_gyro"],
                "has_accel": r["has_accel"],
                "buffered": {k: self.buffers.count(k, r["device_id"]) for k in KINDS},
                "ingested": self.meter.device(r["device_id"]),
                "state": self._state(r["device_id"]),
            }
            for r in rows
        ]

    def overview(self) -> dict:
        """Memory-only snapshot for the admin dashboard: writer, per-device ingest totals
        and per-second history, frame state. Cheap enough to poll every second."""
        meter = self.meter.snapshot()
        devices = []
        for device_id, m in meter["devices"].items():
            devices.append(
                {
                    "device_id": device_id,
                    "rows": {k: m["rows"].get(k, 0) for k in KINDS},
                    "last_ingest": m["last_ingest"],
                    "buffered": {k: self.buffers.count(k, device_id) for k in KINDS},
                    "state": self._state(device_id),
                }
            )
        devices.sort(key=lambda d: d["last_ingest"], reverse=True)
        return {
            "now": time.time(),
            "writer": {**self.writer.snapshot(), "queue_max_rows": self.writer.queue_max_rows},
            "frames": self.registry.snapshot(),
            "devices": devices,
            "rate": {"window_s": meter["window_s"], "history": meter["history"]},
        }

    async def device(self, device_id: str) -> dict | None:
        state = self._state(device_id)
        if state is None:
            row = await self.pool.fetchrow("select * from devices where device_id = $1", device_id)
            if row is None:
                return None
            state = dict(row)
            state["last_seen"] = row["last_seen"].timestamp()
            if state["configuration"] is not None:
                state["configuration"] = json.loads(state["configuration"])
        return {
            "device_id": device_id,
            "buffered": {k: self.buffers.count(k, device_id) for k in KINDS},
            "ingested": self.meter.device(device_id),
            "state": state,
        }

    def _state(self, device_id: str) -> dict | None:
        st = self.registry.get(device_id)
        return st.to_dict() if st else None

    async def db_ok(self) -> bool:
        try:
            await self.pool.fetchval("select 1")
            return True
        except Exception:  # noqa: BLE001
            return False
