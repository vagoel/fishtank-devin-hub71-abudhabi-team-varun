"""Fixed-capacity, numpy-backed circular buffers holding the most recent samples per device.

Each buffer stores timestamps (epoch seconds, float64) and one or more value columns
(float32). Appends and windowed reads are O(k) in the number of rows touched, with no
Python-level per-sample loops.
"""

from __future__ import annotations

import threading
from collections import defaultdict

import numpy as np


class RingBuffer:
    __slots__ = ("capacity", "ncols", "_ts", "_vals", "_head", "_count")

    def __init__(self, capacity: int, ncols: int):
        self.capacity = capacity
        self.ncols = ncols
        self._ts = np.empty(capacity, dtype=np.float64)
        self._vals = np.empty((capacity, ncols), dtype=np.float32)
        self._head = 0  # next write position
        self._count = 0

    def __len__(self) -> int:
        return self._count

    def newest_ts(self) -> float | None:
        if self._count == 0:
            return None
        return float(self._ts[(self._head - 1) % self.capacity])

    def merge(self, ts: np.ndarray, vals: np.ndarray) -> None:
        """Insert samples that are older than the newest buffered one, keeping order."""
        old_ts, old_vals = self.last(self._count)
        all_ts = np.concatenate([old_ts, ts])
        all_vals = np.concatenate([old_vals, vals])
        order = np.argsort(all_ts, kind="stable")
        self._head = 0
        self._count = 0
        self.append_many(all_ts[order], all_vals[order])

    def append_many(self, ts: np.ndarray, vals: np.ndarray) -> None:
        n = len(ts)
        if n == 0:
            return
        if n >= self.capacity:
            # Only the tail of the incoming batch survives.
            self._ts[:] = ts[-self.capacity :]
            self._vals[:] = vals[-self.capacity :]
            self._head = 0
            self._count = self.capacity
            return
        end = self._head + n
        if end <= self.capacity:
            self._ts[self._head : end] = ts
            self._vals[self._head : end] = vals
        else:
            first = self.capacity - self._head
            self._ts[self._head :] = ts[:first]
            self._vals[self._head :] = vals[:first]
            self._ts[: n - first] = ts[first:]
            self._vals[: n - first] = vals[first:]
        self._head = end % self.capacity
        self._count = min(self.capacity, self._count + n)

    def last(self, n: int) -> tuple[np.ndarray, np.ndarray]:
        """Return the newest `n` rows in chronological order (copies)."""
        n = min(n, self._count)
        if n == 0:
            return np.empty(0), np.empty((0, self.ncols), dtype=np.float32)
        start = (self._head - n) % self.capacity
        idx = (start + np.arange(n)) % self.capacity
        return self._ts[idx], self._vals[idx]


class BufferStore:
    """Per-(kind, device) ring buffers, guarded by a lock so the writer and readers can
    run from different threads if needed."""

    def __init__(self, capacity: int):
        self.capacity = capacity
        self._lock = threading.Lock()
        self._buffers: dict[str, dict[str, RingBuffer]] = defaultdict(dict)

    def append(self, kind: str, device_id: str, ts: np.ndarray, vals: np.ndarray, ncols: int):
        with self._lock:
            buf = self._buffers[kind].get(device_id)
            if buf is None:
                buf = self._buffers[kind][device_id] = RingBuffer(self.capacity, ncols)
            newest = buf.newest_ts()
            if newest is not None and ts[0] < newest:
                buf.merge(ts, vals)
            else:
                buf.append_many(ts, vals)

    def last(self, kind: str, device_id: str, n: int) -> tuple[np.ndarray, np.ndarray] | None:
        with self._lock:
            buf = self._buffers[kind].get(device_id)
            if buf is None:
                return None
            return buf.last(n)

    def count(self, kind: str, device_id: str) -> int:
        with self._lock:
            buf = self._buffers[kind].get(device_id)
            return len(buf) if buf else 0

    def devices(self, kind: str) -> list[str]:
        with self._lock:
            return sorted(self._buffers[kind])
