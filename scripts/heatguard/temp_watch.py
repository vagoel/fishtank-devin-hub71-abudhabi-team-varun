"""Continuous chip-temperature readout from the live wearable, for calibration.

Reads the server's state once a second and prints the StickS3's chip temperature
(esp32.mcu_temperature(), whole degrees), the transport, battery and activity,
plus a running min / max / mean. Type a reference reading and press Enter at any
time (e.g. "31.5") to log a calibration pair against the current chip value.

  .venv/bin/python scripts/temp_watch.py            # runs until Ctrl-C
  .venv/bin/python scripts/temp_watch.py --seconds 60
"""
import argparse
import json
import select
import sys
import time

import httpx

ap = argparse.ArgumentParser()
ap.add_argument("--server", default="http://localhost:8000")
ap.add_argument("--seconds", type=float, default=0)
args = ap.parse_args()

vals, pairs, t0 = [], [], time.time()
print("time      chip°C  min   max   mean   link    batt  activity   (type a reference °C + Enter to log a pair)")
while not args.seconds or time.time() - t0 < args.seconds:
    try:
        s = httpx.get(args.server + "/api/v1/state", timeout=3).json()
    except Exception as e:
        print("server unreachable:", type(e).__name__)
        time.sleep(2)
        continue
    live = s["live"][0] if s["live"] else None
    if not live or live.get("device_temp_c") is None:
        print("no wearable reporting yet")
        time.sleep(2)
        continue
    t = float(live["device_temp_c"])
    vals.append(t)
    w = next((x for x in s["config"]["wearables"] if x["id"] == live["id"]), {})
    print("%s  %5.1f  %5.1f %5.1f %6.2f  %-6s  %3s%%  %.3f" % (
        time.strftime("%H:%M:%S"), t, min(vals), max(vals), sum(vals) / len(vals),
        w.get("transport") or "-", live.get("battery"), (live.get("motion") or {}).get("act") or 0), flush=True)
    if select.select([sys.stdin], [], [], 0)[0]:
        line = sys.stdin.readline().strip()
        try:
            ref = float(line)
            pairs.append((t, ref))
            print("  logged pair: chip %.1f °C  ->  reference %.1f °C (offset %+.1f)" % (t, ref, ref - t))
        except ValueError:
            pass
    time.sleep(1)
if pairs:
    print("pairs:", json.dumps(pairs))
