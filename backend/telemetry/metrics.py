"""Per-device ingest counters and a short per-second history, for the admin dashboard."""

from __future__ import annotations

import threading
import time
from collections import defaultdict


class IngestMeter:
    """Counts rows accepted per (device, kind) and keeps `window_s` seconds of per-second
    per-device totals so the dashboard can draw a live ingest-rate chart."""

    def __init__(self, window_s: int = 120):
        self.window_s = window_s
        self._lock = threading.Lock()
        self._totals: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._last_ingest: dict[str, float] = {}
        self._history: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def record(self, kind: str, device_id: str, rows: int, now: float | None = None) -> None:
        now = time.time() if now is None else now
        sec = int(now)
        with self._lock:
            self._totals[device_id][kind] += rows
            self._last_ingest[device_id] = now
            self._history[sec][device_id] += rows
            if len(self._history) > self.window_s + 1:
                cutoff = sec - self.window_s
                for s in [s for s in self._history if s < cutoff]:
                    del self._history[s]

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        cutoff = int(now) - self.window_s
        with self._lock:
            devices = {
                d: {"rows": dict(kinds), "last_ingest": self._last_ingest[d]}
                for d, kinds in self._totals.items()
            }
            history = [
                {"t": s, "rows": dict(per_dev)}
                for s, per_dev in sorted(self._history.items())
                if s >= cutoff
            ]
        return {"window_s": self.window_s, "devices": devices, "history": history}

    def device(self, device_id: str) -> dict | None:
        with self._lock:
            if device_id not in self._totals:
                return None
            return {
                "rows": dict(self._totals[device_id]),
                "last_ingest": self._last_ingest[device_id],
            }
