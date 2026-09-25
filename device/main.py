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
the device joins the network, finds the server from its UDP beacon (or from
the optional fixed 'hg_server' key) and sends JSON datagrams to it. Without
WiFi, or until a server is known, the same JSON goes out as lines on USB
serial, prefixed with '@' so they can be told apart from REPL noise. Events
carry a seq and are resent every second until the server acks them. See
docs/api.md and docs/services.md.

BtnA (big front) = answer / dismiss, hold = SOS
BtnB (side)      = switch page, hold = talk to the assistant
"""
import errno
import gc
import json
import math
import socket
import sys
import time
import select
from array import array

import esp32
import machine
import M5

try:
    import uctypes
except ImportError:
    uctypes = None

FW = "heatguard-0.4"

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
}

DT_MS = 1000 // P["FS_HZ"]
BATCH = 5                   # IMU samples per serial line -> 10 lines/s
WIN = 100                   # 2 s analysis window
HOP = 25                    # re-analyse every 0.5 s
TALK_HOLD_MS = 400          # BtnB held this long = push-to-talk
# UiFlow sets gc.threshold(20480); analyse_window() alone allocates ~22 KB of
# floats, so a 10-20 ms collection landed inside it (and on top of a draw)
# every 0.5 s. Instead the loop collects on an otherwise idle iteration, with
# a larger threshold only as a backstop.
GC_EVERY_MS = 500
GC_THRESHOLD = 65536

W, H = 135, 240
BLACK, WHITE = 0x000000, 0xFFFFFF
DIM, FAINT = 0x7A7A7A, 0x2A2A2A
GREEN, AMBER, RED = 0x22C55E, 0xF59E0B, 0xEF4444
CYAN, BLUE, PURPLE = 0x22D3EE, 0x3B82F6, 0xA855F7
HEAT_COLORS = (GREEN, 0xEAB308, 0xF97316, RED, 0xB91C1C)

# ---------------------------------------------------------------- network
UDP_LOCAL = 47801           # beacon + commands in, telemetry out (one socket)
SRV_UDP = 47800
SRV_VOICE = 47802
WIFI_RETRY_MS = 15000
WIFI_DOWN_GRACE_MS = 3000   # iPhone hotspots drop a station for a moment; ride it out
SRV_FORGET_MS = 60000       # a beacon-found server is dropped after this much silence
EV_RESEND_MS = 1000
EV_TRIES = 30
EV_MAX = 20
WOULD_BLOCK = (errno.EAGAIN, errno.EINPROGRESS)
ENOTCONN = getattr(errno, "ENOTCONN", 128)

# ---------------------------------------------------------------- state
cv = None
FONTS = None
T0 = time.ticks_ms()        # all device timestamps are ms since this
dev_id = "stick"

link_ms = -100000           # last time the host talked to us (any transport)
plan = {"wbgt": None, "work": 45, "rest": 15, "elapsed": 0.0, "phase": "work",
        "remaining": 0.0, "level": 0, "label": "--", "warp": 1, "reason": ""}
rest_end_ms = 0
page = 0

ui = "normal"               # normal | prompt | escalated | sos | ack | msg | banner
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

holdA = 0
holdB = 0
firedA = False
firedB = False

# wifi / udp
wlan = None                 # None = no credentials, WiFi stays off
wifi_ssid = ""
wifi_key = ""
wifi_up = False
wifi_ip = ""
wifi_try = 0
wifi_pm0 = None             # radio power-save mode at boot, restored after voice
rssi = None
udp = None
srv_host = ""               # fixed server from NVS hg_server, if any
srv_port = SRV_UDP
srv_fixed = False
srv_ip = None
srv_addr = None             # (ip, port) telemetry goes to; None = serial
srv_voice = SRV_VOICE
udp_ms = -100000            # last datagram from the server
wifi_down_at = 0            # when isconnected() first went false

# events awaiting an evack
ev_seq = 0
ev_pend = []                # [seq, line, due_ms, tries]


# ---------------------------------------------------------------- output
def out(s):
    """One JSON message to the server: UDP when WiFi is up and the server is
    known, otherwise a '@' line on USB serial. Never both."""
    if udp is not None and srv_addr is not None:
        try:
            udp.sendto(s, srv_addr)
        except Exception:
            pass                # ENOMEM bursts etc.: drop, telemetry is lossy
        return
    try:
        sys.stdout.write("@" + s + "\n")
    except Exception:
        pass


def jstr(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def ev(kind, **detail):
    global ev_seq
    ev_seq += 1
    parts = ",".join(jstr(k) + ":" + (jstr(v) if isinstance(v, str) else
                                      ("%.3f" % v if isinstance(v, float) else str(v)))
                     for k, v in detail.items())
    now = time.ticks_ms()
    line = ('{"k":"ev","id":%s,"seq":%d,"type":%s,"t":%d,"detail":{%s}}'
            % (jstr(dev_id), ev_seq, jstr(kind), time.ticks_diff(now, T0), parts))
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


def hello():
    params = ",".join('"%s":%s' % (k, v) for k, v in P.items())
    out('{"k":"hello","id":%s,"fw":%s,"ip":%s,"params":{%s}}'
        % (jstr(dev_id), jstr(FW), jstr(wifi_ip) if wifi_ip else "null", params))


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
        if ui not in ("prompt", "escalated", "sos"):
            ui, ui_kind, ui_text, ui_color = "msg", "msg", str(c.get("text", ""))[:140], WHITE
            ui_until = time.ticks_add(now, 30000)
        buzz(2)
        beep(1800, 120)
    elif cmd == "say":
        text = str(c.get("text", ""))[:140]
        if v_state:
            v_msg = text
        elif ui not in ("prompt", "escalated", "sos"):
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
        hello()
    elif cmd == "hello":
        hello()


# ---------------------------------------------------------------- wifi / udp
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
    global wlan, wifi_ssid, wifi_key, wifi_try, srv_host, srv_port, srv_fixed, wifi_pm0
    s = nvs_str("hg_server").strip()
    if s:
        host, port = (s.split(":", 1) + [""])[:2]
        try:
            srv_port = int(port) if port else SRV_UDP
            srv_host, srv_fixed = host, True
        except ValueError:
            pass
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


def udp_open():
    global udp
    udp_close()
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)   # lwIP needs it to receive broadcasts
        except Exception:
            pass
        s.bind(("0.0.0.0", UDP_LOCAL))
        s.setblocking(False)
        udp = s
    except Exception:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        udp = None


def udp_close():
    global udp
    if udp is not None:
        try:
            udp.close()
        except Exception:
            pass
    udp = None


def wifi_tick(now):
    global wifi_up, wifi_ip, wifi_try, srv_ip, srv_addr, rssi, wifi_down_at
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
            up = True           # short blip: keep the socket and the server
    if up and not wifi_up:
        wifi_up = True
        try:
            wifi_ip = wlan.ifconfig()[0]
        except Exception:
            wifi_ip = ""
        udp_open()
        wifi_try = now
        if srv_fixed:
            try:
                srv_addr = socket.getaddrinfo(srv_host, srv_port)[0][-1]
                srv_ip = srv_addr[0]
            except Exception:
                srv_ip = srv_addr = None
        hello()
    elif not up and wifi_up:
        wifi_up, wifi_ip, rssi = False, "", None
        udp_close()
        srv_ip = srv_addr = None
        if V_IDLE < v_state < V_DONE:
            voice_fail("WiFi lost")
        wifi_try = now
    if up:
        if udp is None and time.ticks_diff(now, wifi_try) >= 5000:
            wifi_try = now
            udp_open()
        if (srv_addr is not None and not srv_fixed
                and time.ticks_diff(now, udp_ms) > SRV_FORGET_MS):
            srv_ip = srv_addr = None
    elif time.ticks_diff(now, wifi_try) >= WIFI_RETRY_MS:
        wifi_try = now
        try:
            wlan.disconnect()
        except Exception:
            pass
        try:
            wlan.connect(wifi_ssid, wifi_key)
        except Exception:
            pass


def udp_rx(now):
    """Drain the UDP socket (bounded): server beacons and commands."""
    global srv_ip, srv_addr, srv_voice, udp_ms, link_ms
    if udp is None:
        return
    for _ in range(8):
        try:
            data, addr = udp.recvfrom(640)
        except Exception:
            return
        try:
            s = data.decode().strip()
            if s.startswith("@"):
                s = s[1:]
            c = json.loads(s)
            ip = addr[0]
        except Exception:
            continue
        if not isinstance(c, dict):
            continue
        if "hg" in c:
            new = False
            if srv_fixed:
                if ip != srv_ip:
                    continue
            else:
                try:
                    port = int(c.get("udp") or SRV_UDP)
                except Exception:
                    port = SRV_UDP
                if srv_addr is None or srv_ip != ip or srv_addr[1] != port:
                    srv_ip, srv_addr, new = ip, (ip, port), True
            try:
                srv_voice = int(c.get("voice") or SRV_VOICE)
            except Exception:
                srv_voice = SRV_VOICE
            udp_ms = link_ms = now
            if new:
                hello()
        elif "cmd" in c:
            if srv_addr is None and not srv_fixed:
                srv_ip, srv_addr = ip, (ip, SRV_UDP)
            udp_ms = link_ms = now
            try:
                handle(c)
            except Exception:
                pass


def net_state(now):
    """0 no wifi configured, 1 connecting, 2 no server, 3 linked over WiFi."""
    if wlan is None:
        return 0
    if not wifi_up:
        return 1
    if srv_addr is None or time.ticks_diff(now, udp_ms) >= 8000:
        return 2
    return 3


# ---------------------------------------------------------------- voice
# Push-to-talk, driven from the main loop as a state machine on a
# non-blocking socket so the IMU keeps its 50 Hz. Audio lives in a pool of
# 100 ms chunks; the mic, the socket and the speaker all work on chunk
# indices into that pool. Each chunk keeps a 4-byte slot in front of its
# samples so an outgoing 'A' frame (1 type + 2 length bytes) is sent straight
# from the pool with the samples still 2-byte aligned.
#
# The pool exists only while a session runs. The GC scans every live buffer
# and the heap grows to hold it: a permanent 192 KB pool made each collection
# ~45 ms instead of ~8 ms, and analyse_window() triggers one every call.
V_RATE = 24000
V_CS = 2400                 # samples per chunk (100 ms)
V_CE = V_CS + 2             # int16 elements per chunk incl. the header slot
V_CB = V_CE * 2             # bytes per chunk
V_N = 12                    # pool: 1.2 s of audio
V_BLANK = 2400              # bytes blanked at the start of a take (mic start-up click)
V_TIMEOUT_MS = 30000        # whole session
V_HARD_MS = 45000           # ...but a reply that is already playing may run to here
V_CONNECT_MS = 4000
V_GAP_MS = 150              # play a part-filled reply chunk after this much silence
MIC_GAIN = 4                # M5Unified default 16 clips on ambient noise
SPK_VOL = 255             # max: alarms must be heard on a noisy site
V_VOL = 255                 # speaker volume while a reply plays (max)

V_IDLE, V_LISTEN, V_THINK, V_SPEAK, V_DONE, V_ERR = 0, 1, 2, 3, 4, 5

aud = None                  # 'spk' | 'mic' | None -- they share the I2S pins
VA = None                   # pool chunks, array('h') each; None when idle
VH = None                   # int16 memoryview per chunk (mic / speaker)
VB = None                   # byte memoryview per chunk, same memory (socket)
V_NS = array("H", [0] * V_N)    # samples held by each reply chunk
V_RH = bytearray(3)         # incoming frame header
V_RHM = memoryview(V_RH)
V_TXT = bytearray(160)      # incoming T / X payload (truncated)
V_TXTM = memoryview(V_TXT)
V_JUNK = memoryview(bytearray(256))
V_E = memoryview(b"E\x00\x00")
V_ZERO = bytes(V_CB)        # template for new chunks, and the start-up blank
V_ZEROM = memoryview(V_ZERO)

v_state = V_IDLE
v_t0 = 0
v_until = 0
v_sock = None
v_conn = False
v_hdr = None
v_hsent = False
v_esent = False
v_first = False
v_skip = False
v_lvl = 0.0
v_msg = ""
v_free = []                 # chunk indices not in use
v_rec = []                  # queued on the mic, oldest first
v_tx = []                   # recorded, waiting to be sent
v_play = []                 # received, waiting for the speaker
v_spk = []                  # queued on the speaker, oldest first
v_cur = None                # memoryview being sent
v_cur_off = 0
v_cur_i = -1
v_rh_n = 0
v_rtype = 0
v_rlen = 0
v_rgot = 0
v_fill = -1                 # reply chunk being filled
v_fill_n = 0
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
    global v_sock, v_conn, v_hsent, v_esent, v_cur, v_cur_off, v_cur_i
    global v_rh_n, v_rgot, v_rlen, v_fill, v_fill_n, v_rx_done, v_rx_any, v_playing
    if v_sock is not None:
        try:
            v_sock.close()
        except Exception:
            pass
    v_sock = None
    v_conn = v_hsent = v_esent = v_rx_done = v_rx_any = v_playing = False
    v_cur, v_cur_off, v_cur_i = None, 0, -1
    v_rh_n = v_rgot = v_rlen = v_fill_n = 0
    v_fill = -1
    for q in (v_rec, v_tx, v_play, v_spk, v_free):
        q.clear()
    v_free.extend(range(V_N))


def voice_stop():
    """Drop the session and hand the I2S back to the speaker."""
    global v_state
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
    global v_state, v_t0, v_hdr, v_msg, v_lvl, v_first, v_sock, v_skip
    if ui == "prompt":
        return                  # answer the prompt with A first
    if v_state:
        voice_stop()            # a new question replaces the running one
    if not wifi_up:
        banner("VOICE", "Voice needs WiFi", AMBER, 2000)
        buzz(1, 80, 60)
        return
    if srv_addr is None:
        banner("VOICE", "Voice needs WiFi - no server found", AMBER, 2000)
        buzz(1, 80, 60)
        return
    if not voice_alloc():
        banner("VOICE", "Voice unavailable", AMBER, 2000)
        return
    voice_reset()
    wifi_fast(True)
    try:
        v_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        v_sock.setblocking(False)
        try:
            v_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        try:
            v_sock.connect((srv_ip, srv_voice))
        except OSError as e:
            if e.errno not in WOULD_BLOCK:
                raise
    except Exception:
        voice_fail("Server unreachable")
        return
    j = ('{"id":%s,"rate":%d,"fmt":"pcm16"}' % (jstr(dev_id), V_RATE)).encode()
    v_hdr = memoryview(bytes((72, len(j) >> 8, len(j) & 255)) + j)
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
            b[4:4 + V_BLANK] = V_ZEROM[:V_BLANK]
        pk = 0
        for k in range(2, V_CE, 16):
            x = a[k]
            if x < 0:
                x = -x
            if x > pk:
                pk = x
        lv = pk / 16000.0
        v_lvl = lv if lv > v_lvl else 0.6 * v_lvl + 0.4 * lv
        b[1] = 65                               # 'A'
        b[2] = (V_CS * 2) >> 8
        b[3] = (V_CS * 2) & 255
        v_tx.append(i)
    if v_state == V_LISTEN:
        while len(v_rec) < 2 and v_free:
            i = v_free.pop()
            try:
                M5.Mic.record(VH[i][2:], V_RATE)
            except Exception:
                v_free.append(i)
                break
            v_rec.append(i)


def v_send():
    """Push H, then the recorded chunks, then E. Returns an error string or None."""
    global v_cur, v_cur_off, v_cur_i, v_conn, v_hsent, v_esent
    for _ in range(8):
        if v_cur is None:
            if not v_hsent:
                v_cur, v_cur_i, v_hsent = v_hdr, -1, True
            elif v_tx:
                i = v_tx.pop(0)
                v_cur, v_cur_i = VB[i][1:], i
            elif v_state >= V_THINK and not v_rec and not v_esent:
                v_cur, v_cur_i, v_esent = V_E, -1, True
            else:
                return None
            v_cur_off = 0
        try:
            n = v_sock.send(v_cur[v_cur_off:])
        except OSError as e:
            if e.errno in WOULD_BLOCK or (not v_conn and e.errno == ENOTCONN):
                return None     # still connecting; V_CONNECT_MS bounds the wait
            return "Connection lost" if v_conn else "Server unreachable"
        if not n:
            return None
        v_conn = True
        v_cur_off += n
        if v_cur_off >= len(v_cur):
            if v_cur_i >= 0:
                v_free.append(v_cur_i)
            v_cur = None
    return None


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


def v_eof():
    global v_rx_done
    if v_rx_any:
        v_rx_done = True
        v_queue_fill()
        return None
    return "No reply"


def v_recv(now):
    """Parse reply frames. 'A' payloads are packed back to back into pool
    chunks so the speaker always gets full 100 ms buffers. Returns an error
    string or None."""
    global v_rh_n, v_rtype, v_rlen, v_rgot, v_fill, v_fill_n, v_rx_done, v_rx_any
    global v_last_rx, v_msg, v_state
    for _ in range(8):
        if v_rx_done:
            return None
        if v_rh_n < 3:
            r = v_sock.readinto(V_RHM[v_rh_n:])
            if r is None:
                return None
            if not r:
                return v_eof()
            v_rh_n += r
            if v_rh_n < 3:
                continue
            v_rtype, v_rlen, v_rgot = V_RH[0], (V_RH[1] << 8) | V_RH[2], 0
        if v_rgot < v_rlen:
            if v_rtype == 65:
                if v_fill < 0:
                    if not v_free:
                        return None     # pool full: let playback drain (TCP backpressure)
                    v_fill, v_fill_n = v_free.pop(), 0
                b = 4 + v_fill_n
                r = v_sock.readinto(VB[v_fill][b:b + min(v_rlen - v_rgot, V_CS * 2 - v_fill_n)])
            elif v_rgot < len(V_TXT):
                r = v_sock.readinto(V_TXTM[v_rgot:min(v_rlen, len(V_TXT))])
            else:
                r = v_sock.readinto(V_JUNK[:min(v_rlen - v_rgot, len(V_JUNK))])
            if r is None:
                return None
            if not r:
                return v_eof()
            v_rgot += r
            v_last_rx = now
            if v_rtype == 65:
                v_fill_n += r
                if v_fill_n >= V_CS * 2:
                    v_queue_fill()
            if v_rgot < v_rlen:
                continue
        v_rh_n = 0
        t = v_rtype
        if t == 65 or t == 84:                  # 'A', 'T'
            v_rx_any = True
            if t == 84:
                v_msg = clean_text(V_TXT, min(v_rlen, len(V_TXT)))
            if v_state == V_THINK:
                v_state = V_SPEAK
        elif t == 69:                           # 'E'
            v_rx_done = True
            v_queue_fill()
        elif t == 88:                           # 'X'
            return clean_text(V_TXT, min(v_rlen, len(V_TXT))) or "Assistant error"
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
            M5.Speaker.playRaw(VH[i][2:2 + V_NS[i]], V_RATE, False, 1, 0, False)
        except Exception:
            v_free.append(i)
            continue
        v_spk.append(i)
    if not v_play and not v_spk:
        v_playing = False


def voice_tick(now):
    global v_state, v_until, v_skip
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
    if v_state == V_LISTEN or v_rec:
        v_mic()
    if v_state >= V_THINK and not v_rec and aud == "mic":
        audio("spk")
        try:
            M5.Speaker.setVolume(V_VOL)
        except Exception:
            pass
    if not v_conn and el > V_CONNECT_MS:
        voice_fail("Server unreachable")
        return
    try:
        # once the reply is complete the server may have closed; stop sending
        err = None if v_rx_done else v_send()
        if err is None and v_conn:
            err = v_recv(now)
    except Exception:
        err = "Connection lost"
    if err:
        voice_fail(err)
        return
    v_playback(now)
    if v_rx_done and aud != "mic" and v_fill < 0 and not v_play and not v_spk:
        voice_reset()
        voice_free()            # mic is off and the speaker has drained channel 0
        wifi_fast(False)
        try:
            M5.Speaker.setVolume(SPK_VOL)
        except Exception:
            pass
        v_state = V_DONE
        v_until = time.ticks_add(now, 5000 if v_msg else 800)


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
    if ui in ("prompt", "escalated", "sos"):
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
    if ui in ("prompt", "escalated", "sos"):
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
                if ui in ("escalated", "sos"):
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
    return time.ticks_diff(now, link_ms) < 8000


def wifi_icon(x, now):
    """Three bars: faint = no wifi, amber sweep = connecting,
    amber = no server, green = linked. Lit bars follow RSSI once up."""
    ns = net_state(now)
    if ns == 0:
        lit, col = 0, FAINT
    elif ns == 1:
        lit, col = (now // 300) % 4, AMBER
    else:
        r = rssi if rssi is not None else -70
        lit = 3 if r > -60 else (2 if r > -72 else 1)
        col = GREEN if ns == 3 else AMBER
    for k in range(3):
        h = 3 + 3 * k
        cv.fillRect(x + 3 * k, 15 - h, 2, h, col if k < lit else 0x3A3A3A)


def header(now):
    cv.fillRect(0, 0, W, 20, 0x111111)
    font("Montserrat12")
    cv.setTextColor(0xFF7A1A, 0x111111)
    cv.drawString("HEATGUARD", 3, 4)
    wifi_icon(86, now)
    cv.fillCircle(98, 10, 3, GREEN if linked(now) else RED)
    cv.setTextColor(DIM, 0x111111)
    cv.drawRightString(("%d%%" % bat) if bat >= 0 else "--", W - 3, 4)


def fmt_mmss(ms):
    s = max(0, ms // 1000)
    return "%d:%02d" % (s // 60, s % 60)


def draw_main(now):
    cv.fillScreen(BLACK)
    header(now)
    ph = plan["phase"]
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
    if linked(now):
        tip = "A=SOS  hold B=talk" if ns == 3 else "hold A = SOS"
    else:
        tip = ("no wifi - local timer", "wifi connecting...", "no server - local timer")[min(ns, 2)]
    cv.setTextColor(FAINT if linked(now) else AMBER, BLACK)
    cv.drawCenterString(tip, W // 2, 222)


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
        ("id", dev_id[-8:]),
    ]
    y = 112
    for k, v in rows:
        cv.setTextColor(DIM, BLACK)
        cv.drawString(k, 6, y)
        cv.setTextColor(WHITE, BLACK)
        cv.drawRightString(v, W - 6, y)
        y += 16
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
        text_c("speak now" if v_conn else "connecting...", 170, DIM, BLACK, "Montserrat14")
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
                "inactivity": "NO MOVE", "unwell": "UNWELL"}


def draw_overlay(now):
    if ui == "prompt":
        flash = (now // 400) % 2 == 0
        bg = RED if (ui_kind == "fall" or flash) else 0x7F1D1D
        if ui_kind in ("tremor", "erratic", "inactivity"):
            bg = AMBER if flash else 0x78350F
        cv.fillScreen(bg)
        text_c(PROMPT_TITLE.get(ui_kind, ui_kind.upper()), 22, WHITE, bg, "Montserrat24")
        text_c("Are you OK?", 60, WHITE, bg, "Montserrat16")
        left = max(0, time.ticks_diff(ui_until, now)) // 1000 + 1
        text_c(str(left), 92, WHITE, bg, "DejaVu56")
        cv.fillRoundRect(10, 176, W - 20, 40, 10, WHITE)
        text_c("A = I'm OK", 188, bg, WHITE, "Montserrat16")
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
        if not firedA and time.ticks_diff(now, holdA) > 2000 and ui not in ("sos", "escalated"):
            firedA = True
            ev("sos")
            if v_state:
                voice_stop()
            set_ui("sos")
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
    if v_state and ui != "prompt":
        voice_stop()
    elif ui == "prompt":
        ev(ui_kind + "_ok")
        ui = "normal"           # banner() will not replace a prompt; without this it escalates anyway
        banner("THANKS", "Glad you're OK. Stay hydrated.", GREEN, 2500)
        still_ms = 0
    elif ui in ("escalated", "sos"):
        ev((ui_kind or "sos") + "_cancel")
        set_ui("normal")
    elif ui in ("ack", "msg", "banner"):
        set_ui("normal")


def set_ui(mode):
    global ui, ui_kind, ui_until, alert_n
    if mode == "sos":
        ui_kind = "sos"
    if mode in ("sos", "escalated"):
        alert_n = 1
    ui = mode
    ui_until = time.ticks_add(time.ticks_ms(), 3600000)


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


# ---------------------------------------------------------------- main
def main():
    global cv, FONTS, dev_id, am, gm, act, still_ms, wi, hop_n, spark_i
    global bat, chg, chip_t, T0, ev_seq, rssi
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
    import binascii
    dev_id = "stick-" + binascii.hexlify(machine.unique_id()).decode()[:8]
    ev_seq = seq_base()
    voice_init()
    wifi_init()
    try:
        gc.threshold(GC_THRESHOLD)
    except Exception:
        pass

    hello()
    batch = []
    T0 = t0 = time.ticks_ms()
    next_t = t0
    last_ui = last_st = last_hello = last_sec = last_net = gc_at = t0
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

        ax, ay, az = M5.Imu.getAccel()
        gx, gy, gz = M5.Imu.getGyro()
        a = math.sqrt(ax * ax + ay * ay + az * az)
        g = math.sqrt(gx * gx + gy * gy + gz * gz)
        am, gm = a, g

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

        if (ui == "normal" and plan["phase"] == "work"
                and still_ms >= P["INACT_S"] * 1000):
            prompt("inactivity", still_s=still_ms // 1000)
            still_ms = 0

        batch.append("[%.3f,%.3f,%.3f,%.1f,%.1f,%.1f]" % (ax, ay, az, gx, gy, gz))
        if len(batch) >= BATCH:
            out('{"k":"s","id":"%s","t0":%d,"dt":%d,"d":[%s]}'
                % (dev_id, time.ticks_diff(now, t0) - DT_MS * (BATCH - 1), DT_MS, ",".join(batch)))
            batch = []

        M5.update()
        buttons(now)
        read_host()
        udp_rx(now)
        ev_tick(now)
        if v_state:
            voice_safe(voice_tick, now)
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
            try:
                chip_t = float(esp32.mcu_temperature())
            except Exception:
                pass
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
                '"phase":"%s","ui":"%s","free":%d%s}'
                % (dev_id, chip_t, bat, chg, act, workload(), am, gm, trem_hz, trem_amp,
                   still_ms // 1000, plan["phase"], ui, gc.mem_free() // 1024, rs))
        if time.ticks_diff(now, last_hello) >= 10000:
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
