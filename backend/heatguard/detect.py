"""Server-side fall detection from `sticks3.telemetry.v1` frames, plus per-device display state.

Works from the telemetry stream alone, so a wearable running telemetry-only firmware still
raises fall incidents. `feed(app, frames)` runs after every accepted POST /v1/ingest/frames:
pure-Python arithmetic per frame, and each confirmed fall is written from a background task
through `incidents.record()` (so the `on_incident` hook fires) without slowing the ingest.

Fall rule, the same as the on-device firmware (P and fall_step() in device/main.py):
  * free-fall |a| < 0.5 g for >= 80 ms, then an impact |a| > 1.8 g within 1000 ms, or
  * a hard impact |a| > 3.0 g with no free-fall;
  * 0.8-2.0 s after the impact: std(|a|) < 0.12 g and mean |gyro| < 30 dps -> still.
  Free-fall + impact is a fall whatever the stillness; a bare hard impact only when still.
  At most one fall per device per 10 s, and none when the device itself reported a fall
  (source "device") within 10 s.

Time is the device clock (`read_time_us`, per `boot_id`). A new boot resets the state;
duplicate, out-of-order and late frames from an earlier boot are skipped; a hole in the
stream restarts a free-fall measurement.

Temperature is display only here (HeatGuard Core raises the heat alerts): the ESP32 die
temperature smoothed like Core.temp_rules and an estimate of the air near the band
(die - HEATGUARD_CHIP_OFFSET). It is a band/air estimate, not body temperature.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

from . import incidents

log = logging.getLogger("heatguard.detect")

ENABLED = os.environ.get("HEATGUARD_SERVER_FALLS", "1").lower() not in ("0", "false", "no")

# Fall rule. Keep in step with P in device/main.py.
FF_G = 0.5
FF_MIN_US = 80_000
IMPACT_G = 1.8
IMPACT_ONLY_G = 3.0
IMPACT_WIN_US = 1_000_000
SETTLE_US = 800_000
STILL_END_US = 2_000_000
STILL_STD_G = 0.12
STILL_GYRO_DPS = 30.0
REFRACTORY_US = 10_000_000
DEVICE_DEDUP_S = 10.0
MAX_PERIOD_US = 50_000  # longest believable sample period (20 Hz)
GAP_US = 250_000  # a longer hole restarts the free-fall measurement

# Display-only temperature, same smoothing and lines as Core.temp_rules.
CHIP_OFFSET = float(os.environ.get("HEATGUARD_CHIP_OFFSET", "35"))
CHIP_STOP = float(os.environ.get("HEATGUARD_CHIP_STOP", "70"))
CHIP_CALL = float(os.environ.get("HEATGUARD_CHIP_CALL", "80"))
EMA_ALPHA = 0.3
TEMP_NOTE = "band/air estimate from the chip die temperature, not body temperature"


class FallDetector:
    """Streaming port of the firmware's fall_step() on device time (microseconds)."""

    def __init__(self) -> None:
        self.last_fall_us: int | None = None  # refractory
        self.reset()

    def reset(self) -> None:
        self.state = 0  # 0 idle / timing a free-fall, 1 free-fall seen, 2 after an impact
        self.ff_start = 0
        self.ff_us = 0
        self.t_imp = 0
        self.imp_g = 0.0
        self.had_ff = False
        self.last_t: int | None = None
        self.period = 20_000
        self._clear_window()

    def _clear_window(self) -> None:
        self.n, self.s, self.s2, self.gn, self.gs = 0, 0.0, 0.0, 0, 0.0

    def _impact(self, t: int, a: float, had_ff: bool) -> None:
        self.state, self.t_imp, self.imp_g, self.had_ff = 2, t, a, had_ff
        self._clear_window()

    def step(self, t: int, a: float, g: float | None) -> dict | None:
        """One accelerometer sample: device time t (us), |a| in g, |gyro| in dps (None when
        not sampled). Returns the fall details once a fall is confirmed."""
        dt = t - self.last_t if self.last_t is not None else self.period
        self.last_t = t
        if 0 < dt <= MAX_PERIOD_US:
            self.period = dt
        elif dt > GAP_US and self.state == 0:
            self.ff_us = 0  # a free-fall cannot be measured across missing data
        if self.state == 0:
            if a < FF_G:
                if self.ff_us == 0:
                    self.ff_start = t
                self.ff_us = t - self.ff_start + self.period
                if self.ff_us >= FF_MIN_US:
                    self.state = 1
            else:
                self.ff_us = 0
            if a > IMPACT_ONLY_G:
                self._impact(t, a, had_ff=False)
        elif self.state == 1:
            if a < FF_G:
                self.ff_us = t - self.ff_start + self.period
            elif a > IMPACT_G:
                self._impact(t, a, had_ff=True)
            elif t - self.ff_start > self.ff_us + IMPACT_WIN_US:
                self.state, self.ff_us = 0, 0
        else:
            el = t - self.t_imp
            if el < SETTLE_US:
                self.imp_g = max(self.imp_g, a)  # peak of the impact ringing
            elif el < STILL_END_US:
                self.n += 1
                self.s += a
                self.s2 += a * a
                if g is not None:
                    self.gn += 1
                    self.gs += g
            else:
                return self._decide()
        return None

    def _decide(self) -> dict | None:
        std = 9.0
        if self.n:
            m = self.s / self.n
            std = math.sqrt(max(0.0, self.s2 / self.n - m * m))
        gyro = self.gs / self.gn if self.gn else 999.0  # no gyro data: not proven still
        still = std < STILL_STD_G and gyro < STILL_GYRO_DPS
        fall = (self.had_ff and self.imp_g >= IMPACT_G) or (still and self.imp_g >= IMPACT_ONLY_G)
        ev = {
            "impact_us": self.t_imp,
            "impact_g": round(self.imp_g, 2),
            "freefall_ms": round(self.ff_us / 1000) if self.had_ff else 0,
            "still": 1 if still else 0,
            "post_std_g": round(std, 3),
            "post_gyro_dps": round(gyro, 1),
        }
        self.state, self.ff_us = 0, 0
        self._clear_window()
        if not fall:
            return None
        if self.last_fall_us is not None and self.t_imp - self.last_fall_us < REFRACTORY_US:
            return None
        self.last_fall_us = self.t_imp
        return ev


class _Device:
    __slots__ = (
        "device_id",
        "boot_id",
        "old_boots",
        "last_seq",
        "last_rt",
        "fall",
        "ema",
        "last_fall_at",
        "falls",
        "updated_at",
    )

    def __init__(self, device_id: str, boot_id: str) -> None:
        self.device_id = device_id
        self.old_boots: deque[str] = deque(maxlen=4)
        self.last_fall_at: float | None = None
        self.falls: deque[float] = deque()  # wall times of server-detected falls, last 24 h
        self.updated_at = 0.0
        self.new_boot(boot_id)

    def new_boot(self, boot_id: str) -> None:
        self.boot_id = boot_id
        self.last_seq = -1
        self.last_rt = -1
        self.fall = FallDetector()
        self.ema: float | None = None


def heat_level(ema: float | None) -> str | None:
    if ema is None:
        return None
    return "call" if ema >= CHIP_CALL else ("stop_work" if ema >= CHIP_STOP else "ok")


def _iso(t: float | None) -> str | None:
    return None if t is None else datetime.fromtimestamp(t, timezone.utc).isoformat()


class Detector:
    def __init__(self) -> None:
        self._devices: dict[str, _Device] = {}
        self._tail: asyncio.Task | None = None
        self.counters = {"falls": 0, "stored": 0, "device_duplicates": 0, "errors": 0}

    # -- detection (pure, no I/O) ------------------------------------------------------

    def process(self, frames, registry=None) -> list[dict]:
        """Run the detectors over one accepted batch; return the falls it confirmed."""
        events = []
        now = time.time()
        for f in frames:
            dev = self._devices.get(f.device_id)
            if dev is None:
                dev = self._devices[f.device_id] = _Device(f.device_id, f.boot_id)
            elif dev.boot_id != f.boot_id:
                if f.boot_id in dev.old_boots:
                    continue  # late frame from an earlier boot
                dev.old_boots.append(dev.boot_id)
                dev.new_boot(f.boot_id)
            elif f.sequence <= dev.last_seq:
                continue  # duplicate or out of order
            elif f.read_time_us <= dev.last_rt:
                dev.fall = FallDetector()  # device clock went backwards: restart the timing
            dev.last_seq, dev.last_rt, dev.updated_at = f.sequence, f.read_time_us, now
            imu = f.imu
            if f.fresh.temperature and imu.die_temperature_c is not None:
                c = imu.die_temperature_c
                dev.ema = c if dev.ema is None else dev.ema + EMA_ALPHA * (c - dev.ema)
            if not f.fresh.accelerometer or imu.acceleration_g is None:
                continue
            acc, g = imu.acceleration_g, None
            if f.fresh.gyroscope and imu.angular_velocity_dps is not None:
                w = imu.angular_velocity_dps
                g = math.sqrt(w.x * w.x + w.y * w.y + w.z * w.z)
            ev = dev.fall.step(
                f.read_time_us, math.sqrt(acc.x * acc.x + acc.y * acc.y + acc.z * acc.z), g
            )
            if ev is not None:
                events.append(self._fall(dev, ev, registry, now))
        return events

    def _fall(self, dev: _Device, ev: dict, registry, now: float) -> dict:
        rt = ev.pop("impact_us")
        at = None  # wall time of the impact, same clock mapping as the stored frames
        st = registry.get(dev.device_id) if registry is not None else None
        if st is not None and st.boot_id == dev.boot_id:
            at = rt / 1e6 + st.clock_offset_s
        dev.last_fall_at = at or now
        dev.falls.append(dev.last_fall_at)
        self.counters["falls"] += 1
        log.info("server fall %s %s", dev.device_id, ev)
        return {
            "incident_id": f"{dev.device_id}-{dev.boot_id}-fall-{rt // 1000}",
            "device_id": dev.device_id,
            "boot_id": dev.boot_id,
            "read_time_us": rt,
            "occurred_at": _iso(at),
            "details": {**ev, "detector": "server"},
        }

    # -- storage (background) ------------------------------------------------------------

    def spawn(self, pool, registry, events: list[dict]) -> asyncio.Task:
        """Store falls in a background task, after the previous batch's (keeps order)."""
        loop = asyncio.get_running_loop()
        prev = self._tail
        if prev is not None and (prev.done() or prev.get_loop() is not loop):
            prev = None
        task = self._tail = loop.create_task(self._store(pool, registry, events, prev))
        return task

    async def _store(self, pool, registry, events: list[dict], prev) -> None:
        if prev is not None:
            await asyncio.wait([prev])
        # Backup only: wearables with on-device detection ask "are you OK?" and report the
        # fall themselves. Give them time to do so; escalate here only when they did not
        # (telemetry-only firmware, or the device's report was lost).
        grace = float(os.environ.get("HEATGUARD_SERVER_FALL_GRACE_S", "20"))
        if events and grace > 0:
            await asyncio.sleep(grace)
        for ev in events:
            try:
                if await device_reported(pool, ev):
                    self.counters["device_duplicates"] += 1
                    log.info("server fall %s skipped: the device reported it", ev["incident_id"])
                    continue
                await incidents.record(pool, to_incident(ev), registry)  # runs on_incident
                self.counters["stored"] += 1
            except Exception:  # noqa: BLE001 - a failed write must not stop the next ones
                self.counters["errors"] += 1
                log.exception("could not store server fall %s", ev["incident_id"])

    # -- display -------------------------------------------------------------------------

    def status(self, device_id: str) -> dict | None:
        dev = self._devices.get(device_id)
        if dev is None:
            return None
        cutoff = time.time() - 86_400
        while dev.falls and dev.falls[0] < cutoff:
            dev.falls.popleft()
        ema = dev.ema
        return {
            "device_id": device_id,
            "boot_id": dev.boot_id,
            "die_c": None if ema is None else round(ema, 1),
            "air_c_est": None if ema is None else round(ema - CHIP_OFFSET, 1),
            "heat_level": heat_level(ema),
            "temperature_note": TEMP_NOTE,
            "last_fall_at": _iso(dev.last_fall_at),
            "falls_24h": len(dev.falls),
            "updated_at": _iso(dev.updated_at),
        }

    def status_all(self) -> list[dict]:
        out = [self.status(d) for d in self._devices]
        return sorted(out, key=lambda s: s["updated_at"] or "", reverse=True)


def to_incident(ev: dict) -> incidents.IncidentIn:
    return incidents.IncidentIn(
        schema="heatguard.incident.v1",
        incident_id=ev["incident_id"],
        device_id=ev["device_id"],
        type="fall",
        status="suspected",
        severity="critical",
        occurred_at=ev["occurred_at"],
        boot_id=ev["boot_id"],
        read_time_us=ev["read_time_us"],
        details=ev["details"],
        source="server",
    )


async def device_reported(pool, ev: dict) -> bool:
    """True when the device already posted its own fall within DEVICE_DEDUP_S of this one."""
    at = datetime.fromisoformat(ev["occurred_at"]) if ev["occurred_at"] else None
    at = at or datetime.now(timezone.utc)
    win = timedelta(seconds=DEVICE_DEDUP_S)
    return bool(
        await pool.fetchval(
            "select 1 from incidents where device_id = $1 and type = 'fall' and source = 'device' "
            "and occurred_at between $2 and $3 limit 1",
            ev["device_id"],
            at - win,
            at + win,
        )
    )


DETECTOR = Detector()


def feed(app, frames) -> list[dict]:
    """Call after an accepted frame batch. Detects falls in-line (cheap) and stores them in
    the background. Never raises: detection must not break ingestion."""
    if not ENABLED:
        return []
    try:
        svc = getattr(app.state, "service", None)
        registry = getattr(svc, "registry", None)
        events = DETECTOR.process(frames, registry)
        if events and svc is not None:
            DETECTOR.spawn(svc.pool, registry, events)
        return events
    except Exception:  # noqa: BLE001
        log.exception("server fall detection failed")
        return []


def status(device_id: str) -> dict | None:
    return DETECTOR.status(device_id)


# -- HTTP (dashboard) ---------------------------------------------------------------------

router = APIRouter(tags=["heatguard"])


@router.get("/v1/heatguard/status")
async def all_status():
    """Latest server-side state per device seen since this instance started (memory only)."""
    return {
        "devices": DETECTOR.status_all(),
        "lines": {"chip_offset_c": CHIP_OFFSET, "stop_die_c": CHIP_STOP, "call_die_c": CHIP_CALL},
        "server_falls": dict(DETECTOR.counters),
        "temperature_note": TEMP_NOTE,
    }


@router.get("/v1/heatguard/status/{device_id}")
async def device_status(device_id: str):
    s = DETECTOR.status(device_id)
    if s is None:
        raise HTTPException(404, "device not seen by this instance")
    return s
