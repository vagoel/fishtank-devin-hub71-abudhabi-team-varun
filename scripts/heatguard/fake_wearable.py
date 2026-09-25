"""Stand-in wearable for testing the gateway without the StickS3.

Speaks the same UDP protocol as device/main.py. Scenarios:
  idle    stream a still wrist
  fall    free-fall, 4 g impact, stillness, then no answer to the prompt
  tremor  5 Hz shaking for 5 s, then no answer
"""
import argparse
import json
import math
import random
import socket
import time

ap = argparse.ArgumentParser()
ap.add_argument("--server", default="127.0.0.1")
ap.add_argument("--port", type=int, default=47800)
ap.add_argument("--id", default="stick-fake0001")
ap.add_argument("--scenario", default="fall", choices=["idle", "fall", "tremor"])
ap.add_argument("--seconds", type=float, default=14)
args = ap.parse_args()

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setblocking(False)
srv = (args.server, args.port)
PARAMS = {"FS_HZ": 50, "FF_G": 0.5, "FF_MIN_MS": 80, "IMPACT_G": 1.8, "IMPACT_ONLY_G": 3.0,
          "STILL_STD_G": 0.12, "TREM_MIN_HZ": 3.0, "TREM_MAX_HZ": 12.0, "TREM_MIN_DPS": 40,
          "TREM_SUSTAIN_S": 4, "PROMPT_S": 15}
seq = 0
pending = {}


def send(obj):
    sock.sendto(("@" + json.dumps(obj)).encode(), srv)


def event(kind, t, **detail):
    global seq
    seq += 1
    msg = {"k": "ev", "id": args.id, "seq": seq, "type": kind, "t": t, "detail": detail}
    pending[seq] = msg
    send(msg)


def sample(t_ms):
    """Returns ax, ay, az, gx, gy, gz for the scenario at device time t_ms."""
    n = lambda s: random.gauss(0, s)
    if args.scenario == "fall" and 3000 <= t_ms < 3300:
        return n(0.05), n(0.05), 0.1 + n(0.05), n(40), n(40), n(40)
    if args.scenario == "fall" and 3300 <= t_ms < 3360:
        return 2.5, 1.5, 2.6, 250, -180, 90
    if args.scenario == "fall" and 3360 <= t_ms < 3900:
        return n(0.3), 0.9 + n(0.3), n(0.3), n(60), n(60), n(60)
    if args.scenario == "fall" and t_ms >= 3900:
        return 0.02 + n(0.004), 0.98 + n(0.004), 0.1 + n(0.004), n(0.4), n(0.4), n(0.4)
    if args.scenario == "tremor" and 2000 <= t_ms < 7500:
        ph = 2 * math.pi * 5.2 * t_ms / 1000
        return 0.1 + 0.3 * math.sin(ph), 0.6, 0.78, 120 * math.sin(ph) + n(8), n(10), n(10)
    return 0.11 + n(0.004), 0.62 + n(0.004), 0.77 + n(0.004), n(0.4), n(0.4), n(0.4)


send({"k": "hello", "id": args.id, "fw": "fake-1", "ip": "127.0.0.1", "params": PARAMS})
t0 = time.time()
batch, t_ms, last_st, fired, esc = [], 0, 0, False, False
while t_ms < args.seconds * 1000:
    now_ms = int((time.time() - t0) * 1000)
    while t_ms <= now_ms:
        batch.append([round(v, 3) for v in sample(t_ms)])
        if len(batch) == 5:
            send({"k": "s", "id": args.id, "t0": t_ms - 80, "dt": 20, "d": batch})
            batch = []
        t_ms += 20
    if now_ms - last_st >= 1000:
        last_st = now_ms
        send({"k": "st", "id": args.id, "temp": 44.0, "bat": 77, "chg": 0, "act": 0.01, "wl": "rest",
              "am": 1.0, "gm": 0.5, "trem_hz": 0, "trem_amp": 0, "still_s": now_ms // 1000,
              "phase": "work", "ui": "normal", "free": 7000, "rssi": -55})
        for m in list(pending.values()):
            send(m)
    if args.scenario == "fall" and not fired and now_ms > 6400:
        fired = True
        event("fall", 3300, impact_g=4.0, freefall_ms=300, post_std=0.004, post_gyro=0.6)
    if args.scenario == "tremor" and not fired and now_ms > 6100:
        fired = True
        event("tremor", 6000, hz=5.2, rms_dps=85.0, cv=0.1, dur_s=4.0)
    if fired and not esc and now_ms > 9500:
        esc = True
        event(args.scenario + "_noresp", now_ms)
    try:
        while True:
            data, _ = sock.recvfrom(4096)
            c = json.loads(data.decode())
            if c.get("cmd") == "evack":
                pending.pop(c["seq"], None)
            if c.get("cmd") != "plan" or random.random() < 0.2:
                print("<-", json.dumps(c)[:150])
    except BlockingIOError:
        pass
    time.sleep(0.01)
print("unacked events:", list(pending))
