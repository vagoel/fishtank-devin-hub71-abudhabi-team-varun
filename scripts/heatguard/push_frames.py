"""Stopgap StickS3 firmware: stream sticks3.telemetry.v1 frames to the backend over HTTPS.

Samples the IMU at 50 Hz and POSTs a JSON array of frames to /v1/ingest/frames about
once a second on a kept-alive TLS connection, from a second thread so sampling never
waits on the network. WiFi credentials come from NVS (uiflow/ssid0, pswd0); the backend
URL from NVS uiflow/hg_url's host if set, else the Cloud Run host below.

No fall detection or HeatGuard features: this only proves telemetry end to end.
"""
import binascii
import esp32
import gc
import machine
import network
import random
import socket
import ssl
import time
import _thread

import M5

HOST = "telemetry-backend-501582454609.asia-northeast1.run.app"
PATH = "/v1/ingest/frames"
FRAME = "display_portrait_usb_down_rh"
HZ = 50
DT_MS = 1000 // HZ

M5.begin()
d = M5.Display
d.setRotation(0)
cv = d.newCanvas(135, 240)


def nvs(key):
    try:
        return esp32.NVS("uiflow").get_str(key)
    except Exception:
        return ""


dev_id = "sticks3-" + binascii.hexlify(machine.unique_id()).decode()[:6]
boot_id = "%08x" % random.getrandbits(32)
CONFIG = ('{"accelerometer_range_g":8,"accelerometer_odr_hz":50,"gyroscope_range_dps":2000,'
          '"gyroscope_odr_hz":50,"library":"M5Unified (UiFlow2 MicroPython)","library_version":"push-0.1"}')

state = {"sent": 0, "ok": 0, "last": "boot", "ms": 0, "wifi": "connecting", "q": 0}
queue = []            # JSON frame strings, filled by the sampler
lock = _thread.allocate_lock()


def poster():
    """Network thread: keep one TLS connection, POST whatever the sampler queued."""
    ss = None
    while True:
        time.sleep_ms(1000)
        with lock:
            batch = queue[:200]
            del queue[:len(batch)]
        if not batch:
            continue
        body = ("[" + ",".join(batch) + "]").encode()
        for attempt in (1, 2):
            try:
                if ss is None:
                    state["last"] = "tls..."
                    ai = socket.getaddrinfo(HOST, 443)[0][-1]
                    s = socket.socket()
                    s.settimeout(10)
                    s.connect(ai)
                    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                    ctx.verify_mode = ssl.CERT_NONE      # quick and dirty: encrypted, server not verified
                    ss = ctx.wrap_socket(s, server_hostname=HOST)
                t0 = time.ticks_ms()
                ss.write(b"POST " + PATH.encode() + b" HTTP/1.1\r\nHost: " + HOST.encode() +
                         b"\r\nContent-Type: application/json\r\nConnection: keep-alive\r\nContent-Length: " +
                         str(len(body)).encode() + b"\r\n\r\n")
                ss.write(body)
                head = b""
                while b"\r\n\r\n" not in head:
                    ch = ss.read(1)
                    if not ch:
                        raise OSError("closed")
                    head += ch
                status = head.split(b" ", 2)[1].decode()
                n = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        n = int(line.split(b":")[1])
                resp = ss.read(n) if n else b""
                state["ms"] = time.ticks_diff(time.ticks_ms(), t0)
                state["sent"] += len(batch)
                if status == "202":
                    state["ok"] += len(batch)
                state["last"] = status
                if status not in ("202", "503"):
                    print("POST", status, resp[:200])
                break
            except Exception as e:
                state["last"] = "err %s" % type(e).__name__
                try:
                    ss.close()
                except Exception:
                    pass
                ss = None
        gc.collect()


def wifi():
    w = network.WLAN(network.STA_IF)
    w.active(True)
    try:
        w.config(pm=w.PM_NONE)
    except Exception:
        pass
    if not w.isconnected():
        w.connect(nvs("ssid0"), nvs("pswd0"))
    t = time.ticks_ms()
    while not w.isconnected() and time.ticks_diff(time.ticks_ms(), t) < 20000:
        draw()
        time.sleep_ms(200)
    state["wifi"] = "ok" if w.isconnected() else "no wifi"
    return w


def draw():
    cv.fillScreen(0x000000)
    cv.setTextColor(0xFF7A1A, 0x000000)
    cv.setFont(M5.Lcd.FONTS.Montserrat14)
    cv.drawString("HEATGUARD", 6, 6)
    cv.setFont(M5.Lcd.FONTS.Montserrat12)
    cv.setTextColor(0xFFFFFF, 0x000000)
    rows = [("device", dev_id[-6:]), ("wifi", state["wifi"]), ("http", state["last"]),
            ("sent", str(state["sent"])), ("stored", str(state["ok"])), ("rtt", "%d ms" % state["ms"]),
            ("queue", str(state["q"]))]
    y = 34
    for k, v in rows:
        cv.setTextColor(0x7A7A7A, 0x000000)
        cv.drawString(k, 6, y)
        cv.setTextColor(0x22C55E if v in ("202", "ok") else 0xFFFFFF, 0x000000)
        cv.drawRightString(v, 129, y)
        y += 22
    cv.setTextColor(0x7A7A7A, 0x000000)
    cv.drawCenterString("-> cloud telemetry", 67, 222)
    cv.push(0, 0)


def main():
    w = wifi()
    _thread.stack_size(16 * 1024)
    _thread.start_new_thread(poster, ())
    seq, t_us, last = 0, 0, time.ticks_us()
    next_ms = time.ticks_ms()
    last_temp = last_cfg = last_draw = time.ticks_ms()
    first = True
    while True:
        now = time.ticks_ms()
        if time.ticks_diff(now, next_ms) < 0:
            time.sleep_ms(1)
            continue
        next_ms = time.ticks_add(next_ms, DT_MS)
        if time.ticks_diff(now, next_ms) > 200:
            next_ms = time.ticks_add(now, DT_MS)
        us = time.ticks_us()
        t_us += time.ticks_diff(us, last)      # monotonic, never wraps
        last = us
        ax, ay, az = M5.Imu.getAccel()
        gx, gy, gz = M5.Imu.getGyro()
        temp = time.ticks_diff(now, last_temp) >= 1000
        f = ('{"schema":"sticks3.telemetry.v1","device_id":"%s","boot_id":"%s","sequence":%d,'
             '"read_time_us":%d,"frame":"%s","fresh":{"accelerometer":true,"gyroscope":true,"temperature":%s},'
             '"imu":{"acceleration_g":{"x":%.4f,"y":%.4f,"z":%.4f},"angular_velocity_dps":{"x":%.2f,"y":%.2f,"z":%.2f}%s}%s}'
             % (dev_id, boot_id, seq, t_us, FRAME, "true" if temp else "false", ax, ay, az, gx, gy, gz,
                (',"die_temperature_c":%.1f' % float(esp32.mcu_temperature())) if temp else "",
                (',"configuration":' + CONFIG) if (first or time.ticks_diff(now, last_cfg) >= 60000) else ""))
        if temp:
            last_temp = now
        if first or time.ticks_diff(now, last_cfg) >= 60000:
            last_cfg, first = now, False
        seq += 1
        with lock:
            queue.append(f)
            if len(queue) > 1500:          # ~30 s backlog max: drop oldest
                del queue[:len(queue) - 1500]
            state["q"] = len(queue)
        if time.ticks_diff(now, last_draw) >= 500:
            last_draw = now
            state["wifi"] = "ok" if w.isconnected() else "lost"
            draw()


main()
