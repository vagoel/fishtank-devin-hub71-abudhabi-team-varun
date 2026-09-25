"""Simulate sticks3 devices streaming `sticks3.telemetry.v1` frames to the API.

    python scripts/simulate_device.py --devices 3 --hz 100 --batch 20 --url http://localhost:8000

Each device emits frames at `hz` from its own monotonic clock, POSTing them in batches of
`batch` frames. Motion is a slow tilt plus a wobble so the admin dashboard has something to
show; ~1 % of batches are dropped to exercise the sequence-gap counters.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import time

import httpx

FRAME = "display_landscape_usb_right_rh"
CONFIG = {
    "accelerometer_range_g": 8,
    "accelerometer_odr_hz": 100,
    "gyroscope_range_dps": 2000,
    "gyroscope_odr_hz": 200,
    "library": "M5Unified",
    "library_version": "0.2.22",
}


def make_frame(device_id: str, boot_id: str, seq: int, t_us: int, phase: float) -> dict:
    t = t_us / 1e6
    tilt = 0.35 * math.sin(t / 7 + phase)
    wobble = 0.02 * math.sin(2 * math.pi * 3 * t + phase)
    return {
        "schema": "sticks3.telemetry.v1",
        "device_id": device_id,
        "boot_id": boot_id,
        "sequence": seq,
        "read_time_us": t_us,
        "frame": FRAME,
        "fresh": {"accelerometer": True, "gyroscope": True, "temperature": seq % 10 == 0},
        "imu": {
            "acceleration_g": {
                "x": round(math.sin(tilt) + wobble + random.gauss(0, 0.004), 4),
                "y": round(0.03 * math.cos(t / 3 + phase) + random.gauss(0, 0.004), 4),
                "z": round(math.cos(tilt) + random.gauss(0, 0.004), 4),
            },
            "angular_velocity_dps": {
                "x": round(random.gauss(0, 0.3), 3),
                "y": round(20 * math.cos(t / 7 + phase) / 7 + random.gauss(0, 0.3), 3),
                "z": round(40 * math.sin(2 * math.pi * 0.2 * t + phase) + random.gauss(0, 0.3), 3),
            },
            "die_temperature_c": round(
                31 + 2 * math.sin(t / 60 + phase) + random.gauss(0, 0.02), 2
            ),
        },
        "configuration": CONFIG if seq == 0 else None,
    }


async def run_device(
    client: httpx.AsyncClient, url: str, device_id: str, hz: int, batch: int, stop: float
):
    boot_id = f"{random.getrandbits(32):08x}"
    phase = random.uniform(0, 2 * math.pi)
    seq = 0
    t_us = random.randint(0, 5_000_000)
    period_us = round(1e6 / hz)
    frames_sent = 0
    while time.time() < stop:
        frames = []
        for _ in range(batch):
            f = make_frame(device_id, boot_id, seq, t_us, phase)
            if f["configuration"] is None:
                del f["configuration"]
            frames.append(f)
            seq += 1
            t_us += period_us
        if random.random() < 0.01:  # simulate a lost batch -> sequence gap
            await asyncio.sleep(batch / hz)
            continue
        try:
            r = await client.post(f"{url}/v1/ingest/frames", json=frames)
            if r.status_code == 503:
                await asyncio.sleep(float(r.headers.get("Retry-After", "1")))
                continue
            r.raise_for_status()
            frames_sent += len(frames)
        except httpx.HTTPError as exc:
            print(f"{device_id}: {exc}")
            await asyncio.sleep(1)
        await asyncio.sleep(batch / hz)
    print(f"{device_id}: sent {frames_sent} frames")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--devices", type=int, default=3)
    ap.add_argument("--hz", type=int, default=100, help="frames per second per device")
    ap.add_argument("--batch", type=int, default=20, help="frames per POST")
    ap.add_argument("--seconds", type=float, default=60)
    args = ap.parse_args()
    stop = time.time() + args.seconds
    async with httpx.AsyncClient(timeout=10) as client:
        await asyncio.gather(
            *(
                run_device(
                    client,
                    args.url,
                    f"sticks3-{random.getrandbits(24):06x}",
                    args.hz,
                    args.batch,
                    stop,
                )
                for _ in range(args.devices)
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
