"""Watch the wearable over USB and report what its on-device detector sees.

Prints one line per second with the min/max |a| the wrist measured, and every
event the firmware raises (fall, impact, fall_ok, fall_noresp, ...). Detection
happens on the StickS3; this only listens.

  .venv/bin/python scripts/fall_monitor.py --seconds 90
"""
import argparse
import glob
import json
import math
import time

import serial

ap = argparse.ArgumentParser()
ap.add_argument("--port", default=None)
ap.add_argument("--seconds", type=float, default=90)
args = ap.parse_args()

port = args.port or sorted(glob.glob("/dev/cu.usbmodem*"))[0]
ser = serial.Serial(port, 115200, timeout=0.2)
ser.write(b'{"cmd":"hello"}\n')
t0 = time.time()
lo, hi, n, last = 9.0, 0.0, 0, time.time()
print("listening on %s for %d s -- drop the stick onto something soft" % (port, args.seconds), flush=True)
while time.time() - t0 < args.seconds:
    raw = ser.readline().decode("utf-8", "replace").strip()
    if raw.startswith("@{"):
        try:
            m = json.loads(raw[1:])
        except ValueError:
            m = {}
        k = m.get("k")
        if k == "s":
            for ax, ay, az, gx, gy, gz in m.get("d", []):
                a = math.sqrt(ax * ax + ay * ay + az * az)
                lo, hi, n = min(lo, a), max(hi, a), n + 1
        elif k == "ev":
            print("%6.1fs  EVENT %-12s %s" % (time.time() - t0, m.get("type"), json.dumps(m.get("detail") or {})), flush=True)
            if m.get("seq") is not None:
                ser.write(('{"cmd":"evack","seq":%d}\n' % m["seq"]).encode())
        elif k == "hello":
            print("firmware %s on %s" % (m.get("fw"), m.get("id")), flush=True)
        elif k == "st" and m.get("ui") not in (None, "normal"):
            print("%6.1fs  screen: %s" % (time.time() - t0, m.get("ui")), flush=True)
    if time.time() - last >= 1.0:
        if n:
            flag = "  <- free-fall" if lo < 0.5 else ""
            flag += "  <- impact" if hi > 1.8 else ""
            print("%6.1fs  |a| min %.2f g  max %.2f g%s" % (time.time() - t0, lo, hi, flag), flush=True)
        lo, hi, n, last = 9.0, 0.0, 0, time.time()
print("done")
