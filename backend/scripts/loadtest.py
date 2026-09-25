"""Simulate devices streaming temperature + gyro batches and report throughput.

    python scripts/loadtest.py --devices 20 --seconds 10 --batch 500 --hz 200

Each simulated device posts one mixed envelope per `batch / hz` seconds containing
`batch` temperature values and `batch` gyro samples.
"""

import argparse
import asyncio
import math
import random
import statistics
import time

import httpx


def envelope(device: str, n: int, hz: float, t0: float) -> dict:
    phase = random.random() * 10
    return {
        "temperature": [
            {
                "device_id": device,
                "start_ts": t0,
                "interval_ms": 1000 / hz,
                "values": [22 + 2 * math.sin(phase + i / 50) for i in range(n)],
            }
        ],
        "gyro": [
            {
                "device_id": device,
                "start_ts": t0,
                "interval_ms": 1000 / hz,
                "samples": [
                    [math.sin(phase + i / 7), math.cos(phase + i / 11), random.gauss(0, 0.02)]
                    for i in range(n)
                ],
            }
        ],
    }


async def device_loop(client, device, args, stop_at, latencies, counters):
    period = args.batch / args.hz
    t0 = time.time()
    while time.time() < stop_at:
        payload = envelope(device, args.batch, args.hz, t0)
        t0 += period
        started = time.perf_counter()
        try:
            r = await client.post("/v1/ingest", json=payload)
        except httpx.HTTPError as exc:
            counters["errors"] += 1
            print("request failed:", exc)
            await asyncio.sleep(1)
            continue
        latencies.append((time.perf_counter() - started) * 1000)
        if r.status_code == 202:
            counters["rows"] += r.json()["accepted"]
        elif r.status_code == 503:
            counters["backpressure"] += 1
            await asyncio.sleep(float(r.headers.get("Retry-After", "1")))
        else:
            counters["errors"] += 1
            print("unexpected status", r.status_code, r.text[:200])
        remaining = period - (time.perf_counter() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--devices", type=int, default=10)
    p.add_argument("--seconds", type=float, default=10)
    p.add_argument("--batch", type=int, default=200, help="samples per sensor per request")
    p.add_argument("--hz", type=float, default=100, help="device sample rate")
    args = p.parse_args()

    latencies: list[float] = []
    counters = {"rows": 0, "errors": 0, "backpressure": 0}
    stop_at = time.time() + args.seconds
    async with httpx.AsyncClient(base_url=args.url, timeout=30) as client:
        await asyncio.gather(
            *(
                device_loop(client, f"sim-{i:03d}", args, stop_at, latencies, counters)
                for i in range(args.devices)
            )
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            stats = (await client.get("/v1/stats")).json()["writer"]
            if stats["rows_pending"] == 0:
                break
            await asyncio.sleep(0.2)
        analytics = await client.get("/v1/analytics/gyro/sim-000", params={"last": 1000})
        t_an = analytics.elapsed.total_seconds() * 1000

    errors, bp = counters["errors"], counters["backpressure"]
    print(f"requests:        {len(latencies)}  (errors={errors}, 503s={bp})")
    print(f"rows accepted:   {counters['rows']}  ({counters['rows'] / args.seconds:,.0f} rows/s)")
    if latencies:
        latencies.sort()
        print(
            f"ingest latency:  p50={statistics.median(latencies):.1f}ms "
            f"p95={latencies[int(len(latencies) * 0.95) - 1]:.1f}ms max={latencies[-1]:.1f}ms"
        )
    print(f"writer:          {stats}")
    print(f"analytics(1000): {t_an:.1f}ms")


if __name__ == "__main__":
    asyncio.run(main())
