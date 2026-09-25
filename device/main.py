"""HeatGuard wearable firmware -- M5Stack StickS3 (UiFlow2 MicroPython).

Everything safety-critical runs here, on the wrist, with no network needed:

  * fall detection     free-fall -> impact -> stillness, or a hard impact -> stillness
  * unusual movement   rhythmic 3-12 Hz shaking (tremor / seizure-like) and
                       sustained non-rhythmic flailing (erratic)
  * inactivity check   no movement for INACT_S seconds during work
  * SOS                hold BtnA for 2 s
  * heat work/rest     the server pushes the WBGT-based plan; if the link drops the
                       device keeps timing the work cycle itself
  * voice assistant    hold BtnB and speak; the reply is played back (needs WiFi)

Every alarm first asks the worker "are you OK?" (vibrate + beep). BtnA answers
yes; no answer escalates.

Transport. With WiFi credentials in NVS (namespace 'uiflow', keys ssid0/pswd0)
the device joins the network and a network thread holds one secure WebSocket
to the backend on Cloud Run (NVS 'hg_url', default below; token 'hg_token').
It carries sticks3.telemetry.v1 frames (JSON arrays of 10), the HeatGuard
hello / st / ev messages, the server's commands and push-to-talk voice (binary
messages). If the backend refuses the upgrade, the same TLS connection POSTs
the frames to /v1/ingest/frames once a second and the WebSocket is tried again
every 5 minutes. Incidents (fall, SOS, tremor, inactivity, heat, ...) are also
POSTed to /v1/incidents, queued and retried until the server confirms them.
The 50 Hz loop never waits on the network; it only fills bounded queues. With
no WebSocket up, hello / st / ev go out as '@' lines on USB serial. Events carry
a seq and are resent every second until the server acks them. See
docs/heatguard/device-ws.md and docs/heatguard/incidents.md.

BtnA (big front) = answer / dismiss, hold = SOS
BtnB (side)      = switch page, hold = talk to the assistant
"""
import binascii
import gc
import json
import math
import os
import socket
import sys
import time
import select
import _thread
from array import array
from collections import deque

import esp32
import machine
import M5

try:
    import uctypes
except ImportError:
    uctypes = None
try:
    import tls                  # has set_ciphers(); the ssl wrapper does not
except ImportError:
    import ssl as tls
try:
    import hashlib
except ImportError:
    hashlib = None

FW = "heatguard-0.5"

# ---------------------------------------------------------------- tunables
# Reported to the server in every hello so incident analysis knows what fired.
P = {
    "FS_HZ": 50,
    "FF_G": 0.5,            # free-fall when |a| drops below this
    "FF_MIN_MS": 80,        # ...for at least this long
    "IMPACT_G": 1.8,        # impact threshold after free-fall
    "IMPACT_ONLY_G": 3.0,   # hard impact with no free-fall
    "IMPACT_WIN_MS": 1000,  # impact must follow free-fall within this
    "SETTLE_MS": 800,       # ignore the ringing right after impact
    "STILL_WIN_MS": 1200,   # then look for stillness for this long
    "STILL_STD_G": 0.12,
    "STILL_GYRO_DPS": 30,
    "TREM_MIN_HZ": 3.0,
    "TREM_MAX_HZ": 12.0,
    "TREM_MIN_DPS": 40,     # gyro RMS on the dominant axis
    "TREM_MAX_CV": 0.45,    # crossing-interval variation; lower = more rhythmic
    "TREM_SUSTAIN_S": 4,
    "ERRATIC_DPS": 160,
    "ERRATIC_SUSTAIN_S": 5,
    "INACT_S": 300,
    "PROMPT_S": 15,         # how long the worker has to answer "are you OK?"
    "SOS_WAIT_S": 15,       # SOS hold: time to cancel before the call goes out
}

# ---------------------------------------------------------------- heat tiers
# On-device heat tiers from the ESP32 die temperature (air ~ die - 35 C). The
# smoothed die temperature must stay at or above a threshold this long to raise
# a tier, and fall below its clear level to end it. The backend runs the same
# values (HEATGUARD_CHIP_STOP / HEATGUARD_CHIP_CALL) and can change them with
# {"cmd":"cfg","chip_stop":..,"chip_call":..}.
HEAT_OFFSET_C = 35.0        # air ~ die - this
HEAT_EMA = 0.3              # smoothing per 1 s sample
HEAT_SUSTAIN_S = 8
HEAT_STOP_C = 64.0          # stop work (incident heat_stroke, warning) ...
HEAT_STOP_CLEAR_C = 62.0    # ... until below this
HEAT_CALL_C = 66.0          # heat stroke: "are you OK?" prompt (heat_stroke, critical) ...
HEAT_CALL_CLEAR_C = 63.0    # ... until below this
P.update({"CHIP_STOP": HEAT_STOP_C, "CHIP_STOP_CLEAR": HEAT_STOP_CLEAR_C,
          "CHIP_CALL": HEAT_CALL_C, "CHIP_CALL_CLEAR": HEAT_CALL_CLEAR_C})

DT_MS = 1000 // P["FS_HZ"]
WIN = 100                   # 2 s analysis window
HOP = 25                    # re-analyse every 0.5 s
TALK_HOLD_MS = 400          # BtnB held this long = push-to-talk
# UiFlow sets gc.threshold(20480); analyse_window() alone allocates ~22 KB of
# floats, so a 10-20 ms collection landed inside it (and on top of a draw)
# every 0.5 s. Instead the loop collects on an otherwise idle iteration, with
# a larger threshold only as a backstop.
GC_EVERY_MS = 500
GC_THRESHOLD = 262144

W, H = 135, 240
BLACK, WHITE = 0x000000, 0xFFFFFF
DIM, FAINT = 0x7A7A7A, 0x2A2A2A
GREEN, AMBER, RED = 0x22C55E, 0xF59E0B, 0xEF4444
CYAN, BLUE, PURPLE = 0x22D3EE, 0x3B82F6, 0xA855F7
HEAT_COLORS = (GREEN, 0xEAB308, 0xF97316, RED, 0xB91C1C)

# ---------------------------------------------------------------- network
WIFI_RETRY_MS = 15000
WIFI_DOWN_GRACE_MS = 3000   # iPhone hotspots drop a station for a moment; ride it out
EV_RESEND_MS = 1000
EV_TRIES = 30
EV_MAX = 20
# LAN transport of heatguard-0.4 (UDP gateway, LAN beacon, TCP voice), no longer used:
# UDP_LOCAL = 47801           # beacon + commands in, telemetry out (one socket)
# SRV_UDP = 47800
# SRV_VOICE = 47802
# SRV_FORGET_MS = 60000       # a beacon-found server is dropped after this much silence
# WOULD_BLOCK = (errno.EAGAIN, errno.EINPROGRESS)
# ENOTCONN = getattr(errno, "ENOTCONN", 128)

# ---------------------------------------------------------------- cloud link
HG_URL = "wss://telemetry-backend-501582454609.asia-northeast1.run.app/v1/device/ws"
INGEST_PATH = "/v1/ingest/frames"
INCIDENT_PATH = "/v1/incidents"
NET_TIMEOUT_S = 10          # DNS, TCP, TLS and each HTTP response
POST_EVERY_MS = 1000        # one POST of ~50 frames a second
POST_MAX = 10               # batches per POST when catching up (100 frames)
INC_RETRY_MS = 30000        # incidents the server did not take are retried this often
INC_MAX = 20
BACKOFF_MAX_S = 30          # reconnect backoff 1, 2, 4 ... 30 s
NET_STACK = 16384           # network thread stack (mbedtls handshake)
RXB = 2048                  # HTTP response buffer (second connection for incidents)
RXB_WS = 6144               # main connection read buffer (voice frames are 4801 bytes)
TXB = 6144                  # WebSocket send buffer
WR_SLICE = 2048             # bytes per socket write (see w_all)
WS_RETRY_MS = 300000        # HTTPS fallback: try the WebSocket again this often ...
WS_RETRY_5XX_MS = 30000     # ... or this soon after a server error
WS_PING_MS = 25000          # ping the server after this much silence ...
WS_DEAD_MS = 60000          # ... and drop the link after this much (it pings every 20 s)
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CTL_Q = 32                  # hello / st / ev waiting for the socket
TXV_MAX = 8                 # voice frames waiting for the socket (0.8 s)
VIN_MAX = 6                 # reply frames waiting for the voice state machine
# TLS: the chain (Google Trust Services WR2 -> GTS Root R1) is not verified yet.
# TODO(security): pin the CA: put GTS Root R1 (DER) in TLS_CA and verification is on.
TLS_CA = None
# RSA key exchange keeps the handshake off the IMU loop: mbedtls holds the GIL for
# its math, measured on this stick at ~480 ms for ECDHE-ECDSA (the default), ~260 ms
# for ECDHE-RSA and ~70 ms for RSA. After a failed handshake the next attempt uses
# the library defaults, in case a server refuses it.
TLS_CIPHERS = ["TLS-RSA-WITH-AES-128-GCM-SHA256"]
L_OFF, L_CONN, L_TLS, L_WS, L_POST, L_WAIT = 0, 1, 2, 3, 4, 5

# ---------------------------------------------------------------- telemetry contract
FRAME = "display_portrait_usb_down_rh"    # axis frame of M5.Imu on this stick
TEL_BATCH = 10              # frames per batch (200 ms)
TEL_Q = 20                  # batches held for the link (4 s); the oldest go first
CFG_EVERY_MS = 60000

# ---------------------------------------------------------------- state
cv = None
FONTS = None
T0 = time.ticks_ms()
dev_id = "sticks3"
boot_id = "00000000"
t_mono = 0                  # read_time_us: us since boot, summed from ticks_diff so it never wraps
f_seq = 0                   # frame sequence, per boot
F_P = F_T = CONF = ""       # frame templates (frames_init)
gap_s = 0                   # worst main-loop gap in the current second (ms)

link_ms = -100000           # last time the host talked to us (any transport)
plan = {"wbgt": None, "work": 45, "rest": 15, "elapsed": 0.0, "phase": "work",
        "remaining": 0.0, "level": 0, "label": "--", "warp": 1, "reason": ""}
rest_end_ms = 0
page = 0

ui = "normal"               # normal | prompt | escalated | sos_wait | sos | ack | msg | banner
ALARM_UI = ("prompt", "escalated", "sos_wait", "sos")    # screens nothing else may replace
ui_kind = ""
ui_until = 0
ui_text = ""
ui_color = RED
alert_n = 0                 # alarms sent while the current escalated / SOS screen is up

buzz_q = []                 # [(on_ms, off_ms), ...]
buzz_state = 0
buzz_at = 0

# motion features
am = 1.0
gm = 0.0
act = 0.0
still_ms = 0
trem_hz = 0.0
trem_amp = 0.0
trem_run = 0
err_run = 0
trem_cool = 0
spark = array("f", [1.0] * 64)
spark_i = 0

# fall state machine
fs = 0
ff_ms = 0
ff_start = 0
t_imp = 0
imp_g = 0.0
had_ff = False
st_n = 0
st_s = 0.0
st_s2 = 0.0
st_gs = 0.0

gxb = array("f", [0.0] * WIN)
gyb = array("f", [0.0] * WIN)
gzb = array("f", [0.0] * WIN)
wi = 0
hop_n = 0

bat = -1
chg = 0
chip_t = 0.0
heat_ema = None             # smoothed die temperature
heat_lvl = 0                # 0 normal, 1 stop work, 2 extreme heat
heat_on = [False, False, False]
heat_run = [0, 0, 0]        # seconds the smoothed temperature has been over each tier

holdA = 0
holdB = 0
firedA = False
firedB = False

# wifi
wlan = None                 # None = no credentials, WiFi stays off
wifi_ssid = ""
wifi_key = ""
wifi_up = False
wifi_ip = ""
wifi_try = 0
wifi_pm0 = None             # radio power-save mode at boot, restored after voice
rssi = None
wifi_down_at = 0            # when isconnected() first went false
# LAN transport state (heatguard-0.4), no longer used:
# udp = None
# srv_host = ""               # fixed server from NVS hg_server, if any
# srv_port = SRV_UDP
# srv_fixed = False
# srv_ip = None
# srv_addr = None             # (ip, port) telemetry goes to; None = serial
# srv_voice = SRV_VOICE
# udp_ms = -100000            # last datagram from the server

# events awaiting an evack
ev_seq = 0
ev_pend = []                # [seq, line, due_ms, tries]


# ---------------------------------------------------------------- output
ser_off = 0                 # USB serial output paused until this (ticks_ms)


def ser(s):
    """A line on USB serial. If a write stalls (no host draining the port) serial
    output pauses for 30 s so it can never slow the IMU loop."""
    global ser_off
    t = time.ticks_ms()
    if time.ticks_diff(ser_off, t) > 0:
        return
    try:
        sys.stdout.write(s)
        sys.stdout.write("\n")
    except Exception:
        pass
    if time.ticks_diff(time.ticks_ms(), t) > 20:
        ser_off = time.ticks_add(time.ticks_ms(), 30000)


def out(s):
    """One HeatGuard JSON message (hello / st / ev): over the WebSocket when it is
    up, otherwise a '@' line on USB serial (harmless when nobody listens)."""
    if link == L_WS:
        if len(tx_ctl) < CTL_Q:
            tx_ctl.append(s)
        return
    ser("@" + s)


# UDP transport (heatguard-0.4): JSON datagrams to the LAN gateway; the gateway is gone.
# def out(s):
#     """One JSON message to the server: UDP when WiFi is up and the server is
#     known, otherwise a '@' line on USB serial. Never both."""
#     if udp is not None and srv_addr is not None:
#         try:
#             udp.sendto(s, srv_addr)
#         except Exception:
#             pass                # ENOMEM bursts etc.: drop, telemetry is lossy
#         return
#     try:
#         sys.stdout.write("@" + s + "\n")
#     except Exception:
#         pass


def jstr(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def ev(kind, **detail):
    """A device event. 't' is read_time_us // 1000, the clock of the telemetry frames."""
    global ev_seq
    ev_seq += 1
    parts = ",".join(jstr(k) + ":" + (jstr(v) if isinstance(v, str) else
                                      ("%.3f" % v if isinstance(v, float) else str(v)))
                     for k, v in detail.items())
    now = time.ticks_ms()
    line = ('{"k":"ev","id":%s,"seq":%d,"type":%s,"t":%d,"detail":{%s}}'
            % (jstr(dev_id), ev_seq, jstr(kind), t_mono // 1000, parts))
    inc_event(kind, ev_seq, parts)     # queue the incident POST first
    out(line)
    ev_pend.append([ev_seq, line, time.ticks_add(now, EV_RESEND_MS), 1])
    if len(ev_pend) > EV_MAX:
        ev_pend.pop(0)


def ev_tick(now):
    """Resend unacked events once a second; give up after EV_TRIES sends."""
    i = 0
    while i < len(ev_pend):
        p = ev_pend[i]
        if time.ticks_diff(now, p[2]) >= 0:
            if p[3] >= EV_TRIES:
                ev_pend.pop(i)
                continue
            out(p[1])
            p[3] += 1
            p[2] = time.ticks_add(now, EV_RESEND_MS)
        i += 1


def ev_ack(seq):
    for i in range(len(ev_pend)):
        if ev_pend[i][0] == seq:
            ev_pend.pop(i)
            return


def seq_base():
    """Start each boot's seq above the last boot's, so a server that
    de-duplicates on seq never mistakes a new event for an old one."""
    try:
        nv = esp32.NVS("heatguard")
        try:
            n = nv.get_i32("boots")
        except OSError:
            n = 0
        n = (n + 1) % 10000
        nv.set_i32("boots", n)
        nv.commit()
        return n * 100000
    except Exception:
        return 0


def hello_msg():
    params = ",".join('"%s":%s' % (k, jstr(v) if isinstance(v, str) else v) for k, v in P.items())
    return ('{"k":"hello","id":%s,"fw":%s,"ip":%s,"params":{%s}}'
            % (jstr(dev_id), jstr(FW), jstr(wifi_ip) if wifi_ip else "null", params))


def hello():
    out(hello_msg())


# ---------------------------------------------------------------- incidents
# Anything that happened to the worker is also POSTed to /v1/incidents
# (heatguard.incident.v1) by the network thread, retried until the server takes
# it. incident_id = <device_id>-<boot_id>-<event seq>; the answer to the
# "are you OK?" prompt re-POSTs the same id with a new status.
INC_TYPE = {"fall": "fall", "sos": "manual_sos", "tremor": "tremor", "erratic": "tremor",
            "inactivity": "inactivity", "unwell": "unwell", "impact": "impact",
            "heat_stroke": "heat_stroke"}
INC_SEV = {"heat_stroke": "critical"}
INC_STATUS = {"ok": "worker_ok", "noresp": "no_response", "cancel": "cancelled"}
HEAT_KINDS = {"heat_stop": (1, "warning")}      # the heat_stroke tier is a prompt (INC_TYPE)
tx_inc = []                 # [incident_id, JSON body], shared with the network thread
inc_cur = None              # [id, type, read_time_us, details, event, severity, status] of the alarm on screen
heat_rec = [None, None, None]   # open heat_stroke incident per heat tier
inc_at = 0                  # next POST attempt (ticks_ms); the network thread reads it
hg_person = ""              # ',"person":{...}' from NVS hg_worker, else the backend fills it


def inc_event(kind, seq, parts):
    global inc_cur
    try:
        iid = "%s-%s-%d" % (dev_id, boot_id, seq)
        det = '"event":%s%s%s' % (jstr(kind), "," if parts else "", parts)
        if kind in INC_TYPE:
            rec = [iid, INC_TYPE[kind], t_mono, det, kind, INC_SEV.get(kind, ""), ""]
            if kind == "heat_stroke":
                heat_rec[2] = rec
            # the alarm that owns the screen gets the worker's answer
            if kind == "sos" or (kind != "impact" and ui not in ALARM_UI):
                inc_cur = rec
            inc_put(rec, "suspected")
        elif kind in HEAT_KINDS:
            lvl, sev = HEAT_KINDS[kind]
            heat_rec[lvl] = [iid, "heat_stroke", t_mono, det, kind, sev, ""]
            inc_put(heat_rec[lvl], "suspected")
        elif kind.endswith("_clear") and kind[:-6] in HEAT_KINDS:
            lvl = HEAT_KINDS[kind[:-6]][0]
            if heat_rec[lvl] is not None:
                inc_put(heat_rec[lvl], "resolved")
                heat_rec[lvl] = None
        elif kind == "heat_stroke_clear":
            # cooler again: close it only if the worker said OK; an unanswered
            # one stays with the supervisor
            r = heat_rec[2]
            if r is not None and r[6] == "worker_ok":
                inc_put(r, "resolved")
            heat_rec[2] = None
        elif "_" in kind and inc_cur is not None:
            base, what = kind.rsplit("_", 1)
            st = INC_STATUS.get(what)
            if st and base == inc_cur[4]:
                inc_put(inc_cur, st)
                if what != "noresp":
                    inc_cur = None
    except Exception:
        pass                    # never let bookkeeping break an alarm


def inc_put(rec, status):
    """Queue (or update in place) one incident for the network thread."""
    global inc_at
    rec[6] = status
    body = ('{"schema":"heatguard.incident.v1","incident_id":"%s","device_id":"%s","type":"%s",'
            '"status":"%s",%s"boot_id":"%s","read_time_us":%d,"details":{%s},"source":"device"%s}'
            % (rec[0], dev_id, rec[1], status, ('"severity":"%s",' % rec[5]) if rec[5] else "",
               boot_id, rec[2], rec[3], hg_person))
    for i in range(len(tx_inc)):
        if tx_inc[i][0] == rec[0]:
            tx_inc[i] = [rec[0], body]      # a new object: the thread sees it changed
            break
    else:
        if len(tx_inc) >= INC_MAX:
            tx_inc.pop(0)
        tx_inc.append([rec[0], body])
    inc_at = time.ticks_ms()                # post now


# ---------------------------------------------------------------- serial in
_poll = select.poll()
_poll.register(sys.stdin, select.POLLIN)
_line = []


def read_host():
    global link_ms
    n = 0
    while n < 512 and _poll.poll(0):
        ch = sys.stdin.read(1)
        n += 1
        if ch == "\n" or ch == "\r":
            if _line:
                s = "".join(_line)
                _line.clear()
                try:
                    handle(json.loads(s))
                    link_ms = time.ticks_ms()
                except Exception:
                    pass
        elif len(_line) < 400:
            _line.append(ch)


def handle(c):
    global rest_end_ms, ui, ui_until, ui_text, ui_color, ui_kind, v_msg
    cmd = c.get("cmd")
    now = time.ticks_ms()
    if cmd == "plan":
        prev = plan["phase"]
        for k in plan:
            if k in c:
                plan[k] = c[k]
        if plan["phase"] == "rest":
            rest_end_ms = time.ticks_add(now, int(plan["remaining"] * 60000 / max(1, plan["warp"])))
        if prev == "work" and plan["phase"] in ("rest", "stop"):
            start_rest_banner(plan.get("reason") or "Heat limit reached")
        elif prev in ("rest", "stop") and plan["phase"] == "work":
            banner("BACK TO WORK", "Cool-down done", GREEN, 4000)
            buzz(1)
    elif cmd == "evack":
        ev_ack(c.get("seq"))
    elif cmd == "rest":
        mins = float(c.get("mins", 15))
        plan["phase"] = "rest"
        plan["remaining"] = mins
        plan["reason"] = c.get("reason", "Supervisor break")
        rest_end_ms = time.ticks_add(now, int(mins * 60000 / max(1, plan["warp"])))
        start_rest_banner(plan["reason"])
    elif cmd == "resume":
        plan["phase"] = "work"
        plan["elapsed"] = 0.0
        banner("BACK TO WORK", "Resume", GREEN, 4000)
        buzz(1)
    elif cmd == "buzz":
        buzz(3)
        beep(2400, 150)
    elif cmd == "msg":
        if ui not in ALARM_UI:
            ui, ui_kind, ui_text, ui_color = "msg", "msg", str(c.get("text", ""))[:140], WHITE
            ui_until = time.ticks_add(now, 30000)
        buzz(2)
        beep(1800, 120)
    elif cmd == "say":
        text = str(c.get("text", ""))[:140]
        if v_state:
            v_msg = text
        elif ui not in ALARM_UI:
            ui, ui_kind, ui_text, ui_color = "msg", "say", text, WHITE
            ui_until = time.ticks_add(now, 30000)
            buzz(1)
    elif cmd == "clear":
        set_ui("normal")
    elif cmd == "ack":
        ui, ui_kind = "ack", str(c.get("type", ""))
        ui_text = str(c.get("text", "Supervisor is on the way"))
        ui_until = time.ticks_add(now, 30000)
        buzz(1)
    elif cmd == "cfg":
        for k, v in c.items():
            if k in P:
                P[k] = v
        if "inact_s" in c:
            P["INACT_S"] = c["inact_s"]
        heat_cfg(c)
        hello()
    elif cmd == "hello":
        hello()
    elif cmd == "error":
        net_note("server error %s: %s" % (c.get("what", ""), str(c.get("detail", ""))[:120]))
    elif cmd == "slow":
        pass                    # the network thread holds telemetry back itself


# ---------------------------------------------------------------- wifi
def nvs_str(key):
    try:
        v = esp32.NVS("uiflow").get_str(key)
    except Exception:
        return ""
    if isinstance(v, bytes):
        v = v.decode()
    return v if isinstance(v, str) else ""


def wifi_init():
    """Read credentials and start the radio. Never blocks on the connection
    itself; wifi_tick() polls it from the main loop."""
    global wlan, wifi_ssid, wifi_key, wifi_try, wifi_pm0
    # Fixed LAN gateway from NVS hg_server (heatguard-0.4), no longer used:
    # s = nvs_str("hg_server").strip()
    # if s:
    #     host, port = (s.split(":", 1) + [""])[:2]
    #     try:
    #         srv_port = int(port) if port else SRV_UDP
    #         srv_host, srv_fixed = host, True
    #     except ValueError:
    #         pass
    wifi_ssid = nvs_str("ssid0")
    if not wifi_ssid:
        return
    wifi_key = nvs_str("pswd0")
    try:
        import network
        try:
            network.hostname("heatguard-" + dev_id[-4:])
        except Exception:
            pass
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
    except Exception:
        wlan = None
        return
    # Power save stays off: on phone hotspots modem sleep makes the ESP32 miss
    # beacons and get disassociated every few seconds. Costs some battery.
    try:
        wlan.config(pm=wlan.PM_NONE)
        wifi_pm0 = wlan.PM_NONE
    except Exception:
        pass
    wifi_try = time.ticks_add(time.ticks_ms(), -WIFI_RETRY_MS)     # connect on the first tick


def wifi_fast(on):
    """Station power save only wakes the radio about once per beacon interval
    (~100 ms) to fetch buffered frames. Keep it off while voice streams and put
    the original mode back afterwards for the battery."""
    if wlan is None:
        return
    try:
        wlan.config(pm=wlan.PM_NONE if on else (wifi_pm0 if wifi_pm0 is not None else wlan.PM_PERFORMANCE))
    except Exception:
        pass


# UDP transport (heatguard-0.4): one socket for telemetry out and commands in.
# def udp_open():
#     global udp
#     udp_close()
#     s = None
#     try:
#         s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
#         try:
#             s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
#             s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)   # lwIP needs it to receive broadcasts
#         except Exception:
#             pass
#         s.bind(("0.0.0.0", UDP_LOCAL))
#         s.setblocking(False)
#         udp = s
#     except Exception:
#         if s is not None:
#             try:
#                 s.close()
#             except Exception:
#                 pass
#         udp = None
#
#
# def udp_close():
#     global udp
#     if udp is not None:
#         try:
#             udp.close()
#         except Exception:
#             pass
#     udp = None


def wifi_tick(now):
    global wifi_up, wifi_ip, wifi_try, rssi, wifi_down_at
    if wlan is None:
        return
    try:
        up = wlan.isconnected()
    except Exception:
        up = False
    if up:
        wifi_down_at = 0
    elif wifi_up:
        if not wifi_down_at:
            wifi_down_at = now or 1
        if time.ticks_diff(now, wifi_down_at) < WIFI_DOWN_GRACE_MS:
            up = True           # short blip: keep the link
    if up and not wifi_up:
        wifi_up = True          # the network thread starts connecting
        try:
            wifi_ip = wlan.ifconfig()[0]
        except Exception:
            wifi_ip = ""
        wifi_try = now
        # LAN gateway (heatguard-0.4): udp_open(), resolve hg_server, hello() by UDP.
    elif not up and wifi_up:
        wifi_up, wifi_ip, rssi = False, "", None
        if V_IDLE < v_state < V_DONE:
            voice_fail("WiFi lost")
        wifi_try = now
    if not up and time.ticks_diff(now, wifi_try) >= WIFI_RETRY_MS:
        wifi_try = now
        try:
            wlan.disconnect()
        except Exception:
            pass
        try:
            wlan.connect(wifi_ssid, wifi_key)
        except Exception:
            pass


# Beacon discovery (heatguard-0.4): the LAN gateway broadcast {"hg":1,...} on UDP;
# commands came back as datagrams. Replaced by the cloud link below.
# def udp_rx(now):
#     """Drain the UDP socket (bounded): server beacons and commands."""
#     global srv_ip, srv_addr, srv_voice, udp_ms, link_ms
#     if udp is None:
#         return
#     for _ in range(8):
#         try:
#             data, addr = udp.recvfrom(640)
#         except Exception:
#             return
#         try:
#             s = data.decode().strip()
#             if s.startswith("@"):
#                 s = s[1:]
#             c = json.loads(s)
#             ip = addr[0]
#         except Exception:
#             continue
#         if not isinstance(c, dict):
#             continue
#         if "hg" in c:
#             new = False
#             if srv_fixed:
#                 if ip != srv_ip:
#                     continue
#             else:
#                 try:
#                     port = int(c.get("udp") or SRV_UDP)
#                 except Exception:
#                     port = SRV_UDP
#                 if srv_addr is None or srv_ip != ip or srv_addr[1] != port:
#                     srv_ip, srv_addr, new = ip, (ip, port), True
#             try:
#                 srv_voice = int(c.get("voice") or SRV_VOICE)
#             except Exception:
#                 srv_voice = SRV_VOICE
#             udp_ms = link_ms = now
#             if new:
#                 hello()
#         elif "cmd" in c:
#             if srv_addr is None and not srv_fixed:
#                 srv_ip, srv_addr = ip, (ip, SRV_UDP)
#             udp_ms = link_ms = now
#             try:
#                 handle(c)
#             except Exception:
#                 pass


# Link state for the LAN gateway (heatguard-0.4).
# def net_state(now):
#     """0 no wifi configured, 1 connecting, 2 no server, 3 linked over WiFi."""
#     if wlan is None:
#         return 0
#     if not wifi_up:
#         return 1
#     if srv_addr is None or time.ticks_diff(now, udp_ms) >= 8000:
#         return 2
#     return 3


# ---------------------------------------------------------------- cloud link
# A network thread owns the socket: DNS, TCP, TLS, the WebSocket handshake and
# every read and write. The 50 Hz loop never waits on it; the two sides share
# only the bounded queues below (list and deque operations are atomic under
# MicroPython's GIL). The thread must not run long stretches of Python: the IMU
# loop waits while it holds the GIL. Its socket calls release the GIL.
#
# Primary: one WebSocket (device-ws.md) for frames, hello/st/ev, commands and
# voice. If the upgrade is refused (route missing, 4xx/5xx) the same kept-alive
# connection POSTs the frames to /v1/ingest/frames instead and the WebSocket is
# tried again later. Incidents are always POSTed to /v1/incidents; while the
# WebSocket is up that takes a short second HTTPS connection.
tx_tel = deque((), TEL_Q)   # telemetry batches: 10 frames joined by ',' (no brackets)
tx_ctl = []                 # hello / st / ev JSON for the WebSocket
tx_v = []                   # (session, voice frame: type byte + payload) for the WebSocket
rx_cmd = []                 # server commands, parsed, for the main loop
v_in = []                   # reply voice frames for the running session
v_hs = 0                    # voice session whose 'H' went out last
v_es = 0                    # voice session whose 'E' went out last: its reply may come in
link = L_OFF                # what the thread is doing; the screen shows it
link_at = 0                 # L_WAIT: next attempt (ticks_ms)
ws_at = 0                   # next WebSocket attempt (ticks_ms)
net_stop = False            # ends the thread (tests)
net_err = ""
nlog = []                   # log lines from the thread; the main loop prints them
tel_hold = 0                # 'slow' / 503: no telemetry before this (ticks_ms)
tel_drop = 0                # frames dropped because the queue was full
tls_alt = False             # next handshake uses the library's default ciphers
http_st = 0                 # last HTTP status
u_tls = True
u_host = ""
u_port = 443
u_hosthdr = ""
u_ws = ""                   # WebSocket path with ?device_id=..&token=.. (never logged)
txb = None                  # WebSocket send buffer (thread)
txm = None
tx_off = 0
tx_len = 0
tx_cut = 0                  # end of the slice being written (see w_all)
tx_mark = None              # (session, type) of the voice frame in the send buffer
ws_pong = None              # ping payload to answer
ws_ping = False
ws_skip = 0                 # bytes of an oversized frame still to discard
ws_code = b""               # close code from the server
frag = None                 # fragments of a message in progress
frag_op = 0
NS = {"conn": 0, "post": 0, "post_ok": 0, "frames": 0, "rtt": 0, "rtt_max": 0, "hs": 0,
      "bytes": 0, "inc_ok": 0, "inc_try": 0, "e422": 0, "ws": 0, "ws_tel": 0, "ws_ctl": 0,
      "ws_vtx": 0, "ws_txb": 0, "ws_rx": 0, "ws_rxb": 0, "ping": 0, "vrx": 0, "vdrop": 0,
      "skip": 0, "slow": 0}


def net_note(msg):
    if len(nlog) < 12:
        nlog.append(msg)


def err_s(e):
    return "%s %s" % (type(e).__name__, e.args[0] if e.args else "")


def url_parse(u):
    """ws[s]:// or http[s]:// URL -> (tls, host, port, path)."""
    on = u.startswith("wss://") or u.startswith("https://")
    rest = u.split("://", 1)[1] if "://" in u else u
    i = rest.find("/")
    hp, path = (rest, "/") if i < 0 else (rest[:i], rest[i:])
    port = 443 if on else 80
    if ":" in hp:
        hp, p = hp.rsplit(":", 1)
        port = int(p)
    return on, hp, port, path


def qesc(s):
    """Percent-encode a query value."""
    o = []
    for c in s:
        if ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") or c in "-._~":
            o.append(c)
        else:
            for b in c.encode():
                o.append("%%%02X" % b)
    return "".join(o)


def link_init():
    """Backend URL from NVS hg_url (default: the Cloud Run service), device token
    from hg_token, the worker for incidents from hg_worker (a JSON object, or a
    worker id). The token only ever goes into the request line."""
    global u_tls, u_host, u_port, u_hosthdr, u_ws, hg_person
    try:
        u_tls, u_host, u_port, path = url_parse(nvs_str("hg_url").strip() or HG_URL)
    except Exception:
        u_tls, u_host, u_port, path = url_parse(HG_URL)
    u_hosthdr = u_host if u_port == (443 if u_tls else 80) else "%s:%d" % (u_host, u_port)
    q = "device_id=" + qesc(dev_id)
    tok = nvs_str("hg_token").strip()
    if tok:
        q += "&token=" + qesc(tok)
    u_ws = path + ("&" if "?" in path else "?") + q
    w = nvs_str("hg_worker").strip()
    if w:
        try:
            hg_person = ',"person":' + w if isinstance(json.loads(w), dict) else ""
        except Exception:
            hg_person = ""
        if not hg_person:
            hg_person = ',"person":{"worker_id":%s}' % jstr(w)


def net_start():
    try:
        _thread.stack_size(NET_STACK)
    except Exception:
        pass
    try:
        _thread.start_new_thread(net_thread, ())
    except Exception as e:
        net_note("no network thread: " + err_s(e))


def net_thread():
    global txb, txm
    txb = bytearray(TXB)
    txm = memoryview(txb)
    while not net_stop:
        try:
            net_loop()
        except Exception as e:  # never let the thread die
            net_note("net " + err_s(e))
            time.sleep_ms(1000)


def net_loop():
    global link, link_at, net_err
    back = 1
    while not net_stop:
        if not wifi_up:
            link = L_OFF
            time.sleep_ms(200)
            continue
        s = raw = None
        t0 = time.ticks_ms()
        try:
            s, raw = net_open(False)
            NS["conn"] += 1
            raw.setblocking(False)      # after the handshake; the TLS layer reads through it
            rd = Rd(s, RXB_WS)
            st = 0
            if time.ticks_diff(time.ticks_ms(), ws_at) >= 0:
                st, close = ws_try(s, rd)
                if st != 101 and close:
                    st = -1             # the server hung up: reconnect for HTTPS
            if st == 0 or (st > 0 and st != 101):
                st = post_run(s, rd)
            if st == 101:
                ws_run(s, rd)
        except Exception as e:
            net_err = err_s(e)
            net_note("link " + net_err)
        net_close(s, raw)
        if net_stop:
            break
        if time.ticks_diff(time.ticks_ms(), t0) > 10000:
            back = 1            # it worked for a while: start the backoff over
        link = L_WAIT
        link_at = time.ticks_add(time.ticks_ms(), back * 1000)
        while not net_stop and wifi_up and time.ticks_diff(link_at, time.ticks_ms()) > 0:
            time.sleep_ms(100)
        back = min(BACKOFF_MAX_S, back * 2)
    link = L_OFF


def net_open(side):
    """DNS, TCP and (https/wss) TLS, blocking with a timeout. Returns (stream, raw socket)."""
    global link, tls_alt
    if not side:
        link = L_CONN
    ai = socket.getaddrinfo(u_host, u_port, 0, socket.SOCK_STREAM)[0][-1]
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        raw.settimeout(NET_TIMEOUT_S)
        raw.connect(ai)
        try:
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        if not u_tls:
            return raw, raw
        if not side:
            link = L_TLS
        t = time.ticks_ms()
        ctx = tls.SSLContext(tls.PROTOCOL_TLS_CLIENT)
        if TLS_CA:
            ctx.load_verify_locations(cadata=TLS_CA)
            ctx.verify_mode = tls.CERT_REQUIRED
        else:
            ctx.verify_mode = tls.CERT_NONE
        if TLS_CIPHERS and not tls_alt:
            try:
                ctx.set_ciphers(TLS_CIPHERS)
            except Exception:
                pass
        try:
            s = ctx.wrap_socket(raw, server_hostname=u_host)    # SNI: Google's front end needs it
        except Exception:
            tls_alt = not tls_alt
            raise
        NS["hs"] = time.ticks_diff(time.ticks_ms(), t)
        return s, raw
    except Exception:
        try:
            raw.close()
        except Exception:
            pass
        raise


def net_close(s, raw):
    for x in (s, raw):
        if x is not None:
            try:
                x.close()
            except Exception:
                pass


class Rd:
    """Read buffer over a non-blocking socket. (A blocking readinto() waits until
    the whole buffer is full, so reads are non-blocking with a deadline.)"""

    def __init__(self, s, n):
        self.s = s
        self.b = bytearray(n)
        self.m = memoryview(self.b)
        self.r = 0
        self.n = 0
        self.dl = 0

    def arm(self):
        self.dl = time.ticks_add(time.ticks_ms(), NET_TIMEOUT_S * 1000)

    def wait(self):
        """Nothing to read or write yet: sleep (the GIL is free meanwhile) or time out."""
        if time.ticks_diff(time.ticks_ms(), self.dl) > 0:
            raise OSError("timeout")
        time.sleep_ms(5)

    def fill(self):
        """Read what is there. Returns the byte count, 0 = nothing yet; EOF raises."""
        if self.r == self.n:
            self.r = self.n = 0
        elif self.n == len(self.b):
            if not self.r:
                raise OSError("rx full")
            t = bytes(self.m[self.r:self.n])
            self.m[0:len(t)] = t
            self.r, self.n = 0, len(t)
        k = self.s.readinto(self.m[self.n:])
        if k is None:
            return 0
        if not k:
            raise OSError("closed")
        self.n += k
        return k

    def find(self, pat):
        return bytes(self.m[self.r:self.n]).find(pat)


def w_all(s, rd, b):
    """Write all of b on the non-blocking socket, WR_SLICE bytes per call (TLS
    encrypts under the GIL). After a would-block the same slice is offered
    again: mbedtls requires the retry to carry the same buffer and length."""
    m = memoryview(b)
    off = 0
    while off < len(m):
        cut = min(len(m), off + WR_SLICE)
        while off < cut:
            n = s.write(m[off:cut])
            if not n:
                rd.wait()
                continue
            off += n
        time.sleep_ms(0)        # let the IMU loop in between


def rd_line(rd):
    while True:
        i = rd.find(b"\r\n")
        if i >= 0:
            ln = bytes(rd.m[rd.r:rd.r + i])
            rd.r += i + 2
            return ln
        if not rd.fill():
            rd.wait()


def rd_skip(rd, n, got, keep):
    """Consume n bytes, keeping up to `keep` of them in `got`."""
    while n > 0:
        if rd.r == rd.n and not rd.fill():
            rd.wait()
            continue
        k = min(n, rd.n - rd.r)
        if keep > 0:
            j = min(k, keep)
            got.append(bytes(rd.m[rd.r:rd.r + j]))
            keep -= j
        rd.r += k
        n -= k
    return keep


def http_head(rd):
    """Status line and headers -> (status, {lower-case name: value}).
    Whatever follows the head stays in the buffer."""
    while True:
        i = rd.find(b"\r\n\r\n")
        if i >= 0:
            break
        if not rd.fill():
            rd.wait()
    head = bytes(rd.m[rd.r:rd.r + i]).decode()
    rd.r += i + 4
    lines = head.split("\r\n")
    st = int(lines[0].split(" ")[1])
    h = {}
    for ln in lines[1:]:
        j = ln.find(":")
        if j > 0:
            h[ln[:j].strip().lower()] = ln[j + 1:].strip()
    return st, h


def http_body(rd, st, h, keep=200):
    """Consume the body (Content-Length or chunked). Returns (first bytes, close):
    close is True when the connection cannot carry another request."""
    got = []
    close = h.get("connection", "").lower() == "close"
    if "chunked" in h.get("transfer-encoding", "").lower():
        while True:
            n = int(rd_line(rd).decode().split(";")[0].strip(), 16)
            if not n:
                while rd_line(rd):      # trailers up to the blank line
                    pass
                break
            keep = rd_skip(rd, n, got, keep)
            rd_line(rd)
    elif "content-length" in h:
        rd_skip(rd, int(h["content-length"]), got, keep)
    elif st not in (101, 204, 304):
        close = True                    # body runs to the end of the connection
    return b"".join(got), close


def http_post(s, rd, path, body):
    """One POST on the kept-alive connection -> (status, headers, body start, close)."""
    rd.arm()
    w_all(s, rd, ("POST %s HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n"
                  "Content-Length: %d\r\nConnection: keep-alive\r\nUser-Agent: %s\r\n\r\n"
                  % (path, u_hosthdr, len(body), FW)).encode())
    w_all(s, rd, body)
    st, h = http_head(rd)
    b, close = http_body(rd, st, h)
    return st, h, b, close


def retry_after(h):
    try:
        return max(500, min(30000, int(float(h.get("retry-after", "1")) * 1000)))
    except Exception:
        return 1000


# ---- HTTPS mode
def post_run(s, rd):
    """HTTPS mode: incidents first, then about once a second every queued frame;
    the WebSocket upgrade is retried on the same connection when due.
    Returns 101 when it switched to a WebSocket, else 0 (reconnect)."""
    global link
    link = L_POST
    t_post = time.ticks_add(time.ticks_ms(), -POST_EVERY_MS)
    while not net_stop and wifi_up:
        now = time.ticks_ms()
        if tx_inc and time.ticks_diff(now, inc_at) >= 0:
            if inc_send(s, rd):
                return 0
        if time.ticks_diff(now, ws_at) >= 0:
            st, close = ws_try(s, rd)
            if st == 101:
                return 101
            if close:
                return 0
        if (tx_tel and time.ticks_diff(now, t_post) >= POST_EVERY_MS
                and time.ticks_diff(now, tel_hold) >= 0):
            t_post = now
            if post_frames(s, rd):
                return 0
        time.sleep_ms(20)
    return 0


def post_frames(s, rd):
    """POST the queued batches as one JSON array. True = reconnect."""
    global tel_hold, http_st
    n = min(len(tx_tel), POST_MAX)
    parts = ["["]
    for _ in range(n):
        parts.append(tx_tel.popleft())
        parts.append(",")
    parts[-1] = "]"
    body = "".join(parts).encode()
    parts = None
    t = time.ticks_ms()
    st, h, b, close = http_post(s, rd, INGEST_PATH, body)
    ms = time.ticks_diff(time.ticks_ms(), t)
    http_st = st
    NS["post"] += 1
    NS["bytes"] += len(body)
    NS["rtt"] = ms
    if ms > NS["rtt_max"]:
        NS["rtt_max"] = ms
    if 200 <= st < 300:
        NS["post_ok"] += 1
        NS["frames"] += n * TEL_BATCH
    elif st == 422:
        if not NS["e422"]:
            net_note("frames 422: " + str(b[:160]))
        NS["e422"] += 1                 # the batch is dropped
    elif st == 503:
        tel_hold = time.ticks_add(time.ticks_ms(), retry_after(h))
    else:
        net_note("frames http %d" % st)
    return close


def inc_send(s, rd):
    """POST every queued incident. A 2xx (or a 422, which a retry cannot fix)
    removes it; anything else keeps it for INC_RETRY_MS. True = reconnect."""
    global inc_at, http_st
    while tx_inc and not net_stop:
        e = tx_inc[0]
        NS["inc_try"] += 1
        st, h, b, close = http_post(s, rd, INCIDENT_PATH, e[1].encode())
        http_st = st
        if 200 <= st < 300 or st == 422:
            if st == 422:
                net_note("incident 422: " + str(b[:160]))
            else:
                NS["inc_ok"] += 1
            if tx_inc and tx_inc[0] is e:   # not replaced by a newer status meanwhile
                tx_inc.pop(0)
        else:
            inc_at = time.ticks_add(time.ticks_ms(), retry_after(h) if st == 503 else INC_RETRY_MS)
            net_note("incident http %d, %d queued" % (st, len(tx_inc)))
            return close
        if close:
            return True
    return False


def inc_side():
    """Incidents while the WebSocket is up: a short second HTTPS connection."""
    global inc_at
    s2 = r2 = None
    try:
        s2, r2 = net_open(True)
        r2.setblocking(False)
        inc_send(s2, Rd(s2, RXB))
    except Exception as e:
        inc_at = time.ticks_add(time.ticks_ms(), INC_RETRY_MS)
        net_note("incident link " + err_s(e))
    net_close(s2, r2)


# ---- WebSocket mode
def ws_try(s, rd):
    """HTTP/1.1 Upgrade on this connection -> (status, close). Anything but 101
    leaves the connection to HTTPS POSTs and schedules the next try."""
    global ws_at, http_st
    key = binascii.b2a_base64(os.urandom(16)).strip()
    rd.arm()
    w_all(s, rd, ("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                  "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nUser-Agent: %s\r\n\r\n"
                  % (u_ws, u_hosthdr, key.decode(), FW)).encode())
    st, h = http_head(rd)
    http_st = st
    if st == 101:
        acc = h.get("sec-websocket-accept", "")
        if acc and hashlib is not None:
            if acc.encode() != binascii.b2a_base64(hashlib.sha1(key + WS_GUID).digest()).strip():
                raise OSError("ws accept")
        return 101, False
    b, close = http_body(rd, st, h)
    ws_at = time.ticks_add(time.ticks_ms(), WS_RETRY_5XX_MS if st >= 500 else WS_RETRY_MS)
    net_note("ws upgrade %d: https fallback" % st)
    return st, close


def ws_run(s, rd):
    """WebSocket mode: hello first, then everything on this socket until it ends."""
    global link, ws_pong, ws_ping, ws_skip, frag, tx_off, tx_len, tx_cut, tx_mark, v_hs, v_es
    tx_ctl.clear()              # hello goes first; old status is not worth sending
    ws_pong, ws_ping, ws_skip, frag, tx_mark = None, False, 0, None, None
    v_hs = v_es = 0
    tx_off = tx_len = tx_cut = 0
    ws_frame(1, hello_msg().encode())
    link = L_WS
    NS["ws"] += 1
    net_note("ws online")
    t_rx = t_ping = time.ticks_ms()
    while not net_stop and wifi_up:
        now = time.ticks_ms()
        busy = False
        if len(v_in) < VIN_MAX:             # else leave it in the socket: backpressure
            k = rd.fill()
            if k:
                busy = True
                t_rx = now
                NS["ws_rxb"] += k
            if rd.r < rd.n and not ws_parse(rd):
                ws_bye(s, ws_code)
                return
        for _ in range(4):
            if tx_off >= tx_len and not ws_next(now):
                break
            if not ws_pump(s):
                break
            busy = True
            if tx_mark is not None:
                if tx_mark[1] == 72:        # 'H' out: an 'X' may now come back
                    v_hs = tx_mark[0]
                elif tx_mark[1] == 69:      # 'E' out: the reply may now come
                    v_es = tx_mark[0]
                tx_mark = None
        if time.ticks_diff(now, t_rx) > WS_DEAD_MS:
            raise OSError("ws silent")
        if time.ticks_diff(now, t_rx) > WS_PING_MS and time.ticks_diff(now, t_ping) > WS_PING_MS:
            ws_ping = True
            t_ping = now
        if tx_inc and time.ticks_diff(now, inc_at) >= 0:
            inc_side()
        time.sleep_ms(0 if busy else 5)
    ws_bye(s, b"\x03\xe9")                  # 1001 going away


def ws_frame(op, a, b=None, c=None):
    """One message (a + b + c, bytes) into the send buffer: FIN, opcode, length and
    the mask bit with a zero key (RFC 6455 lets the key be anything; zero spares
    XOR-ing every audio byte). False if it does not fit."""
    global tx_off, tx_len, tx_cut
    n = len(a) + (len(b) if b is not None else 0) + (len(c) if c is not None else 0)
    if n > TXB - 8:
        return False
    o = 8
    for p in (a, b, c):
        if p is not None:
            txm[o:o + len(p)] = p
            o += len(p)
    if n < 126:
        tx_off = 2
        txb[2] = 0x80 | op
        txb[3] = 0x80 | n
    else:
        tx_off = 0
        txb[0] = 0x80 | op
        txb[1] = 0x80 | 126
        txb[2] = n >> 8
        txb[3] = n & 255
    txb[4] = txb[5] = txb[6] = txb[7] = 0
    tx_len = o
    tx_cut = tx_off
    return True


def ws_pump(s):
    """Write as much of the framed message as the socket takes. True when all of
    it is out. Same slice rule as w_all, and a yield after every slice so the
    IMU loop gets the GIL between encryptions."""
    global tx_off, tx_cut
    while tx_off < tx_len:
        if tx_cut <= tx_off:
            tx_cut = min(tx_len, tx_off + WR_SLICE)
        n = s.write(txm[tx_off:tx_cut])
        if not n:
            return False
        tx_off += n
        NS["ws_txb"] += n
        time.sleep_ms(0)
    return True


def ws_next(now):
    """Frame the next message: pong, ping, hello/st/ev, voice, telemetry."""
    global ws_pong, ws_ping, tx_mark
    tx_mark = None
    if ws_pong is not None:
        p = ws_pong
        ws_pong = None
        NS["ping"] += 1
        return ws_frame(10, p)
    if ws_ping:
        ws_ping = False
        return ws_frame(9, b"hg")
    if tx_ctl:
        NS["ws_ctl"] += 1
        return ws_frame(1, tx_ctl.pop(0).encode())
    if tx_v:
        sid, fr = tx_v.pop(0)
        tx_mark = (sid, fr[0])
        NS["ws_vtx"] += 1
        return ws_frame(2, fr)
    if tx_tel and time.ticks_diff(now, tel_hold) >= 0:
        NS["ws_tel"] += 1
        return ws_frame(1, b"[", tx_tel.popleft().encode(), b"]")
    return False


def ws_bye(s, code):
    """Best effort: finish the frame in flight, then a close frame."""
    try:
        for _ in range(20):
            if ws_pump(s):
                break
            time.sleep_ms(10)
        if ws_frame(8, code or b""):
            for _ in range(20):
                if ws_pump(s):
                    break
                time.sleep_ms(10)
    except Exception:
        pass


def ws_parse(rd):
    """Handle every complete frame in the read buffer. False when the server closed."""
    global ws_skip, ws_pong, ws_code, frag, frag_op
    b = rd.b
    while True:
        r, n = rd.r, rd.n
        if ws_skip:
            k = min(ws_skip, n - r)
            rd.r = r + k
            ws_skip -= k
            if ws_skip:
                return True
            continue
        a = n - r
        if a < 2:
            return True
        b0 = b[r]
        b1 = b[r + 1]
        ln = b1 & 0x7F
        h = 2
        if ln == 126:
            if a < 4:
                return True
            ln = (b[r + 2] << 8) | b[r + 3]
            h = 4
        elif ln == 127:
            if a < 10:
                return True
            ln = 0
            for i in range(2, 10):
                ln = (ln << 8) | b[r + i]
            h = 10
        if b1 & 0x80:
            raise OSError("masked frame")   # servers never mask
        op = b0 & 0x0F
        if ln > len(b) - 16:                # too big to hold: skip it
            rd.r = r + h
            ws_skip = ln
            NS["skip"] += 1
            if op < 8:
                frag = None
            continue
        if a < h + ln:
            return True
        p = rd.m[r + h:r + h + ln]
        rd.r = r + h + ln
        NS["ws_rx"] += 1
        if op >= 8:
            if op == 8:
                ws_code = bytes(p[:2])
                return False
            if op == 9:
                ws_pong = bytes(p)
            continue
        if op == 0:                         # continuation
            if frag is not None:
                frag.append(bytes(p))
                if b0 & 0x80:
                    m = b"".join(frag)
                    frag = None
                    if len(m) <= len(b):
                        ws_msg(frag_op, memoryview(m))
            continue
        if not b0 & 0x80:                   # first fragment
            frag = [bytes(p)]
            frag_op = op
            continue
        ws_msg(op, p)


def ws_msg(op, p):
    """A whole message: text = a server command, binary = a voice frame."""
    global tel_hold
    if op == 1:
        try:
            c = json.loads(bytes(p))
        except Exception:
            return
        if not isinstance(c, dict):
            return
        if c.get("cmd") == "slow":
            try:
                ms = int(c.get("retry_ms", 1000))
            except Exception:
                ms = 1000
            tel_hold = time.ticks_add(time.ticks_ms(), max(0, min(30000, ms)))
            NS["slow"] += 1
        if len(rx_cmd) < 16:
            rx_cmd.append(c)
    elif op == 2 and len(p):
        sid = v_sid
        t = p[0]
        # reply frames only for the running session and only after its 'E' went
        # out; anything else is the tail of a cancelled question
        if sid and len(v_in) < 2 * VIN_MAX and (
                (t == 88 and v_hs == sid) or (v_es == sid and (t == 65 or t == 84 or t == 69))):
            v_in.append(bytes(p))
            NS["vrx"] += 1
        else:
            NS["vdrop"] += 1


def net_rx():
    """Server commands that came in over the WebSocket (a few per iteration)."""
    for _ in range(4):
        if not rx_cmd:
            return
        c = rx_cmd.pop(0)
        try:
            handle(c)
        except Exception:
            pass


def net_state(now):
    """0 no wifi configured, 1 wifi connecting, 2 cloud not up yet,
    3 online over the WebSocket, 4 online over HTTPS (telemetry and incidents only)."""
    if wlan is None:
        return 0
    if not wifi_up:
        return 1
    if link == L_WS:
        return 3
    if link == L_POST:
        return 4
    return 2


def link_tip(now):
    """Footer text: the cloud link state."""
    if wlan is None:
        return "no wifi - local timer"
    if not wifi_up:
        return "wifi connecting..."
    if link == L_WS:
        return "online  hold B=talk"
    if link == L_POST:
        return "online (https)"
    if link == L_TLS:
        return "cloud TLS..."
    if link == L_WAIT:
        return "retrying in %ds" % max(1, (time.ticks_diff(link_at, now) + 999) // 1000)
    return "cloud connecting..."


def link_short(now):
    """Motion page: link state and the last HTTP result."""
    if link == L_POST:
        return "https %d %dms" % (http_st, NS["rtt"])
    if link == L_WS:
        return "ws online"
    if link == L_TLS:
        return "tls"
    if link == L_CONN:
        return "connect"
    if link == L_WAIT:
        return "retry %ds" % max(1, (time.ticks_diff(link_at, now) + 999) // 1000)
    return "off"


# ---------------------------------------------------------------- voice
# Push-to-talk, driven from the main loop as a state machine so the IMU keeps
# its 50 Hz. Voice frames are binary WebSocket messages (first byte = type: H, A,
# E up; T, A, E, X down); the network thread moves them and this code only
# touches the queues tx_v and v_in. Audio lives in a pool of 100 ms chunks; the
# mic, the queues and the speaker all work on chunk indices into that pool.
# Each chunk keeps a 2-byte slot in front of its samples: byte 1 holds the 'A',
# so a recorded chunk is one copy away from a voice frame, and the samples stay
# 2-byte aligned.
#
# The pool exists only while a session runs. The GC scans every live buffer
# and the heap grows to hold it: a permanent 192 KB pool made each collection
# ~45 ms instead of ~8 ms, and analyse_window() triggers one every call.
V_RATE = 24000
V_CS = 2400                 # samples per chunk (100 ms)
V_CE = V_CS + 1             # int16 elements per chunk incl. the slot
V_CB = V_CE * 2             # bytes per chunk
V_N = 12                    # pool: 1.2 s of audio
V_BLANK = 2400              # bytes blanked at the start of a take (mic start-up click)
V_TIMEOUT_MS = 30000        # whole session
V_HARD_MS = 45000           # ...but a reply that is already playing may run to here
V_GAP_MS = 150              # play a part-filled reply chunk after this much silence
MIC_GAIN = 4                # M5Unified default 16 clips on ambient noise
SPK_VOL = 255             # max: alarms must be heard on a noisy site
V_VOL = 255                 # speaker volume while a reply plays (max)
V_HF = b'H{"rate":24000,"fmt":"pcm16"}'
V_EF = b"E"

V_IDLE, V_LISTEN, V_THINK, V_SPEAK, V_DONE, V_ERR = 0, 1, 2, 3, 4, 5

aud = None                  # 'spk' | 'mic' | None -- they share the I2S pins
VA = None                   # pool chunks, array('h') each; None when idle
VH = None                   # int16 memoryview per chunk (mic / speaker)
VB = None                   # byte memoryview per chunk, same memory
V_NS = array("H", [0] * V_N)    # samples held by each reply chunk
V_ZERO = bytes(V_CB)        # template for new chunks, and the start-up blank
V_ZEROM = memoryview(V_ZERO)

v_state = V_IDLE
v_t0 = 0
v_until = 0
v_sid = 0                   # running session, 0 = none; only its reply frames get through
v_sids = 0
v_esent = False
v_first = False
v_skip = False
v_lvl = 0.0
v_msg = ""
v_free = []                 # chunk indices not in use
v_rec = []                  # queued on the mic, oldest first
v_tx = []                   # recorded, waiting for room in tx_v
v_play = []                 # received, waiting for the speaker
v_spk = []                  # queued on the speaker, oldest first
v_fill = -1                 # reply chunk being filled
v_fill_n = 0
v_in_off = 0                # bytes of v_in[0] already taken (pool was full)
v_rx_done = False
v_rx_any = False
v_last_rx = 0
v_playing = False


def audio(want):
    """Mic and speaker share I2S: end one before begin()-ing the other."""
    global aud
    if aud == want:
        return
    try:
        if aud == "spk":
            M5.Speaker.end()
        elif aud == "mic":
            M5.Mic.end()
    except Exception:
        pass
    try:
        if want == "spk":
            M5.Speaker.begin()
            M5.Speaker.setVolume(SPK_VOL)
        elif want == "mic":
            M5.Mic.begin()
    except Exception:
        pass
    aud = want


def voice_init():
    try:
        M5.Mic.config(magnification=MIC_GAIN)
    except Exception:
        pass


def voice_alloc():
    """Build the chunk pool (~8 ms). GC is held off while the ~58 KB is
    allocated, then run once here (~17 ms) instead of at some random later
    point in the session."""
    global VA, VH, VB
    if VA is not None:
        return True
    if uctypes is None:
        return False
    gc.disable()
    try:
        VA = [array("h", V_ZERO) for _ in range(V_N)]
        VH = [memoryview(a) for a in VA]
        VB = [memoryview(uctypes.bytearray_at(uctypes.addressof(a), V_CB)) for a in VA]
        ok = True
    except Exception:
        VA = VH = VB = None
        ok = False
    gc.enable()
    gc.collect()
    return ok


def voice_free():
    """Only after the mic is ended and the speaker channel stopped or drained:
    both write/read the chunks from their own tasks."""
    global VA, VH, VB
    VA = VH = VB = None


def voice_reset():
    global v_esent, v_rx_done, v_rx_any, v_playing, v_fill, v_fill_n, v_in_off
    v_esent = v_rx_done = v_rx_any = v_playing = False
    v_fill, v_fill_n, v_in_off = -1, 0, 0
    for q in (v_rec, v_tx, v_play, v_spk, v_free):
        q.clear()
    v_free.extend(range(V_N))


def voice_stop():
    """Drop the session and hand the I2S back to the speaker. Unsent frames are
    dropped; the rest of a reply is ignored (the next 'H' cancels it server-side)."""
    global v_state, v_sid
    v_sid = 0
    tx_v.clear()
    v_in.clear()
    if aud == "mic":
        audio("spk")            # Mic.end() stops it writing into the pool
    else:
        try:
            M5.Speaker.stop(0)
        except Exception:
            pass
    try:
        M5.Speaker.setVolume(SPK_VOL)
    except Exception:
        pass
    voice_reset()
    voice_free()
    wifi_fast(False)
    v_state = V_IDLE


def voice_fail(msg):
    global v_state, v_msg, v_until
    voice_stop()
    v_state, v_msg = V_ERR, msg
    v_until = time.ticks_add(time.ticks_ms(), 2500)
    buzz(2, 120, 80)


def talk_start(now):
    global v_state, v_t0, v_msg, v_lvl, v_first, v_skip, v_sid, v_sids
    if ui == "prompt" or ui == "sos_wait":
        return                  # answer the prompt (or cancel the SOS) with A first
    if v_state:
        voice_stop()            # a new question replaces the running one
    if not wifi_up:
        banner("VOICE", "Voice needs WiFi", AMBER, 2000)
        buzz(1, 80, 60)
        return
    if link != L_WS:
        banner("VOICE", "Voice needs the live link" if link == L_POST else "Cloud not connected",
               AMBER, 2000)
        buzz(1, 80, 60)
        return
    if not voice_alloc():
        banner("VOICE", "Voice unavailable", AMBER, 2000)
        return
    voice_reset()
    wifi_fast(True)
    v_in.clear()
    v_sids = v_sids % 30000 + 1
    v_sid = v_sids
    tx_v.append((v_sid, V_HF))
    v_msg, v_lvl, v_first = "", 0.0, True
    v_t0 = now
    v_state = V_LISTEN
    v_skip = True               # start the mic on the next iteration, not this one
    buzz(1, 60, 40)


def voice_safe(fn, *args):
    """Run a voice step. A fault in the voice path ends the session; it must
    never reach crash(), which reboots and stops fall detection for seconds."""
    try:
        fn(*args)
    except Exception:
        try:
            voice_fail("Voice error")
        except Exception:
            pass


def talk_release():
    global v_state
    if v_state == V_LISTEN:
        v_state = V_THINK


def v_mic():
    """Collect finished mic chunks for sending; keep two queued on the mic."""
    global v_first, v_lvl
    try:
        busy = M5.Mic.isRecording()
    except Exception:
        busy = 0
    while len(v_rec) > busy:
        i = v_rec.pop(0)
        a, b = VA[i], VB[i]
        if v_first:
            v_first = False
            b[2:2 + V_BLANK] = V_ZEROM[:V_BLANK]
        pk = 0
        for k in range(1, V_CE, 16):
            x = a[k]
            if x < 0:
                x = -x
            if x > pk:
                pk = x
        lv = pk / 16000.0
        v_lvl = lv if lv > v_lvl else 0.6 * v_lvl + 0.4 * lv
        b[1] = 65                               # 'A'
        v_tx.append(i)
    if v_state == V_LISTEN:
        while len(v_rec) < 2 and v_free:
            i = v_free.pop()
            try:
                M5.Mic.record(VH[i][1:], V_RATE)
            except Exception:
                v_free.append(i)
                break
            v_rec.append(i)


def v_send():
    """Hand recorded chunks to the network thread as 'A' frames (a copy, so the
    chunk is free again at once), then 'E' once the button is up. When tx_v is
    full the chunks wait in the pool: voice is never dropped here."""
    global v_esent
    while v_tx and len(tx_v) < TXV_MAX:
        i = v_tx.pop(0)
        tx_v.append((v_sid, bytes(VB[i][1:V_CB])))
        v_free.append(i)
    if v_state >= V_THINK and not v_rec and not v_tx and not v_esent:
        tx_v.append((v_sid, V_EF))
        v_esent = True


def v_queue_fill():
    global v_fill, v_fill_n
    if v_fill < 0:
        return
    if v_fill_n >= 2:
        V_NS[v_fill] = v_fill_n // 2
        v_play.append(v_fill)
    else:
        v_free.append(v_fill)
    v_fill, v_fill_n = -1, 0


def v_recv(now):
    """Take reply frames from the network thread. 'A' payloads are packed back
    to back into pool chunks so the speaker always gets full 100 ms buffers.
    Returns an error string or None."""
    global v_fill, v_fill_n, v_in_off, v_rx_done, v_rx_any, v_last_rx, v_msg, v_state
    for _ in range(4):
        if v_rx_done or not v_in:
            return None
        m = v_in[0]
        t = m[0]
        if t == 65:                             # 'A'
            mv = memoryview(m)
            n = len(m)
            off = v_in_off or 1
            while off < n:
                if v_fill < 0:
                    if not v_free:
                        v_in_off = off          # pool full: the speaker frees chunks
                        return None
                    v_fill, v_fill_n = v_free.pop(), 0
                k = min(n - off, V_CS * 2 - v_fill_n)
                VB[v_fill][2 + v_fill_n:2 + v_fill_n + k] = mv[off:off + k]
                v_fill_n += k
                off += k
                if v_fill_n >= V_CS * 2:
                    v_queue_fill()
            v_in_off = 0
            v_last_rx = now
            v_rx_any = True
            if v_state == V_THINK:
                v_state = V_SPEAK
        elif t == 84:                           # 'T'
            v_msg = clean_text(memoryview(m)[1:], min(len(m) - 1, 160))
            v_rx_any = True
            if v_state == V_THINK:
                v_state = V_SPEAK
        elif t == 69:                           # 'E'
            v_rx_done = True
            v_queue_fill()
        elif t == 88:                           # 'X'
            v_in.pop(0)
            return clean_text(memoryview(m)[1:], min(len(m) - 1, 160)) or "Assistant error"
        v_in.pop(0)
    return None


def v_playback(now):
    """Keep two chunks queued on speaker channel 0; free the ones it finished."""
    global v_playing
    if v_fill >= 0 and v_fill_n >= 2 and time.ticks_diff(now, v_last_rx) > V_GAP_MS:
        v_queue_fill()
    if aud != "spk":
        return
    try:
        busy = M5.Speaker.isPlaying(0)
    except Exception:
        busy = 0
    while len(v_spk) > busy:
        v_free.append(v_spk.pop(0))
    if not v_playing:
        # ~200 ms of prebuffer so network jitter does not stutter the reply
        if len(v_play) >= 2 or (v_play and (v_rx_done or time.ticks_diff(now, v_last_rx) > V_GAP_MS)):
            v_playing = True
        else:
            return
    while v_play and len(v_spk) < 2:
        i = v_play.pop(0)
        try:
            M5.Speaker.playRaw(VH[i][1:1 + V_NS[i]], V_RATE, False, 1, 0, False)
        except Exception:
            v_free.append(i)
            continue
        v_spk.append(i)
    if not v_play and not v_spk:
        v_playing = False


def voice_tick(now):
    global v_state, v_until, v_skip, v_sid
    if v_state >= V_DONE:
        if time.ticks_diff(now, v_until) >= 0:
            v_state = V_IDLE
        return
    if v_skip:
        v_skip = False          # talk_start already spent this iteration's budget
        return
    if v_state == V_LISTEN and aud != "mic":
        audio("mic")            # own iteration: begin() takes up to ~16 ms
        return
    el = time.ticks_diff(now, v_t0)
    if el > V_HARD_MS or (el > V_TIMEOUT_MS and not v_playing):
        voice_fail("Timed out")
        return
    if link != L_WS:
        voice_fail("Connection lost")
        return
    if v_state == V_LISTEN or v_rec:
        v_mic()
    if v_state >= V_THINK and not v_rec and aud == "mic":
        audio("spk")
        try:
            M5.Speaker.setVolume(V_VOL)
        except Exception:
            pass
    v_send()
    err = v_recv(now)
    if err:
        voice_fail(err)
        return
    v_playback(now)
    if v_rx_done and aud != "mic" and v_fill < 0 and not v_play and not v_spk:
        v_sid = 0
        voice_reset()
        voice_free()            # mic is off and the speaker has drained channel 0
        wifi_fast(False)
        try:
            M5.Speaker.setVolume(SPK_VOL)
        except Exception:
            pass
        v_state = V_DONE
        v_until = time.ticks_add(now, 5000 if v_msg else 800)


# TCP voice client (heatguard-0.4): one TCP connection to the LAN gateway's :47802 per
# question, frames type|len16|payload, read straight into the pool. Replaced by binary
# WebSocket messages above; kept for reference, not called.
# V_RH = bytearray(3)         # incoming frame header
# V_RHM = memoryview(V_RH)
# V_TXT = bytearray(160)      # incoming T / X payload (truncated)
# V_TXTM = memoryview(V_TXT)
# V_JUNK = memoryview(bytearray(256))
# V_E = memoryview(b"E\x00\x00")
# v_sock = None
# v_conn = False
# v_hdr = None
# v_hsent = False
# v_esent = False
# v_cur = None                # memoryview being sent
# v_cur_off = 0
# v_cur_i = -1
# v_rh_n = 0
# v_rtype = 0
# v_rlen = 0
# v_rgot = 0
# def talk_start(now):
#     global v_state, v_t0, v_hdr, v_msg, v_lvl, v_first, v_sock, v_skip
#     if ui == "prompt":
#         return                  # answer the prompt with A first
#     if v_state:
#         voice_stop()            # a new question replaces the running one
#     if not wifi_up:
#         banner("VOICE", "Voice needs WiFi", AMBER, 2000)
#         buzz(1, 80, 60)
#         return
#     if srv_addr is None:
#         banner("VOICE", "Voice needs WiFi - no server found", AMBER, 2000)
#         buzz(1, 80, 60)
#         return
#     if not voice_alloc():
#         banner("VOICE", "Voice unavailable", AMBER, 2000)
#         return
#     voice_reset()
#     wifi_fast(True)
#     try:
#         v_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
#         v_sock.setblocking(False)
#         try:
#             v_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
#         except Exception:
#             pass
#         try:
#             v_sock.connect((srv_ip, srv_voice))
#         except OSError as e:
#             if e.errno not in WOULD_BLOCK:
#                 raise
#     except Exception:
#         voice_fail("Server unreachable")
#         return
#     j = ('{"id":%s,"rate":%d,"fmt":"pcm16"}' % (jstr(dev_id), V_RATE)).encode()
#     v_hdr = memoryview(bytes((72, len(j) >> 8, len(j) & 255)) + j)
#     v_msg, v_lvl, v_first = "", 0.0, True
#     v_t0 = now
#     v_state = V_LISTEN
#     v_skip = True               # start the mic on the next iteration, not this one
#     buzz(1, 60, 40)
# def v_send():
#     """Push H, then the recorded chunks, then E. Returns an error string or None."""
#     global v_cur, v_cur_off, v_cur_i, v_conn, v_hsent, v_esent
#     for _ in range(8):
#         if v_cur is None:
#             if not v_hsent:
#                 v_cur, v_cur_i, v_hsent = v_hdr, -1, True
#             elif v_tx:
#                 i = v_tx.pop(0)
#                 v_cur, v_cur_i = VB[i][1:], i
#             elif v_state >= V_THINK and not v_rec and not v_esent:
#                 v_cur, v_cur_i, v_esent = V_E, -1, True
#             else:
#                 return None
#             v_cur_off = 0
#         try:
#             n = v_sock.send(v_cur[v_cur_off:])
#         except OSError as e:
#             if e.errno in WOULD_BLOCK or (not v_conn and e.errno == ENOTCONN):
#                 return None     # still connecting; V_CONNECT_MS bounds the wait
#             return "Connection lost" if v_conn else "Server unreachable"
#         if not n:
#             return None
#         v_conn = True
#         v_cur_off += n
#         if v_cur_off >= len(v_cur):
#             if v_cur_i >= 0:
#                 v_free.append(v_cur_i)
#             v_cur = None
#     return None
# def v_eof():
#     global v_rx_done
#     if v_rx_any:
#         v_rx_done = True
#         v_queue_fill()
#         return None
#     return "No reply"
# def v_recv(now):
#     """Parse reply frames. 'A' payloads are packed back to back into pool
#     chunks so the speaker always gets full 100 ms buffers. Returns an error
#     string or None."""
#     global v_rh_n, v_rtype, v_rlen, v_rgot, v_fill, v_fill_n, v_rx_done, v_rx_any
#     global v_last_rx, v_msg, v_state
#     for _ in range(8):
#         if v_rx_done:
#             return None
#         if v_rh_n < 3:
#             r = v_sock.readinto(V_RHM[v_rh_n:])
#             if r is None:
#                 return None
#             if not r:
#                 return v_eof()
#             v_rh_n += r
#             if v_rh_n < 3:
#                 continue
#             v_rtype, v_rlen, v_rgot = V_RH[0], (V_RH[1] << 8) | V_RH[2], 0
#         if v_rgot < v_rlen:
#             if v_rtype == 65:
#                 if v_fill < 0:
#                     if not v_free:
#                         return None     # pool full: let playback drain (TCP backpressure)
#                     v_fill, v_fill_n = v_free.pop(), 0
#                 b = 4 + v_fill_n
#                 r = v_sock.readinto(VB[v_fill][b:b + min(v_rlen - v_rgot, V_CS * 2 - v_fill_n)])
#             elif v_rgot < len(V_TXT):
#                 r = v_sock.readinto(V_TXTM[v_rgot:min(v_rlen, len(V_TXT))])
#             else:
#                 r = v_sock.readinto(V_JUNK[:min(v_rlen - v_rgot, len(V_JUNK))])
#             if r is None:
#                 return None
#             if not r:
#                 return v_eof()
#             v_rgot += r
#             v_last_rx = now
#             if v_rtype == 65:
#                 v_fill_n += r
#                 if v_fill_n >= V_CS * 2:
#                     v_queue_fill()
#             if v_rgot < v_rlen:
#                 continue
#         v_rh_n = 0
#         t = v_rtype
#         if t == 65 or t == 84:                  # 'A', 'T'
#             v_rx_any = True
#             if t == 84:
#                 v_msg = clean_text(V_TXT, min(v_rlen, len(V_TXT)))
#             if v_state == V_THINK:
#                 v_state = V_SPEAK
#         elif t == 69:                           # 'E'
#             v_rx_done = True
#             v_queue_fill()
#         elif t == 88:                           # 'X'
#             return clean_text(V_TXT, min(v_rlen, len(V_TXT))) or "Assistant error"
#     return None


def clean_text(buf, n):
    """UTF-8 to what the LCD fonts can draw: ASCII, with common punctuation mapped."""
    s = []
    i = 0
    while i < n:
        c = buf[i]
        if c < 128:
            if c >= 32:
                s.append(chr(c))
            elif c in (9, 10, 13):
                s.append(" ")
            i += 1
            continue
        if c == 0xE2 and i + 2 < n and buf[i + 1] == 0x80:
            d = buf[i + 2]
            if d in (0x98, 0x99):
                s.append("'")
            elif d in (0x9C, 0x9D):
                s.append('"')
            elif d in (0x93, 0x94):
                s.append("-")
            elif d == 0xA6:
                s.append("...")
            i += 3
            continue
        i += 1
        while i < n and (buf[i] & 0xC0) == 0x80:
            i += 1
    return "".join(s).strip()


# ---------------------------------------------------------------- feedback
def buzz(n, on=180, off=140):
    for _ in range(n):
        buzz_q.append((on, off))


def beep(hz, ms):
    if aud != "spk":
        return                  # the mic holds I2S while listening
    try:
        M5.Speaker.tone(hz, ms)
    except Exception:
        pass


def run_buzz(now):
    global buzz_state, buzz_at
    if buzz_state == 0:
        if buzz_q:
            on, off = buzz_q[0]
            try:
                M5.Power.setVibration(200)
            except Exception:
                pass
            buzz_state, buzz_at = 1, time.ticks_add(now, on)
    elif time.ticks_diff(now, buzz_at) >= 0:
        if buzz_state == 1:
            try:
                M5.Power.setVibration(0)
            except Exception:
                pass
            buzz_state, buzz_at = 2, time.ticks_add(now, buzz_q[0][1])
        else:
            buzz_q.pop(0)
            buzz_state = 0


def banner(title, text, color, ms):
    global ui, ui_kind, ui_text, ui_color, ui_until
    if ui in ALARM_UI:
        return
    ui, ui_kind, ui_text, ui_color = "banner", title, text, color
    ui_until = time.ticks_add(time.ticks_ms(), ms)


def start_rest_banner(reason):
    banner("COOL-DOWN", reason, CYAN, 6000)
    buzz(3, 250, 150)
    beep(1400, 200)


def alert_again():
    """Another alarm fired while the escalated / SOS screen is up. Its event
    has gone out; keep the screen, count it and tell the worker."""
    global alert_n
    if v_state:
        voice_stop()            # show the alert screen, free the speaker for the beep
    alert_n += 1
    buzz(3, 300, 150)
    beep(3200, 300)


def prompt(kind, **detail):
    """Ask 'are you OK?'. The event goes to the host immediately; the answer
    (or the lack of one) follows as <kind>_ok / <kind>_noresp."""
    global ui, ui_kind, ui_until
    ev(kind, **detail)          # first, so nothing below can delay or lose it
    if ui in ALARM_UI:
        if ui != "prompt":
            alert_again()
        return
    if v_state:
        voice_stop()            # the alarm needs the screen and the speaker
    ui, ui_kind = "prompt", kind
    ui_until = time.ticks_add(time.ticks_ms(), P["PROMPT_S"] * 1000)
    buzz(3, 300, 150)
    beep(2600, 250)


# ---------------------------------------------------------------- detection
def fall_step(now, a, g):
    global fs, ff_ms, ff_start, t_imp, imp_g, had_ff, st_n, st_s, st_s2, st_gs
    if fs == 0:
        if a < P["FF_G"]:
            if ff_ms == 0:
                ff_start = now
            ff_ms = time.ticks_diff(now, ff_start) + DT_MS
            if ff_ms >= P["FF_MIN_MS"]:
                fs = 1
        else:
            ff_ms = 0
        if a > P["IMPACT_ONLY_G"]:
            fs, t_imp, imp_g, had_ff = 2, now, a, False
            st_n = 0
    elif fs == 1:
        if a < P["FF_G"]:
            ff_ms = time.ticks_diff(now, ff_start) + DT_MS
        elif a > P["IMPACT_G"]:
            fs, t_imp, imp_g, had_ff = 2, now, a, True
            st_n, st_s, st_s2, st_gs = 0, 0.0, 0.0, 0.0
        elif time.ticks_diff(now, ff_start) > ff_ms + P["IMPACT_WIN_MS"]:
            fs, ff_ms = 0, 0
    else:
        el = time.ticks_diff(now, t_imp)
        if el < P["SETTLE_MS"]:
            if a > imp_g:
                imp_g = a
            st_n, st_s, st_s2, st_gs = 0, 0.0, 0.0, 0.0
        elif el < P["SETTLE_MS"] + P["STILL_WIN_MS"]:
            st_n += 1
            st_s += a
            st_s2 += a * a
            st_gs += g
        else:
            if st_n:
                m = st_s / st_n
                std = math.sqrt(max(0.0, st_s2 / st_n - m * m))
                gmean = st_gs / st_n
            else:
                std, gmean = 9.0, 999.0
            still = std < P["STILL_STD_G"] and gmean < P["STILL_GYRO_DPS"]
            ffd = ff_ms if had_ff else 0
            # free-fall + impact is a fall even if the worker moves afterwards;
            # a bare impact still needs the stillness to count
            if (had_ff and imp_g >= P["IMPACT_G"]) or (still and imp_g >= P["IMPACT_ONLY_G"]):
                prompt("fall", impact_g=imp_g, freefall_ms=ffd, post_std=std, post_gyro=gmean,
                       still=1 if still else 0)
            elif imp_g >= P["IMPACT_ONLY_G"]:
                ev("impact", impact_g=imp_g, freefall_ms=ffd, post_std=std, post_gyro=gmean)
                if ui in ("escalated", "sos", "sos_wait"):
                    alert_again()
            fs, ff_ms = 0, 0


def analyse_window():
    """Dominant-axis gyro rhythm over the last 2 s: frequency, RMS, regularity."""
    global trem_hz, trem_amp, trem_run, err_run, trem_cool
    best_var, best, best_m = -1.0, gxb, 0.0
    for buf in (gxb, gyb, gzb):
        s = 0.0
        s2 = 0.0
        for v in buf:
            s += v
            s2 += v * v
        m = s / WIN
        var = s2 / WIN - m * m
        if var > best_var:
            best_var, best, best_m = var, buf, m
    rms = math.sqrt(max(0.0, best_var))
    hyst = max(5.0, 0.3 * rms)
    state, cross, last, n_int, si, si2 = 0, 0, -1, 0, 0.0, 0.0
    for i in range(WIN):
        v = best[(wi + i) % WIN] - best_m
        flip = False
        if state <= 0 and v > hyst:
            flip, state = state == -1, 1
        elif state >= 0 and v < -hyst:
            flip, state = state == 1, -1
        if flip:
            cross += 1
            if last >= 0:
                d = i - last
                n_int += 1
                si += d
                si2 += d * d
            last = i
    hz = cross / 2.0 / (WIN * DT_MS / 1000.0)
    cvar = 9.0
    if n_int >= 3:
        mu = si / n_int
        cvar = math.sqrt(max(0.0, si2 / n_int - mu * mu)) / mu if mu else 9.0
    rhythmic = P["TREM_MIN_HZ"] <= hz <= P["TREM_MAX_HZ"] and cvar <= P["TREM_MAX_CV"]
    trem_hz, trem_amp = hz, rms

    if trem_cool > 0:
        trem_cool -= 1
        trem_run = err_run = 0
        return
    if rhythmic and rms >= P["TREM_MIN_DPS"]:
        trem_run += 1
    else:
        trem_run = 0
    if not rhythmic and rms >= P["ERRATIC_DPS"]:
        err_run += 1
    else:
        err_run = 0
    per_s = 1000.0 / (HOP * DT_MS)
    if trem_run >= P["TREM_SUSTAIN_S"] * per_s:
        prompt("tremor", hz=hz, rms_dps=rms, cv=cvar, dur_s=trem_run / per_s)
        trem_run, trem_cool = 0, int(30 * per_s)
    elif err_run >= P["ERRATIC_SUSTAIN_S"] * per_s:
        prompt("erratic", hz=hz, rms_dps=rms, cv=cvar, dur_s=err_run / per_s)
        err_run, trem_cool = 0, int(30 * per_s)


# ---------------------------------------------------------------- drawing
def font(name):
    try:
        cv.setFont(getattr(FONTS, name))
    except Exception:
        cv.setFont(FONTS.DejaVu12)


def text_c(s, y, color, bg, fname):
    font(fname)
    cv.setTextColor(color, bg)
    cv.drawCenterString(s, W // 2, y)


def wrap(s, n):
    words, lines, cur = s.split(), [], ""
    for w_ in words:
        if len(cur) + len(w_) + (1 if cur else 0) > n:
            if cur:
                lines.append(cur)
            cur = w_
        else:
            cur = (cur + " " + w_) if cur else w_
    if cur:
        lines.append(cur)
    return lines


def linked(now):
    """The server can reach us: it owns the heat plan while this holds."""
    return link == L_WS or time.ticks_diff(now, link_ms) < 8000


def link_col(now):
    if link == L_WS:
        return GREEN
    if link == L_POST:
        return CYAN             # telemetry and incidents up, no commands
    if link in (L_CONN, L_TLS):
        return AMBER if (now // 300) % 2 else FAINT
    return RED


def wifi_icon(x, now):
    """Three bars: faint = no wifi, amber sweep = connecting,
    amber = cloud not up, green = online. Lit bars follow RSSI once up."""
    ns = net_state(now)
    if ns == 0:
        lit, col = 0, FAINT
    elif ns == 1:
        lit, col = (now // 300) % 4, AMBER
    else:
        r = rssi if rssi is not None else -70
        lit = 3 if r > -60 else (2 if r > -72 else 1)
        col = GREEN if ns >= 3 else AMBER
    for k in range(3):
        h = 3 + 3 * k
        cv.fillRect(x + 3 * k, 15 - h, 2, h, col if k < lit else 0x3A3A3A)


def header(now):
    cv.fillRect(0, 0, W, 20, 0x111111)
    font("Montserrat12")
    cv.setTextColor(0xFF7A1A, 0x111111)
    cv.drawString("HEATGUARD", 3, 4)
    wifi_icon(86, now)
    cv.fillCircle(98, 10, 3, link_col(now))
    cv.setTextColor(DIM, 0x111111)
    cv.drawRightString(("%d%%" % bat) if bat >= 0 else "--", W - 3, 4)


def fmt_mmss(ms):
    s = max(0, ms // 1000)
    return "%d:%02d" % (s // 60, s % 60)


def draw_main(now):
    cv.fillScreen(BLACK)
    header(now)
    ph = "stop" if heat_lvl else plan["phase"]
    if ph == "rest":
        col, word = CYAN, "COOL-DOWN"
    elif ph == "stop":
        col, word = RED, "STOP WORK"
    else:
        col, word = GREEN, "WORKING"
    cv.fillRoundRect(4, 26, W - 8, 34, 8, col)
    text_c(word, 34, BLACK, col, "Montserrat18")

    # WBGT
    font("Montserrat12")
    cv.setTextColor(DIM, BLACK)
    cv.drawString("WBGT", 6, 68)
    lvl = int(plan.get("level") or 0)
    hc = HEAT_COLORS[max(0, min(4, lvl))]
    cv.setTextColor(hc, BLACK)
    cv.drawRightString(str(plan.get("label") or "--").upper(), W - 6, 68)
    font("DejaVu24")
    cv.setTextColor(WHITE, BLACK)
    wb = plan.get("wbgt")
    cv.drawString(("%.1f" % wb) if wb is not None else "--.-", 6, 84)
    font("Montserrat12")
    cv.setTextColor(DIM, BLACK)
    cv.drawString("C", 92 if wb is not None else 80, 86)

    # work budget or cool-down countdown
    if ph == "rest":
        left = time.ticks_diff(rest_end_ms, now)
        text_c(fmt_mmss(left * max(1, plan["warp"])), 118, CYAN, BLACK, "DejaVu40")
        text_c("shade + water", 162, DIM, BLACK, "Montserrat12")
    elif ph == "stop":
        text_c("Too hot to work", 124, RED, BLACK, "Montserrat14")
        text_c("rest in shade", 146, DIM, BLACK, "Montserrat12")
    else:
        el, bud = float(plan["elapsed"]), float(plan["work"] or 1)
        frac = min(1.0, el / bud) if bud else 1.0
        font("Montserrat12")
        cv.setTextColor(DIM, BLACK)
        cv.drawString("work cycle", 6, 122)
        cv.setTextColor(WHITE, BLACK)
        cv.drawRightString("%d/%d min" % (int(el), int(bud)), W - 6, 122)
        cv.fillRoundRect(6, 140, W - 12, 10, 4, FAINT)
        bc = GREEN if frac < 0.8 else (AMBER if frac < 1.0 else RED)
        cv.fillRoundRect(6, 140, max(6, int((W - 12) * frac)), 10, 4, bc)
        text_c("break in %d min" % max(0, int(bud - el)), 158, DIM, BLACK, "Montserrat12")

    # motion
    font("Montserrat12")
    cv.setTextColor(DIM, BLACK)
    cv.drawString("activity", 6, 184)
    cv.setTextColor(WHITE, BLACK)
    cv.drawRightString(workload(), W - 6, 184)
    cv.fillRect(6, 200, W - 12, 5, FAINT)
    cv.fillRect(6, 200, int((W - 12) * min(1.0, act / 0.3)), 5, AMBER if act > 0.2 else GREEN)
    ns = net_state(now)
    cv.setTextColor(FAINT if ns == 3 else (DIM if ns == 4 else AMBER), BLACK)
    cv.drawCenterString(link_tip(now), W // 2, 222)


def draw_motion(now):
    cv.fillScreen(BLACK)
    header(now)
    font("Montserrat12")
    cv.setTextColor(DIM, BLACK)
    cv.drawString("|a| last 3 s", 6, 26)
    top, h = 42, 60
    cv.drawRect(4, top, W - 8, h, FAINT)
    y1 = top + h - int(h * 1.0 / 3.0)
    cv.drawLine(5, y1, W - 6, y1, FAINT)
    px, py = -1, -1
    for i in range(64):
        v = spark[(spark_i + i) % 64]
        x = 6 + i * (W - 14) // 63
        y = top + h - 1 - int((h - 2) * min(3.0, v) / 3.0)
        if px >= 0:
            cv.drawLine(px, py, x, y, CYAN)
        px, py = x, y
    rows = [
        ("|a|", "%.2f g" % am), ("|w|", "%.0f dps" % gm),
        ("rhythm", "%.1f Hz" % trem_hz), ("shake", "%.0f dps" % trem_amp),
        ("still", "%d s" % (still_ms // 1000)), ("chip", "%.0f C" % chip_t),
        ("id", dev_id[-6:]), ("cloud", link_short(now)),
    ]
    y = 106
    for k, v in rows:
        cv.setTextColor(DIM, BLACK)
        cv.drawString(k, 6, y)
        cv.setTextColor(WHITE, BLACK)
        cv.drawRightString(v, W - 6, y)
        y += 14
    cv.setTextColor(FAINT, BLACK)
    cv.drawCenterString("hold B = talk", W // 2, 226)


def draw_voice(now):
    cv.fillScreen(BLACK)
    header(now)
    st = v_state
    if st == V_LISTEN:
        title, col, tc = "LISTENING", PURPLE, WHITE
    elif st == V_THINK:
        title, col, tc = "THINKING", AMBER, BLACK
    elif st == V_ERR:
        title, col, tc = "VOICE", RED, WHITE
    else:
        title, col, tc = "SPEAKING", GREEN, BLACK
    cv.fillRoundRect(4, 26, W - 8, 34, 8, col)
    text_c(title, 34, tc, col, "Montserrat18")
    if st == V_LISTEN:
        text_c(fmt_mmss(time.ticks_diff(now, v_t0)), 80, WHITE, BLACK, "DejaVu40")
        cv.fillRoundRect(6, 140, W - 12, 16, 6, FAINT)
        lv = min(1.0, v_lvl)
        if lv > 0.03:
            cv.fillRoundRect(6, 140, max(12, int((W - 12) * lv)), 16, 6, RED if lv > 0.9 else PURPLE)
        text_c("speak now", 170, DIM, BLACK, "Montserrat14")
        text_c("release B to send", 222, DIM, BLACK, "Montserrat12")
    elif st == V_THINK:
        k = (now // 300) % 3
        for i in range(3):
            cv.fillCircle(W // 2 - 24 + 24 * i, 120, 7, AMBER if i == k else FAINT)
        text_c("A = cancel", 222, DIM, BLACK, "Montserrat12")
    else:
        lines, fname, step = wrap(v_msg, 14), "Montserrat14", 20
        if len(lines) > 7:
            lines, fname, step = wrap(v_msg, 18), "Montserrat12", 16
        y = 70
        for ln in lines[:9]:
            text_c(ln, y, RED if st == V_ERR else WHITE, BLACK, fname)
            y += step
        text_c("A = close", 222, DIM, BLACK, "Montserrat12")


PROMPT_TITLE = {"fall": "FALL?", "tremor": "SHAKING", "erratic": "MOVEMENT",
                "inactivity": "NO MOVE", "unwell": "UNWELL", "heat_stroke": "HEAT STROKE?"}


def draw_overlay(now):
    if ui == "prompt":
        flash = (now // 400) % 2 == 0
        bg = RED if (ui_kind == "fall" or flash) else 0x7F1D1D
        if ui_kind in ("tremor", "erratic", "inactivity"):
            bg = AMBER if flash else 0x78350F
        cv.fillScreen(bg)
        title = PROMPT_TITLE.get(ui_kind, ui_kind.upper())
        text_c(title, 22, WHITE, bg, "Montserrat24" if len(title) <= 8 else "Montserrat18")
        text_c("Are you OK?", 60, WHITE, bg, "Montserrat16")
        left = max(0, time.ticks_diff(ui_until, now)) // 1000 + 1
        text_c(str(left), 92, WHITE, bg, "DejaVu56")
        cv.fillRoundRect(10, 176, W - 20, 40, 10, WHITE)
        text_c("A = I'm OK", 188, bg, WHITE, "Montserrat16")
    elif ui == "sos_wait":
        bg = RED if (now // 400) % 2 == 0 else 0x7F1D1D
        cv.fillScreen(bg)
        left = max(0, time.ticks_diff(ui_until, now)) // 1000 + 1
        text_c("SOS", 22, WHITE, bg, "Montserrat24")
        text_c("Calling in %d s" % left, 60, WHITE, bg, "Montserrat14")
        text_c(str(left), 92, WHITE, bg, "DejaVu56")
        cv.fillRoundRect(10, 176, W - 20, 40, 10, WHITE)
        text_c("A = cancel", 188, bg, WHITE, "Montserrat16")
    elif ui == "escalated" or ui == "sos":
        cv.fillScreen(RED)
        text_c("SOS" if ui == "sos" else "ALERT", 30, WHITE, RED, "Montserrat24")
        text_c("SENT (%d)" % alert_n if alert_n > 1 else "SENT", 62, WHITE, RED, "Montserrat24")
        y = 110
        for ln in wrap("Supervisor notified. Stay where you are.", 14):
            text_c(ln, y, WHITE, RED, "Montserrat14")
            y += 20
        text_c("A = cancel", 216, 0xFECACA, RED, "Montserrat12")
    elif ui == "ack":
        cv.fillScreen(BLUE)
        text_c("HELP", 30, WHITE, BLUE, "Montserrat24")
        text_c("COMING", 62, WHITE, BLUE, "Montserrat24")
        y = 112
        for ln in wrap(ui_text, 14)[:5]:
            text_c(ln, y, WHITE, BLUE, "Montserrat14")
            y += 20
    elif ui == "msg":
        cv.fillScreen(WHITE)
        cv.fillRect(0, 0, W, 22, 0x111111)
        font("Montserrat12")
        cv.setTextColor(0xFF7A1A, 0x111111)
        cv.drawString("ASSISTANT" if ui_kind == "say" else "SUPERVISOR", 6, 5)
        y = 34
        for ln in wrap(ui_text, 13)[:8]:
            text_c(ln, y, BLACK, WHITE, "Montserrat14")
            y += 20
        text_c("A = OK", 218, DIM, WHITE, "Montserrat12")
    elif ui == "banner":
        cv.fillScreen(ui_color)
        text_c(ui_kind, 60, BLACK, ui_color, "Montserrat18" if len(ui_kind) > 9 else "Montserrat24")
        y = 110
        for ln in wrap(ui_text, 14)[:4]:
            text_c(ln, y, BLACK, ui_color, "Montserrat14")
            y += 20
        if ui_kind == "COOL-DOWN":
            text_c("Go to shade.", 180, BLACK, ui_color, "Montserrat14")
            text_c("Drink water.", 200, BLACK, ui_color, "Montserrat14")


def workload():
    if act < 0.02:
        return "rest"
    if act < 0.08:
        return "light"
    if act < 0.2:
        return "moderate"
    return "heavy"


# ---------------------------------------------------------------- buttons / ui
def buttons(now):
    global holdA, holdB, firedA, firedB, page
    a_down = M5.BtnA.isPressed()
    b_down = M5.BtnB.isPressed()
    if a_down:
        if holdA == 0:
            holdA = now
        if (not firedA and time.ticks_diff(now, holdA) > 2000
                and ui not in ("sos_wait", "sos", "escalated")):
            firedA = True       # so letting go of this hold is not the cancel press
            ev("sos")           # event + incident (suspected) first
            if v_state:
                voice_stop()
            # the call waits SOS_WAIT_S for a cancel; then sos_noresp and SOS SENT
            set_ui("sos_wait", P["SOS_WAIT_S"] * 1000)
            buzz(2, 400, 150)
            beep(3000, 300)
    else:
        if holdA and not firedA:
            click_a()
        holdA, firedA = 0, False
    if b_down:
        if holdB == 0:
            holdB = now
        if not firedB and time.ticks_diff(now, holdB) >= TALK_HOLD_MS:
            firedB = True
            voice_safe(talk_start, now)
    else:
        if holdB:
            if firedB:
                voice_safe(talk_release)
            elif v_state >= V_DONE:
                voice_stop()
            elif v_state == V_IDLE and ui == "normal":
                page = (page + 1) % 2
        holdB, firedB = 0, False


def click_a():
    global ui, still_ms
    if ui == "sos_wait":
        ev("sos_cancel")        # incident: cancelled, no call
        ui = "normal"
        banner("CANCELLED", "SOS cancelled. No call.", GREEN, 2500)
        buzz(1)
    elif v_state and ui != "prompt":
        voice_stop()
    elif ui == "prompt":
        ev(ui_kind + "_ok")
        ui = "normal"           # banner() will not replace a prompt; without this it escalates anyway
        if ui_kind == "heat_stroke":
            banner("COOL DOWN", "Rest in the shade and drink water now.", CYAN, 8000)
        else:
            banner("THANKS", "Glad you're OK. Stay hydrated.", GREEN, 2500)
        still_ms = 0
    elif ui in ("escalated", "sos"):
        ev((ui_kind or "sos") + "_cancel")
        set_ui("normal")
    elif ui in ("ack", "msg", "banner"):
        set_ui("normal")


def set_ui(mode, ms=3600000):
    global ui, ui_kind, ui_until, alert_n
    if mode in ("sos", "sos_wait"):
        ui_kind = "sos"
    if mode in ("sos", "escalated", "sos_wait") and ui != "sos_wait":
        alert_n = 1
    ui = mode
    ui_until = time.ticks_add(time.ticks_ms(), ms)


def ui_tick(now):
    global ui
    if ui == "prompt":
        if (now // 1000) % 2 == 0 and buzz_state == 0 and not buzz_q:
            buzz(1, 250, 100)
        if time.ticks_diff(now, ui_until) >= 0:
            ev(ui_kind + "_noresp")
            set_ui("escalated")
            buzz(4, 400, 150)
            beep(3200, 500)
    elif ui == "sos_wait":
        if (now // 1000) % 2 == 0 and buzz_state == 0 and not buzz_q:
            buzz(1, 250, 100)
        if time.ticks_diff(now, ui_until) >= 0:
            ev("sos_noresp")    # incident: no_response, the backend calls now
            set_ui("sos")
            buzz(4, 400, 150)
            beep(3200, 500)
    elif ui in ("banner", "msg", "ack") and time.ticks_diff(now, ui_until) >= 0:
        ui = "normal"


# ---------------------------------------------------------------- heat fallback
def heat_tick(dt_s, now):
    """Server owns the plan; this only keeps the cycle honest when the link drops."""
    global rest_end_ms
    warp = max(1, plan["warp"])
    if plan["phase"] == "work":
        plan["elapsed"] = float(plan["elapsed"]) + dt_s * warp / 60.0
        if not linked(now) and plan["work"] and plan["elapsed"] >= plan["work"]:
            plan["phase"], plan["remaining"] = "rest", float(plan["rest"])
            plan["reason"] = "Work cycle done (offline)"
            rest_end_ms = time.ticks_add(now, int(plan["rest"] * 60000 / warp))
            start_rest_banner(plan["reason"])
    elif plan["phase"] == "rest" and not linked(now):
        if time.ticks_diff(now, rest_end_ms) >= 0:
            plan["phase"], plan["elapsed"] = "work", 0.0
            banner("BACK TO WORK", "Cool-down done", GREEN, 4000)
            buzz(1)


# ---------------------------------------------------------------- telemetry frames
def frames_init():
    """Build the sticks3.telemetry.v1 templates once: per sample it is then a
    single % with 9 or 10 values (~0.15 ms), no dicts, no json.dumps."""
    global F_P, F_T, CONF
    head = ('{"schema":"sticks3.telemetry.v1","device_id":"%s","boot_id":"%s","sequence":%%d,'
            '"read_time_us":%%d,"frame":"%s","fresh":{"accelerometer":true,"gyroscope":true,'
            '"temperature":' % (dev_id, boot_id, FRAME))
    imu = ('"imu":{"acceleration_g":{"x":%.4f,"y":%.4f,"z":%.4f},'
           '"angular_velocity_dps":{"x":%.2f,"y":%.2f,"z":%.2f}')
    F_P = head + 'false},' + imu + '}%s}'
    F_T = head + 'true},' + imu + ',"die_temperature_c":%.1f}%s}'
    CONF = (',"configuration":{"accelerometer_range_g":8,"accelerometer_odr_hz":50,'
            '"gyroscope_range_dps":2000,"gyroscope_odr_hz":50,'
            '"library":"M5Unified (UiFlow2 MicroPython)","library_version":"%s"}' % FW)


# ---------------------------------------------------------------- heat tiers
HEAT_TIERS = ((1, "CHIP_STOP", "CHIP_STOP_CLEAR", "heat_stop", "stop_work"),
              (2, "CHIP_CALL", "CHIP_CALL_CLEAR", "heat_stroke", "call"))


def heat_step(die):
    """Once a second with a fresh die temperature: raise or clear the tiers."""
    global heat_ema, heat_lvl
    heat_ema = die if heat_ema is None else heat_ema + HEAT_EMA * (die - heat_ema)
    t = heat_ema
    for lvl, k_on, k_off, kind, level in HEAT_TIERS:
        on_c, off_c = P[k_on], P[k_off]
        if not heat_on[lvl]:
            heat_run[lvl] = heat_run[lvl] + 1 if t >= on_c else 0
            if heat_run[lvl] >= HEAT_SUSTAIN_S:
                heat_on[lvl] = True
                heat_run[lvl] = 0
                if lvl == 1:
                    ev(kind, level=level, die_c=t, air_c=t - HEAT_OFFSET_C)   # incident first
                    heat_alarm()
                else:
                    # like a fall: ask first. The event and the incident (suspected)
                    # go now; A -> heat_stroke_ok / worker_ok, no answer in
                    # PROMPT_S -> heat_stroke_noresp / no_response and ALERT SENT
                    prompt(kind, level=level, die_c=t, air_c=t - HEAT_OFFSET_C)
        elif t < off_c:
            heat_on[lvl] = False
            ev(kind + "_clear", level=level, die_c=t, air_c=t - HEAT_OFFSET_C)
            if lvl == 1:
                banner("HEAT OK", "Cooler now. Back to normal.", GREEN, 4000)
                buzz(1)
    heat_lvl = 2 if heat_on[2] else (1 if heat_on[1] else 0)


def heat_cfg(c):
    """cfg chip_stop / chip_call (die C): new thresholds; the clear levels keep
    their margin unless chip_stop_clear / chip_call_clear come too."""
    for key, k_on, k_off in (("chip_stop", "CHIP_STOP", "CHIP_STOP_CLEAR"),
                             ("chip_call", "CHIP_CALL", "CHIP_CALL_CLEAR")):
        try:
            if key in c:
                v = float(c[key])
                P[k_off] = v - (P[k_on] - P[k_off])
                P[k_on] = v
            if key + "_clear" in c:
                P[k_off] = float(c[key + "_clear"])
        except Exception:
            pass


def heat_alarm():
    """Stop-work tier: a warning, no prompt."""
    banner("STOP WORK", "Too hot. Rest in the shade and drink water.", RED, 8000)
    buzz(3, 300, 150)
    beep(2600, 250)


# ---------------------------------------------------------------- main
def main():
    global cv, FONTS, dev_id, boot_id, am, gm, act, still_ms, wi, hop_n, spark_i
    global bat, chg, chip_t, T0, ev_seq, rssi, t_mono, f_seq, gap_s, tel_drop
    M5.begin()
    FONTS = M5.Lcd.FONTS
    d = M5.Display
    d.setRotation(0)
    try:
        d.setBrightness(150)
    except Exception:
        pass
    cv = d.newCanvas(W, H)
    audio("spk")
    dev_id = "sticks3-" + binascii.hexlify(machine.unique_id()).decode()[:6]
    boot_id = binascii.hexlify(os.urandom(4)).decode()
    frames_init()
    ev_seq = seq_base()
    voice_init()
    wifi_init()
    link_init()
    try:
        gc.threshold(GC_THRESHOLD)
    except Exception:
        pass
    if wlan is not None:
        net_start()

    hello()
    fb = []                                         # frames of the current batch
    T0 = t0 = time.ticks_ms()
    us0 = time.ticks_us()
    next_t = t0
    last_ui = last_st = last_hello = last_sec = last_net = gc_at = t0
    temp_at = cfg_at = prev = t0
    was_on = False
    am_slow = 1.0
    alpha = 1.0 - math.exp(-DT_MS / 5000.0)
    spark_n = 0

    while True:
        now = time.ticks_ms()
        if time.ticks_diff(now, next_t) < 0:
            time.sleep_ms(max(0, min(DT_MS, time.ticks_diff(next_t, now))))
            continue
        next_t = time.ticks_add(next_t, DT_MS)
        if time.ticks_diff(now, next_t) > 200:      # fell far behind; resync
            next_t = time.ticks_add(now, DT_MS)
        quiet = True                                # nothing heavy ran this iteration
        g_ = time.ticks_diff(now, prev)
        prev = now
        if g_ > gap_s:
            gap_s = g_

        us = time.ticks_us()
        t_mono += time.ticks_diff(us, us0)          # ticks_us wraps every ~17.9 min; this does not
        us0 = us
        ax, ay, az = M5.Imu.getAccel()
        gx, gy, gz = M5.Imu.getGyro()
        a = math.sqrt(ax * ax + ay * ay + az * az)
        g = math.sqrt(gx * gx + gy * gy + gz * gz)
        am, gm = a, g

        # telemetry frame: temperature once a second, configuration at boot, on
        # every new link and then every 60 s
        on = link == L_WS or link == L_POST
        if on and not was_on:
            cfg_at = now
        was_on = on
        conf = ""
        if time.ticks_diff(now, cfg_at) >= 0:
            cfg_at = time.ticks_add(now, CFG_EVERY_MS)
            conf = CONF
        if time.ticks_diff(now, temp_at) >= 0:
            temp_at = time.ticks_add(now, 1000)
            try:
                chip_t = float(esp32.mcu_temperature())
                heat_step(chip_t)
            except Exception:
                pass
            fb.append(F_T % (f_seq, t_mono, ax, ay, az, gx, gy, gz, chip_t, conf))
        else:
            fb.append(F_P % (f_seq, t_mono, ax, ay, az, gx, gy, gz, conf))
        f_seq += 1
        if len(fb) >= TEL_BATCH:
            if wlan is not None:
                if len(tx_tel) >= TEL_Q:
                    tel_drop += TEL_BATCH               # the deque drops the oldest batch
                tx_tel.append(",".join(fb))
            fb.clear()

        # features
        act += alpha * ((0.5 * abs(a - 1.0) + g / 2000.0) - act)
        am_slow += 0.05 * (a - am_slow)
        if abs(a - am_slow) > 0.05 or g > 8.0:
            still_ms = 0
        else:
            still_ms += DT_MS
        gxb[wi], gyb[wi], gzb[wi] = gx, gy, gz
        wi = (wi + 1) % WIN
        hop_n += 1
        if hop_n >= HOP:
            hop_n = 0
            analyse_window()
            quiet = False
        spark_n += 1
        if spark_n >= 2:
            spark_n = 0
            spark[spark_i] = a
            spark_i = (spark_i + 1) % 64

        fall_step(now, a, g)

        if (ui == "normal" and plan["phase"] == "work" and not heat_lvl
                and still_ms >= P["INACT_S"] * 1000):
            prompt("inactivity", still_s=still_ms // 1000)
            still_ms = 0

        M5.update()
        buttons(now)
        read_host()
        # udp_rx(now)   # LAN gateway beacon + commands (heatguard-0.4)
        net_rx()
        ev_tick(now)
        if v_state:
            voice_safe(voice_tick, now)
        elif v_in and not v_sid:
            v_in.clear()
        run_buzz(now)
        ui_tick(now)

        if time.ticks_diff(now, last_net) >= 250:
            last_net = now
            wifi_tick(now)

        if time.ticks_diff(now, last_sec) >= 1000:
            heat_tick(time.ticks_diff(now, last_sec) / 1000.0, now)
            last_sec = now

        if time.ticks_diff(now, last_st) >= 1000:
            last_st = now
            quiet = False
            if bat < 0 or (now // 1000) % 10 == 0:
                try:
                    bat = M5.Power.getBatteryLevel()
                    chg = 1 if M5.Power.isCharging() else 0
                except Exception:
                    pass
            rs = ""
            if wifi_up:
                try:
                    rssi = wlan.status("rssi")
                    rs = ',"rssi":%d' % rssi
                except Exception:
                    pass
            out('{"k":"st","id":"%s","temp":%.1f,"bat":%d,"chg":%d,"act":%.3f,"wl":"%s",'
                '"am":%.3f,"gm":%.1f,"trem_hz":%.2f,"trem_amp":%.1f,"still_s":%d,'
                '"phase":"%s","ui":"%s","free":%d,"gap":%d,"heat":%d%s}'
                % (dev_id, chip_t, bat, chg, act, workload(), am, gm, trem_hz, trem_amp,
                   still_ms // 1000, plan["phase"], ui, gc.mem_free() // 1024, gap_s, heat_lvl, rs))
            gap_s = 0
            while nlog:
                ser("hg " + nlog.pop(0))
        if time.ticks_diff(now, last_hello) >= 60000:
            last_hello = now
            hello()

        if time.ticks_diff(now, last_ui) >= 200:
            last_ui = now
            quiet = False
            if v_state:
                draw_voice(now)
            elif ui == "normal":
                (draw_main if page == 0 else draw_motion)(now)
            else:
                draw_overlay(now)
            cv.push(0, 0)

        if quiet and time.ticks_diff(now, gc_at) >= 0:
            gc_at = time.ticks_add(now, GC_EVERY_MS)
            gc.collect()


def crash(e):
    try:
        sys.print_exception(e)
        M5.Display.fillScreen(RED)
        M5.Display.setTextColor(WHITE, RED)
        M5.Display.setTextSize(1)
        M5.Display.drawString("HeatGuard error", 4, 20)
        M5.Display.drawString(str(e)[:22], 4, 40)
    except Exception:
        pass
    time.sleep(4)
    machine.reset()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        raise
    except Exception as e:
        crash(e)
