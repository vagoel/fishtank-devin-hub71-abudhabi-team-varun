"""Asynchronous batched writer.

Ingest handlers enqueue rows and return immediately; a background task drains the queue
every `flush_interval_ms` (or as soon as `flush_max_rows` accumulate) and bulk-loads the
rows with Postgres COPY, which is an order of magnitude faster than multi-row INSERTs.
Failed flushes are retried with exponential backoff without losing rows; if the queue
grows past `queue_max_rows` the API applies backpressure by rejecting new batches.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

import asyncpg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[str, ...]


TEMPERATURE = Table("temperature_readings", ("device_id", "ts", "value"))
GYRO = Table("gyro_readings", ("device_id", "ts", "x", "y", "z"))
ACCEL = Table("accel_readings", ("device_id", "ts", "x", "y", "z"))
TABLES = (TEMPERATURE, GYRO, ACCEL)


class QueueFull(Exception):
    pass


@dataclass
class WriterStats:
    rows_queued: int = 0
    rows_written: int = 0
    flushes: int = 0
    failed_flushes: int = 0
    rejected_rows: int = 0
    last_flush_ms: float = 0.0
    total_flush_ms: float = 0.0
    last_error: str | None = None
    pending: dict[str, int] = field(default_factory=dict)

    def snapshot(self, pending: dict[str, int]) -> dict:
        return {
            "rows_queued": self.rows_queued,
            "rows_written": self.rows_written,
            "rows_pending": sum(pending.values()),
            "pending_by_table": pending,
            "flushes": self.flushes,
            "failed_flushes": self.failed_flushes,
            "rejected_rows": self.rejected_rows,
            "last_flush_ms": round(self.last_flush_ms, 2),
            "avg_flush_ms": round(self.total_flush_ms / self.flushes, 2) if self.flushes else 0,
            "last_error": self.last_error,
        }


class BatchWriter:
    def __init__(
        self,
        pool: asyncpg.Pool,
        *,
        flush_interval_ms: int,
        flush_max_rows: int,
        queue_max_rows: int,
    ):
        self._pool = pool
        self._interval = flush_interval_ms / 1000
        self._max_rows = flush_max_rows
        self._queue_max = queue_max_rows
        self._queues: dict[Table, deque[list[tuple]]] = {t: deque() for t in TABLES}
        self._pending = dict.fromkeys(TABLES, 0)
        self._wake = asyncio.Event()
        self._stop = False
        self._task: asyncio.Task | None = None
        self._backoff = 0.0
        self.stats = WriterStats()

    # -- producer side -------------------------------------------------------------

    @property
    def pending_rows(self) -> int:
        return sum(self._pending.values())

    @property
    def queue_max_rows(self) -> int:
        return self._queue_max

    def ensure_capacity(self, n: int) -> None:
        if self.pending_rows + n > self._queue_max:
            self.stats.rejected_rows += n
            raise QueueFull(f"writer queue is full ({self.pending_rows} rows pending)")

    def enqueue(self, table: Table, rows: list[tuple]) -> None:
        n = len(rows)
        self.ensure_capacity(n)
        self._queues[table].append(rows)
        self._pending[table] += n
        self.stats.rows_queued += n
        if self._pending[table] >= self._max_rows:
            self._wake.set()

    # -- lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="batch-writer")

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        if self._task:
            await self._task
        await self._flush_all()
        if self.pending_rows:
            log.error("shutting down with %d unflushed rows", self.pending_rows)

    def snapshot(self) -> dict:
        return self.stats.snapshot({t.name: n for t, n in self._pending.items()})

    # -- consumer side -------------------------------------------------------------

    async def _run(self) -> None:
        while not self._stop:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            await self._flush_all()
            if self._backoff:
                await asyncio.sleep(self._backoff)

    async def _flush_all(self) -> None:
        for table in self._queues:
            while self._pending[table]:
                if not await self._flush_table(table):
                    return

    def _take(self, table: Table) -> list[tuple]:
        q = self._queues[table]
        rows: list[tuple] = []
        while q and len(rows) < self._max_rows:
            rows.extend(q.popleft())
        self._pending[table] -= len(rows)
        return rows

    async def _flush_table(self, table: Table) -> bool:
        rows = self._take(table)
        if not rows:
            return True
        t0 = time.perf_counter()
        try:
            async with self._pool.acquire() as conn:
                await conn.copy_records_to_table(table.name, records=rows, columns=table.columns)
        except Exception as exc:  # noqa: BLE001 - any DB error must not lose rows
            self._queues[table].appendleft(rows)
            self._pending[table] += len(rows)
            self.stats.failed_flushes += 1
            self.stats.last_error = f"{type(exc).__name__}: {exc}"
            self._backoff = min(10.0, (self._backoff or 0.25) * 2)
            log.warning("flush of %d rows to %s failed: %s", len(rows), table.name, exc)
            return False
        elapsed = (time.perf_counter() - t0) * 1000
        self._backoff = 0.0
        self.stats.flushes += 1
        self.stats.rows_written += len(rows)
        self.stats.last_flush_ms = elapsed
        self.stats.total_flush_ms += elapsed
        log.debug("flushed %d rows to %s in %.1fms", len(rows), table.name, elapsed)
        return True
