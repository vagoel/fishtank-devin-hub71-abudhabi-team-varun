"""Per-device state derived from sticks3 frames: boot/sequence tracking, clock alignment
and the latest reported configuration. Kept in memory and periodically upserted to the
`devices` table (metadata only; sensor rows go through the batch writer)."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field

import asyncpg

from .models import TelemetryFrame

log = logging.getLogger(__name__)

# A frame arriving this much earlier than the current boot offset predicts means the device
# clock runs fast (or the initial anchor was taken during a network stall): re-anchor.
CLOCK_RESYNC_S = 0.25

_UPSERT = """
insert into devices (device_id, boot_id, last_sequence, frame, configuration, last_seen)
values ($1, $2, $3, $4, $5::jsonb, to_timestamp($6))
on conflict (device_id) do update set
    boot_id = excluded.boot_id,
    last_sequence = excluded.last_sequence,
    frame = excluded.frame,
    configuration = coalesce(excluded.configuration, devices.configuration),
    last_seen = excluded.last_seen
"""


@dataclass
class DeviceState:
    device_id: str
    boot_id: str
    last_sequence: int
    last_read_time_us: int
    frame: str | None = None
    configuration: dict | None = None
    last_seen: float = 0.0
    frames_received: int = 0
    dropped_frames: int = 0
    out_of_order_frames: int = 0
    reboots: int = 0
    clock_resyncs: int = 0
    clock_offset_s: float = field(default=0.0, repr=False)
    last_ts: float = field(default=0.0, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("clock_offset_s")
        d.pop("last_ts")
        return d


class DeviceRegistry:
    def __init__(self) -> None:
        self._states: dict[str, DeviceState] = {}
        self._dirty: set[str] = set()
        self.frames_received = 0
        self.dropped_frames = 0

    def observe(self, frames: list[TelemetryFrame], now: float) -> list[float]:
        """Record a batch of frames and return their wall-clock timestamps (epoch seconds).

        The device clock (`read_time_us`, monotonic since boot) is mapped to server time
        with a per-boot offset `wall = read_time + offset`. The offset is anchored on the
        first request of a boot (its least-delayed frame) and then kept fixed, so frames
        keep the device's exact spacing across requests regardless of network jitter.
        It is only re-anchored when a frame arrives `CLOCK_RESYNC_S` earlier than the
        offset predicts (device clock running fast), when `boot_id` changes, or when the
        device clock goes backwards; in-order frames are never stamped before the previous
        one, so re-anchoring cannot interleave a new batch with already-stored samples.
        """
        anchors = self._anchors(frames, now)
        return [self._observe(f, now, anchors[i]) for i, f in enumerate(frames)]

    @staticmethod
    def _anchors(frames: list[TelemetryFrame], now: float) -> list[float]:
        """Per frame, the candidate offset of its (device, boot, monotonic segment)."""
        seg_of: dict[tuple[str, str], tuple[int, int, int]] = {}  # -> (seg, last_rt, last_seq)
        keys: list[tuple[str, str, int]] = []
        best: dict[tuple[str, str, int], float] = {}
        for f in frames:
            k = (f.device_id, f.boot_id)
            seg, last_rt, last_seq = seg_of.get(k, (0, -1, -1))
            if f.sequence > last_seq:
                if f.read_time_us < last_rt:
                    seg += 1  # device clock went backwards: new segment
                seg_of[k] = (seg, f.read_time_us, f.sequence)
            key = (f.device_id, f.boot_id, seg)
            keys.append(key)
            cand = now - f.read_time_us / 1e6
            best[key] = min(best.get(key, cand), cand)
        return [best[k] for k in keys]

    def _observe(self, f: TelemetryFrame, now: float, anchor: float) -> float:
        rt = f.read_time_us / 1e6
        st = self._states.get(f.device_id)
        if st is None:
            st = DeviceState(
                f.device_id, f.boot_id, f.sequence - 1, f.read_time_us, clock_offset_s=anchor
            )
            self._states[f.device_id] = st
        elif st.boot_id != f.boot_id:
            st.boot_id = f.boot_id
            st.reboots += 1
            st.clock_offset_s = anchor
            st.last_sequence = f.sequence - 1
        elif f.sequence > st.last_sequence and f.read_time_us < st.last_read_time_us:
            # Device clock went backwards without a new boot_id (e.g. 32-bit micros wrap).
            st.clock_offset_s = anchor
        elif anchor < st.clock_offset_s - CLOCK_RESYNC_S:
            st.clock_offset_s = anchor
            st.clock_resyncs += 1

        ts = rt + st.clock_offset_s
        if f.sequence > st.last_sequence:
            gap = f.sequence - st.last_sequence - 1
            st.dropped_frames += gap
            self.dropped_frames += gap
            st.last_sequence = f.sequence
            st.last_read_time_us = f.read_time_us
            ts = max(ts, st.last_ts)
            st.last_ts = ts
        else:
            st.out_of_order_frames += 1
        if f.frame is not None:
            st.frame = f.frame
        if f.configuration is not None:
            st.configuration = f.configuration.model_dump(exclude_none=True)
        st.frames_received += 1
        st.last_seen = max(st.last_seen, now)
        self.frames_received += 1
        self._dirty.add(f.device_id)
        return ts

    def get(self, device_id: str) -> DeviceState | None:
        return self._states.get(device_id)

    def all(self) -> list[DeviceState]:
        return sorted(self._states.values(), key=lambda s: -s.last_seen)

    def snapshot(self) -> dict:
        states = self._states.values()
        return {
            "devices": len(self._states),
            "frames_received": self.frames_received,
            "dropped_frames": self.dropped_frames,
            "out_of_order_frames": sum(s.out_of_order_frames for s in states),
            "reboots": sum(s.reboots for s in states),
            "clock_resyncs": sum(s.clock_resyncs for s in states),
            "pending_upserts": len(self._dirty),
        }

    async def persist(self, pool: asyncpg.Pool) -> None:
        if not self._dirty:
            return
        dirty, self._dirty = self._dirty, set()
        rows = [
            (
                s.device_id,
                s.boot_id,
                s.last_sequence,
                s.frame,
                json.dumps(s.configuration) if s.configuration is not None else None,
                s.last_seen,
            )
            for s in (self._states[d] for d in dirty)
        ]
        try:
            async with pool.acquire() as conn:
                await conn.executemany(_UPSERT, rows)
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            self._dirty |= dirty
            log.warning("device upsert failed: %s", exc)

    async def run(self, pool: asyncpg.Pool, interval_s: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval_s)
                await self.persist(pool)
        except asyncio.CancelledError:
            await self.persist(pool)
            raise
