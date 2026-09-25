"""HeatGuard core: heat engine, alert lifecycle, the simulated fleet, Devin incident analysis,
WhatsApp and phone escalation, the voice assistant hooks, the dashboard API and SSE.

Runs inside the telemetry app (backend/telemetry/main.py): `router` is mounted there and
`startup()` / `shutdown()` run in its lifespan. Wearables reach it over the device WebSocket
(heatguard/device_ws.py) or as sticks3.telemetry.v1 frames on POST /v1/ingest/frames. The UDP
gateway, discovery beacon, USB serial bridge and TCP voice port only start with HEATGUARD_LAN=1.
"""
import asyncio
import json
import logging
import os
import random
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse

from . import devin as devin_mod
from . import heat, sim
from . import incidents as incidents_mod
from .gateway import UDP_PORT, SerialBridge, UdpGateway, beacon, lan_ips

log = logging.getLogger("heatguard")

STATIC = Path(__file__).resolve().parent / "static"


async def twilio_sandbox_sender(c):
    """The WhatsApp number our recipients joined, read from their inbound 'join' messages.
    Twilio gives each account its own sandbox number, so there is no safe default."""
    sid = os.environ["TWILIO_ACCOUNT_SID"]
    for to in filter(None, (x.strip() for x in os.environ.get("WHATSAPP_TO", "").split(","))):
        try:
            r = await c.get("https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % sid,
                            params={"From": "whatsapp:" + to, "PageSize": 1})
            msgs = r.json().get("messages") or []
            if msgs:
                return msgs[0]["to"]
        except Exception:
            pass
    return None


async def twilio_account_type(c):
    try:
        r = await c.get("https://api.twilio.com/2010-04-01/Accounts/%s.json" % os.environ["TWILIO_ACCOUNT_SID"])
        return r.json().get("type")
    except Exception:
        return None


try:
    from .services.whatsapp import WhatsApp
except Exception:  # service not present
    WhatsApp = None
try:
    from .services.assistant import Assistant
except Exception:
    Assistant = None
try:
    from .services.voice_call import VoiceCall
except Exception:
    VoiceCall = None

LAN = os.environ.get("HEATGUARD_LAN", "0") == "1"             # UDP gateway, beacon, USB serial, TCP voice
WEATHER = os.environ.get("HEATGUARD_WEATHER", "1") != "0"      # Open-Meteo; 0 = fallback values
HTTP_PORT = int(os.environ.get("PORT", "8000"))
FLEET_SIZE = int(os.environ.get("HEATGUARD_FLEET", "1200"))
AUTO_DEVIN = os.environ.get("HEATGUARD_AUTO_DEVIN", "1") != "0"
AUTO_WHATSAPP = os.environ.get("HEATGUARD_AUTO_WHATSAPP", "1") != "0"
SIM_RATE_S = float(os.environ.get("HEATGUARD_SIM_EVERY_S", "35"))
OFFLINE_S = 10
# Chip-temperature calibration (2026-09-25, StickS3 on WiFi, screen on): chip 60 °C read
# with 25 °C air, so air at the wrist ≈ chip - 35. The chip is not a body sensor; these
# are air-temperature triggers for hot pockets the site-wide WBGT misses.
CHIP_OFFSET = float(os.environ.get("HEATGUARD_CHIP_OFFSET", "35"))
CHIP_STOP = float(os.environ.get("HEATGUARD_CHIP_STOP", "70"))    # ≈35 °C air: stop work
CHIP_CALL = float(os.environ.get("HEATGUARD_CHIP_CALL", "80"))    # ≈45 °C air: call for help
TEMP_SUSTAIN_S = float(os.environ.get("HEATGUARD_TEMP_SUSTAIN_S", "8"))
RING_S = 60

SITE_BASE = {
    "name": os.environ.get("HEATGUARD_SITE_NAME", "Dubai South · Tower B site (demo)"),
    "lat": float(os.environ.get("HEATGUARD_LAT", "24.8962")),
    "lon": float(os.environ.get("HEATGUARD_LON", "55.1602")),
}
LIVE_PROFILE = {
    "name": os.environ.get("HEATGUARD_WORKER_NAME", "Ravi Kumar"),
    "trade": os.environ.get("HEATGUARD_WORKER_TRADE", "Steel fixer"),
    "zone": os.environ.get("HEATGUARD_WORKER_ZONE", "Tower B · Level 4"),
    "crew": os.environ.get("HEATGUARD_WORKER_CREW", "B-2"),
}
FALLBACK_WEATHER = {"temp_c": 38.0, "rh": 45.0, "wind_ms": 3.0, "solar_wm2": 600.0,
                    "apparent_c": 43.0, "source": "fallback"}

STATUS_CHAR = {"ok": "o", "caution": "c", "rest": "r", "alert": "a", "offline": "x"}
SEV_RANK = {"info": 0, "warning": 1, "critical": 2}
CLOSED = ("resolved", "false_alarm")
TITLES = {"fall": "Fall detected", "impact": "Hard impact", "tremor": "Unusual shaking",
          "erratic": "Erratic movement", "inactivity": "No movement", "sos": "SOS pressed",
          "unwell": "Feeling unwell"}
WINDOW_TYPES = ("fall", "impact", "tremor", "erratic", "inactivity", "sos")
# HeatGuard alert type <-> incident type (heatguard.incident.v1)
INCIDENT_TYPE = {"fall": "fall", "sos": "manual_sos", "tremor": "tremor", "erratic": "other",
                 "inactivity": "inactivity", "unwell": "unwell", "impact": "impact",
                 "heat_critical": "heat_stroke"}
ALERT_TYPE = {"fall": "fall", "heat_stroke": "heat_critical", "manual_sos": "sos", "tremor": "tremor",
              "inactivity": "inactivity", "unwell": "unwell", "impact": "impact", "other": "other"}
INCIDENT_TITLES = {"heat_stroke": "Suspected heat stroke", "manual_sos": "SOS", "other": "Incident reported"}
INCIDENT_CLOSED = ("worker_ok", "cancelled", "resolved")


def public(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


class Core:
    def __init__(self):
        self.loop = None
        self.subs = set()
        self.rng = random.Random()
        self.config = {"timewarp": 1, "sim_events": True}
        self.site = dict(SITE_BASE)
        self.weather = None
        self.override = None
        self._site_sig = None
        self.live = {}        # device id -> worker
        self.by_wid = {}      # worker id -> worker (live)
        self.fleet = sim.make_fleet(FLEET_SIZE)
        for w in self.fleet:
            w["_offline"] = w.pop("offline")
            w["_offline_until"] = 0
            w["_alerts"] = []
            w["status"], w["risk"] = "ok", 0
        self.fleet_idx = {w["id"]: w for w in self.fleet}
        self.alerts = OrderedDict()
        self.alert_seq = 0
        self.voice = OrderedDict()
        self.notifications = OrderedDict()
        self.devin = devin_mod.Devin()
        self.whatsapp = None          # created in setup_escalation() at startup
        self.caller = VoiceCall() if VoiceCall else None
        self.assistant = None
        self.udp = UdpGateway(self.on_device_msg)
        self.bridge = None
        self.ips = []
        self._last_auto_devin = 0.0
        self._jobs = set()            # background jobs (Devin polling, escalation, trace capture)
        self.service = None           # the telemetry app's TelemetryService (incidents table)
        self._incident_lock = None
        self.compute_site()

    def spawn(self, coro):
        """Run a background job on the app's event loop; shutdown() cancels what is left."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:          # called from another thread
            if self.loop is None or self.loop.is_closed():
                coro.close()
                return None
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        t = loop.create_task(coro)
        self._jobs.add(t)
        t.add_done_callback(self._jobs.discard)
        return t

    async def setup_escalation(self):
        """Pick the WhatsApp provider/sender and the call's From number (Twilio lookups)."""
        if os.environ.get("TWILIO_ACCOUNT_SID") and os.environ.get("TWILIO_AUTH_TOKEN"):
            auth = (os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"])
            async with httpx.AsyncClient(timeout=6, auth=auth) as c:
                # Credentials alone pick Twilio for WhatsApp, except on a trial account, which can
                # only send Twilio's own templates: free-text alerts stay a dry run there.
                if "WHATSAPP_PROVIDER" not in os.environ:
                    trial = await twilio_account_type(c) == "Trial" and not os.environ.get("TWILIO_CONTENT_SID")
                    os.environ["WHATSAPP_PROVIDER"] = "dryrun" if trial else "twilio"
                if not os.environ.get("TWILIO_WHATSAPP_FROM"):
                    frm = await twilio_sandbox_sender(c)
                    if frm:
                        os.environ["TWILIO_WHATSAPP_FROM"] = frm
        try:
            self.whatsapp = WhatsApp() if WhatsApp else None
        except Exception as e:
            log.error("whatsapp service failed to load: %s", e)
            self.whatsapp = None
        if self.caller:
            try:
                await self.caller.setup()
            except Exception as e:
                log.error("call service setup failed: %s", e)
        self.publish("config", self.config_view())

    # ------------------------------------------------------------ pub/sub
    def publish(self, event, data):
        msg = "event: %s\ndata: %s\n\n" % (event, json.dumps(data, separators=(",", ":"), default=str))
        for q in list(self.subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    # ------------------------------------------------------------ site / weather
    def compute_site(self):
        src = self.override or self.weather or FALLBACK_WEATHER
        wb = heat.wbgt_estimate(src["temp_c"], src["rh"], src.get("solar_wm2", 0), src.get("wind_ms", 1))
        now = datetime.now(heat.TZ)
        self.site.update({
            "temp_c": round(src["temp_c"], 1), "rh": round(src["rh"]), "wind_ms": round(src.get("wind_ms", 0), 1),
            "solar_wm2": round(src.get("solar_wm2", 0)), "apparent_c": src.get("apparent_c"),
            "source": src["source"], "wbgt_c": wb, "category": heat.category(wb), "policy": heat.policy(wb),
            "midday_break": heat.midday_break(now), "local_time": now.strftime("%H:%M"),
            "updated": time.time(),
        })
        self.zone_heat = {}
        for zone, sun in sim.ZONE_SUN.items():
            zw = heat.wbgt_estimate(src["temp_c"], src["rh"], src.get("solar_wm2", 0) * sun, src.get("wind_ms", 1))
            self.zone_heat[zone] = {"zone": zone, "sun": sun, "wbgt_c": zw,
                                    "category": heat.category(zw), "policy": heat.policy(zw)}
        self.site["zones"] = list(self.zone_heat.values())
        sig = (wb, src["source"], self.site["midday_break"]["active"], self.site["local_time"])
        changed = sig != self._site_sig
        self._site_sig = sig
        return changed

    async def weather_loop(self):
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
               "&current=temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,shortwave_radiation"
               "&wind_speed_unit=ms&timezone=Asia%%2FDubai") % (SITE_BASE["lat"], SITE_BASE["lon"])
        while True:
            try:
                async with httpx.AsyncClient(timeout=15) as c:
                    cur = (await c.get(url)).json()["current"]
                self.weather = {"temp_c": cur["temperature_2m"], "rh": cur["relative_humidity_2m"],
                                "wind_ms": cur["wind_speed_10m"], "solar_wm2": cur["shortwave_radiation"],
                                "apparent_c": cur["apparent_temperature"], "source": "open-meteo"}
                if self.compute_site():
                    self.publish("site", self.site)
                await asyncio.sleep(600)
            except Exception:
                await asyncio.sleep(60)

    # ------------------------------------------------------------ workers
    def add_live(self, did):
        wid = "LIVE-" + did.split("-")[-1][:8]
        w = {
            "id": wid, "device_id": did, **LIVE_PROFILE, "live": True, "acclimatized": True,
            "status": "ok", "risk": 0, "workload": "light", "phase": "work", "phase_reason": "",
            "work_elapsed_min": 0.0, "work_budget_min": 45, "rest_remaining_min": 0.0, "rest_required_min": 15,
            "shift_exposure_min": 0.0, "device_temp_c": None, "battery": None, "last_event": None,
            "wrist_temp_c": None, "temp_level": "ok",
            "open_alerts": 0, "updated": time.time(),
            "motion": {"am": 1.0, "gm": 0.0, "act": 0.0, "trem_hz": 0.0, "trem_amp": 0.0, "still_s": 0},
            "transport": None,
            "_last_seen": 0.0, "_transport": None, "_addr": None, "_params": {}, "_fw": None,
            "_ring": deque(maxlen=50 * RING_S), "_act": 0.0, "_alerts": [], "_seqs": deque(maxlen=300),
            "_pending": [], "_viol": 0, "_viol_sent": False, "_offline_alert": None, "_plan_at": 0.0,
            "_temp_ema": None, "_stop_since": 0.0, "_call_since": 0.0, "_temp_stop": False, "_temp_called": False,
            "configuration": None, "_ip": None,
            # telemetry frames: boot, last sample, last sensor values, next imu publish time
            "_boot": None, "_last_t": None, "_last_acc": None, "_last_gyr": None, "_imu_next": None,
        }
        self.live[did] = w
        self.by_wid[wid] = w
        self.publish("config", self.config_view())
        return w

    def find_worker(self, wid):
        return self.by_wid.get(wid) or self.fleet_idx.get(wid)

    def worker_alerts(self, w):
        w["_alerts"] = [i for i in w["_alerts"] if i in self.alerts]
        return [self.alerts[i] for i in w["_alerts"]]

    def is_offline(self, w, now):
        return (now - w["_last_seen"] > OFFLINE_S) if w.get("live") else w["_offline"]

    def refresh_status(self, w, now):
        open_ = [a for a in self.worker_alerts(w)
                 if a["state"] not in CLOSED and a["severity"] in ("critical", "warning")]
        w["open_alerts"] = len(open_)
        budget, el = w["work_budget_min"], w["work_elapsed_min"]
        if self.is_offline(w, now):
            w["status"] = "offline"
        elif open_:
            w["status"] = "alert"
        elif w["phase"] in ("rest", "stop"):
            w["status"] = "rest"
        elif budget and el / budget >= 0.8:
            w["status"] = "caution"
        else:
            w["status"] = "ok"
        lvl = self.heat_for_zone(w.get("zone"))["category"]["level"]
        r = lvl * 8
        if w["phase"] == "work" and budget:
            r += min(1.2, el / budget) * 40
        elif w["phase"] == "stop":
            r += 30
        else:
            r += 5
        r += 0 if w["acclimatized"] else 8
        r += {"heavy": 8, "moderate": 4}.get(w["workload"], 0)
        r += min(10, w["shift_exposure_min"] / 60 * 1.5)
        top = max((SEV_RANK[a["severity"]] for a in open_), default=0)
        if top == 2:
            r = max(r, 95)
        elif top == 1:
            r = max(r, 70)
        w["risk"] = int(max(0, min(100, r)))

    # ------------------------------------------------------------ heat engine
    def heat_step(self, w, dt_min):
        """Advance one worker's work/rest cycle. Returns the previous phase if it changed."""
        s = self.site
        z = self.heat_for_zone(w.get("zone"))
        wb = w["wbgt_c"] = z["wbgt_c"]
        pol = z["policy"]["acclimatized" if w["acclimatized"] else "unacclimatized"][w["workload"]]
        work, rest = pol["work"], pol["rest"]
        w["work_budget_min"], w["rest_required_min"] = work, rest
        prev = w["phase"]
        reason = w["phase_reason"] or ""
        if w.get("_temp_stop"):
            w["phase"], w["rest_remaining_min"] = "stop", 0.0
            w["phase_reason"] = "Wrist about %.0f °C: stop work" % (w.get("wrist_temp_c") or 0)
            return prev if prev != "stop" else None
        if s["midday_break"]["active"]:
            now = datetime.now(heat.TZ)
            end = now.replace(hour=15, minute=0, second=0, microsecond=0)
            w["phase"], w["phase_reason"] = "rest", "UAE midday break 12:30–15:00"
            w["rest_remaining_min"] = max(0.0, (end - now).total_seconds() / 60)
        elif work == 0:
            w["phase"], w["phase_reason"] = "stop", "WBGT %.1f °C: too hot for %s work" % (wb, w["workload"])
            w["rest_remaining_min"] = 0.0
        elif prev == "stop" or (prev == "rest" and reason.startswith("UAE midday")):
            w["phase"], w["phase_reason"] = "rest", "Cool-down before restart"
            w["rest_remaining_min"] = float(rest)
        elif prev == "work":
            w["work_elapsed_min"] += dt_min
            w["shift_exposure_min"] += dt_min
            if w["work_elapsed_min"] >= work:
                w["phase"], w["rest_remaining_min"] = "rest", float(rest)
                w["phase_reason"] = "%d min work limit at WBGT %.1f °C" % (work, wb)
        else:  # rest
            w["rest_remaining_min"] -= dt_min
            if w["rest_remaining_min"] <= 0:
                w["phase"], w["phase_reason"] = "work", ""
                w["work_elapsed_min"], w["rest_remaining_min"] = 0.0, 0.0
        return prev if prev != w["phase"] else None

    def location_of(self, w):
        """Best location we have for a worker: their zone's centre on the site."""
        zone = (w or {}).get("zone") or ""
        dlat, dlon = next((off for name, off in sim.ZONE_OFFSET.items() if zone.startswith(name)), (0.0, 0.0))
        lat, lon = round(SITE_BASE["lat"] + dlat, 5), round(SITE_BASE["lon"] + dlon, 5)
        return {"lat": lat, "lon": lon, "label": " · ".join(x for x in (zone, self.site["name"]) if x),
                "maps_url": "https://maps.google.com/?q=%s,%s" % (lat, lon), "source": "zone"}

    def heat_for_zone(self, zone):
        zone = zone or ""
        for name, z in self.zone_heat.items():
            if zone.startswith(name):
                return z
        return {"wbgt_c": self.site["wbgt_c"], "category": self.site["category"], "policy": self.site["policy"]}

    def cmd_rest(self, w, mins, reason):
        prev = w["phase"]
        w["phase"], w["rest_remaining_min"], w["phase_reason"] = "rest", float(mins), reason
        if w.get("live"):
            self.send_cmd(w, {"cmd": "rest", "mins": mins, "reason": reason})
            if prev != "rest":
                self.on_phase_change(w, prev)
            self.send_plan(w)

    def cmd_resume(self, w):
        prev = w["phase"]
        w["phase"], w["phase_reason"] = "work", ""
        w["work_elapsed_min"], w["rest_remaining_min"] = 0.0, 0.0
        if w.get("live"):
            self.send_cmd(w, {"cmd": "resume"})
            if prev != "work":
                self.on_phase_change(w, prev)
            self.send_plan(w)

    def on_phase_change(self, w, prev):
        """Live workers: a cool-down is shown as an alert card until work resumes."""
        w["_viol"], w["_viol_sent"] = 0, False
        for a in self.worker_alerts(w):
            if a["type"] == "heat_limit" and a["state"] not in CLOSED and a["id"] != w.get("_temp_stop_alert"):
                self.update_alert(a, state="resolved")
        if w["phase"] in ("rest", "stop"):
            stop = w["phase"] == "stop"
            if stop and w.get("_temp_stop"):
                return                          # temp_rules already raised and notified this one
            self.new_alert(w, "heat_limit", "warning" if stop else "info",
                           "Stop work: heat" if stop else "Cool-down started",
                           "%s. %s" % (w["phase_reason"], "Worker told to rest in shade." if stop else
                                       "Wearable is counting down %d min in shade." % round(w["rest_remaining_min"])),
                           source="server")

    # ------------------------------------------------------------ device transport
    def send_cmd(self, w, obj):
        t = w.get("_transport")
        if t == "ws":                       # device WebSocket: queued on that device's socket
            if w["_addr"] is not None:
                w["_addr"].send(obj)
        elif t == "wifi":
            self.udp.send(w["_addr"], obj)
        elif t == "serial" and self.bridge:
            self.bridge.send(obj)
        elif t == "http":                   # HTTP frames have no downlink; kept for the record
            w["_pending"] = (w["_pending"] + [obj])[-20:]

    @staticmethod
    def _socket_open(w):
        return w.get("_transport") == "ws" and bool(getattr(w.get("_addr"), "open", False))

    def _heard(self, w, transport, addr, now):
        """A live worker's device was heard from: last seen, transport, back online."""
        was_offline = now - w["_last_seen"] > OFFLINE_S
        if transport != "http" or not self._socket_open(w):   # an open socket keeps the downlink
            w["_transport"], w["_addr"] = transport, addr
            w["transport"] = transport
        w["_last_seen"] = now
        if was_offline:
            self.on_back_online(w)

    def send_plan(self, w):
        z = self.heat_for_zone(w.get("zone"))
        w["_plan_at"] = time.time()
        self.send_cmd(w, {
            "cmd": "plan", "wbgt": z["wbgt_c"], "work": w["work_budget_min"], "rest": w["rest_required_min"],
            "elapsed": round(w["work_elapsed_min"], 2), "phase": w["phase"],
            "remaining": round(max(0.0, w["rest_remaining_min"]), 2), "level": z["category"]["level"],
            "label": z["category"]["label"], "warp": self.config["timewarp"], "reason": w["phase_reason"][:40],
        })

    def on_device_msg(self, obj, transport, addr):
        did = obj.get("id")
        if not did:
            return
        w = self.live.get(did) or self.add_live(did)
        self._heard(w, transport, addr, time.time())
        k = obj.get("k")
        if k == "hello":
            w["_params"] = obj.get("params") or {}
            w["_fw"] = obj.get("fw")
            w["_ip"] = obj.get("ip")
        # Raw IMU is no longer a HeatGuard message: telemetry now comes as sticks3.telemetry.v1
        # frames (on_frames). Old handler kept for reference:
        # elif k == "s":
        #     t0, dt = int(obj.get("t0", 0)), int(obj.get("dt", 20))
        #     samples = [[t0 + i * dt] + list(row) for i, row in enumerate(obj.get("d") or [])]
        #     w["_ring"].extend(samples)
        #     self.publish("imu", {"id": w["id"], "samples": samples})
        elif k == "st":
            self.on_status(w, obj)
        elif k == "ev":
            self.on_device_event(w, obj)

    IMU_PUBLISH_MS = 40          # SSE imu: at most one sample per 40 ms of device time (25 Hz)

    def on_frames(self, frames, transport, addr=None):
        """sticks3.telemetry.v1 frames (device WebSocket or POST /v1/ingest/frames), already
        validated and stored by the telemetry service, into the live pipeline: worker, ring
        buffer for fall traces, SSE imu at <= 25 Hz, wrist-temperature tiers. Never raises."""
        try:
            by_dev = {}
            for f in frames:
                by_dev.setdefault(f.device_id, []).append(f)
            now = time.time()
            for did, fs in by_dev.items():
                w = self.live.get(did) or self.add_live(did)
                self._heard(w, transport, addr, now)
                self._frames(w, fs)
        except Exception:
            log.exception("heatguard: on_frames failed")

    def _frames(self, w, fs):
        if all(f.boot_id == fs[0].boot_id for f in fs):
            fs = sorted(fs, key=lambda f: f.read_time_us)
        ring, pub, temps = w["_ring"], [], []
        for f in fs:
            if f.boot_id != w["_boot"]:               # new boot: its clock restarts near zero
                w["_boot"], w["_last_t"], w["_imu_next"] = f.boot_id, None, None
                ring.clear()
            if f.configuration is not None:
                w["configuration"] = f.configuration.model_dump(exclude_none=True)
            imu = f.imu
            if f.fresh.temperature and imu.die_temperature_c is not None:
                temps.append(imu.die_temperature_c)
            # A sensor that is not fresh repeats its last value.
            if f.fresh.accelerometer and imu.acceleration_g is not None:
                w["_last_acc"] = imu.acceleration_g.as_tuple()
            if f.fresh.gyroscope and imu.angular_velocity_dps is not None:
                w["_last_gyr"] = imu.angular_velocity_dps.as_tuple()
            acc = w["_last_acc"] or (imu.acceleration_g.as_tuple() if imu.acceleration_g else None)
            gyr = w["_last_gyr"] or (imu.angular_velocity_dps.as_tuple() if imu.angular_velocity_dps
                                     else (0.0, 0.0, 0.0))
            t = f.read_time_us / 1000
            if acc is None or (w["_last_t"] is not None and t <= w["_last_t"]):
                continue                               # no accel yet, or a duplicate / late frame
            w["_last_t"] = t
            s = [t, *acc, *gyr]
            ring.append(s)
            nxt = w["_imu_next"]
            if nxt is None or t >= nxt:
                pub.append(s)
                nxt = t if nxt is None else nxt
                while nxt <= t:
                    nxt += self.IMU_PUBLISH_MS
                w["_imu_next"] = nxt
        if pub:
            self.publish("imu", {"id": w["id"], "samples": pub})
        for c in temps:
            w["device_temp_c"] = c
            self.temp_rules(w, float(c))
        w["updated"] = time.time()

    def on_back_online(self, w):
        a = w.get("_offline_alert")
        if a and a in self.alerts and self.alerts[a]["state"] not in CLOSED:
            self.update_alert(self.alerts[a], state="resolved",
                              message=self.alerts[a]["message"] + " Back online.")
        w["_offline_alert"] = None
        self.send_cmd(w, {"cmd": "hello"})
        self.send_plan(w)
        self.publish("config", self.config_view())

    def on_status(self, w, st):
        w["device_temp_c"] = st.get("temp", w["device_temp_c"])
        if st.get("temp") is not None:
            self.temp_rules(w, float(st["temp"]))
        w["battery"] = st.get("bat", w["battery"])
        act = float(st.get("act") or 0.0)
        w["_act"] += 0.15 * (act - w["_act"])
        w["workload"] = heat.workload_from_activity(w["_act"])
        w["motion"] = {k: st.get(k) for k in ("am", "gm", "act", "trem_hz", "trem_amp", "still_s", "rssi")}
        w["motion"]["ui"] = st.get("ui")
        w["updated"] = time.time()
        # still hauling rebar in the middle of a mandatory cool-down?
        if w["phase"] == "rest" and act > 0.12:
            w["_viol"] += 1
        else:
            w["_viol"] = max(0, w["_viol"] - 1)
        if w["_viol"] >= 15 and not w["_viol_sent"]:
            w["_viol_sent"] = True
            self.new_alert(w, "rest_violation", "warning", "Working during cool-down",
                           "Wearable shows %s activity %d min into a mandatory cool-down." %
                           (w["workload"], round(w["rest_required_min"] - w["rest_remaining_min"])), source="server")
            self.send_cmd(w, {"cmd": "msg", "text": "You are on a cool-down. Stop work, sit in the shade, drink water."})

    def temp_rules(self, w, chip):
        """Calibrated wrist-temperature tiers: >= CHIP_STOP stop work, >= CHIP_CALL call for help.
        Readings are whole degrees with +-1 jitter, so smooth, require TEMP_SUSTAIN_S above the
        line to trigger and 2-3 degrees below it to clear."""
        now = time.time()
        ema = w["_temp_ema"] = chip if w["_temp_ema"] is None else w["_temp_ema"] + 0.3 * (chip - w["_temp_ema"])
        est = round(ema - CHIP_OFFSET, 1)
        w["wrist_temp_c"] = est
        w["temp_level"] = "call" if ema >= CHIP_CALL else ("stop" if ema >= CHIP_STOP else "ok")

        if ema >= CHIP_STOP:
            w["_stop_since"] = w["_stop_since"] or now
            if not w["_temp_stop"] and now - w["_stop_since"] >= TEMP_SUSTAIN_S:
                w["_temp_stop"] = True          # heat_step turns this into phase 'stop' on the next tick
                a = self.new_alert(w, "heat_limit", "warning", "Stop work: wrist temperature",
                                   "Wearable reads about %.0f °C air at the wrist (chip %.0f °C, stop line %.0f °C). "
                                   "Worker told to stop and cool down in the shade."
                                   % (est, chip, CHIP_STOP - CHIP_OFFSET), source="device",
                                   detail={"chip_c": chip, "wrist_c": est, "line_c": CHIP_STOP - CHIP_OFFSET})
                w["_temp_stop_alert"] = a["id"]
                self.send_cmd(w, {"cmd": "buzz"})
                if AUTO_WHATSAPP:                   # supervisor notification
                    self.spawn(self.notify_alert(a["id"], "auto"))
        elif ema < CHIP_STOP - 2:
            w["_stop_since"] = 0.0
            if w["_temp_stop"]:
                w["_temp_stop"] = False
                aid = w.pop("_temp_stop_alert", None)
                if aid in self.alerts and self.alerts[aid]["state"] not in CLOSED:
                    self.update_alert(self.alerts[aid], state="resolved",
                                      message=self.alerts[aid]["message"] + " Cooled to about %.0f °C." % est)

        if ema >= CHIP_CALL:
            w["_call_since"] = w["_call_since"] or now
            if not w["_temp_called"] and now - w["_call_since"] >= TEMP_SUSTAIN_S:
                w["_temp_called"] = True
                a = self.new_alert(w, "heat_critical", "critical", "Heat emergency at the wrist",
                                   "Wearable reads about %.0f °C air at the wrist (chip %.0f °C, call line %.0f °C). "
                                   "Possible heat stroke: stop, shade, cool with water. Calling the emergency contact."
                                   % (est, chip, CHIP_CALL - CHIP_OFFSET), source="device",
                                   detail={"chip_c": chip, "wrist_c": est, "line_c": CHIP_CALL - CHIP_OFFSET})
                self.send_cmd(w, {"cmd": "msg", "text": "Extreme heat. Stop work, sit in the shade, pour water on "
                                                        "your head and neck. Help is on the way."})
                self.escalate(a)
                self.record_incident(a, w, itype="heat_stroke", source="server",
                                     details={"chip_c": chip, "wrist_c": est, "line_c": CHIP_CALL - CHIP_OFFSET})
        elif ema < CHIP_CALL - 3:
            w["_call_since"] = 0.0
            w["_temp_called"] = False

    def on_device_event(self, w, obj):
        seq, t = obj.get("seq"), obj.get("t")
        if seq is not None:
            self.send_cmd(w, {"cmd": "evack", "seq": seq})
            if (seq, t) in w["_seqs"]:
                return
            w["_seqs"].append((seq, t))
        typ = str(obj.get("type") or "")
        d = obj.get("detail") or {}
        now = time.time()
        base, _, suffix = typ.partition("_")
        if suffix in ("ok", "noresp", "cancel"):
            recent = [x for x in self.worker_alerts(w) if x["type"] == base and now - x["ts"] < 900]
            if suffix == "cancel":
                # the worker cancelled from the ALERT SENT screen: that covers every open one
                for x in recent:
                    if x["state"] not in CLOSED:
                        x["timeline"].append({"type": typ, "ts": now})
                        self.update_alert(x, state="resolved", message=x["message"] + " Worker cancelled the alarm.")
                        self.record_incident(x, w, status="cancelled")
                return
            a = recent[-1] if recent else None
            if not a:
                return
            a["timeline"].append({"type": typ, "ts": now})
            title = TITLES.get(base, base.title())
            if suffix == "ok":
                self.update_alert(a, state="resolved", severity="info", title=title + " · worker OK",
                                  message=a["message"].split(" Asking")[0] + " Worker answered “I'm OK”.")
            elif suffix == "cancel":
                self.update_alert(a, state="resolved", message=a["message"] + " Worker cancelled the alarm.")
            else:
                self.update_alert(a, state="open", severity="critical", title=title + " · NO RESPONSE",
                                  message=a["message"].split(" Asking")[0] +
                                  " No answer to the on-wrist prompt. Send help.")
                self.escalate(a)
            self.record_incident(a, w, status={"ok": "worker_ok", "cancel": "cancelled"}.get(suffix, "no_response"))
            return

        msg = {
            "fall": lambda: ("critical", ("Free-fall %d ms, impact %.1f g, " % (d.get("freefall_ms", 0), d.get("impact_g", 0))
                                          if d.get("freefall_ms") else "Hard impact %.1f g, " % d.get("impact_g", 0))
                             + ("then no movement." if d.get("still", 1) else "then movement (may be trying to get up).")
                             + " Asking the worker if they're OK."),
            "impact": lambda: ("info", "Impact %.1f g but the worker kept moving." % d.get("impact_g", 0)),
            "tremor": lambda: ("warning", "Rhythmic %.1f Hz shaking at %d °/s for %d s (tremor/seizure-like). Asking the worker." %
                               (d.get("hz", 0), d.get("rms_dps", 0), d.get("dur_s", 0))),
            "erratic": lambda: ("warning", "Irregular high-energy movement (%d °/s) for %d s. Asking the worker." %
                                (d.get("rms_dps", 0), d.get("dur_s", 0))),
            "inactivity": lambda: ("warning", "No movement for %d min during a work cycle. Asking the worker." %
                                   max(1, round(d.get("still_s", 0) / 60))),
            "sos": lambda: ("critical", "Worker held the SOS button on the wearable."),
            "unwell": lambda: ("warning", "Worker reported feeling unwell. Cool-down started."),
        }.get(base)
        if not msg:
            return
        sev, text = msg()
        a = self.new_alert(w, base, sev, TITLES[base], text, detail=d, event_t_ms=t,
                           state="resolved" if base == "impact" else "open")
        if base in WINDOW_TYPES and t is not None:
            self.spawn(self.capture_window(a, w, int(t)))
        if base == "sos":
            self.escalate(a)
        elif base == "unwell":
            self.cmd_rest(w, 30, "Reported unwell")
        elif base in ("tremor", "erratic"):
            a["_auto_devin"] = True
        boot = w.get("_boot")
        self.record_incident(a, w, itype=INCIDENT_TYPE[base], details=d, source="device",
                             incident_id="%s-%s-%s" % (w["device_id"], boot, seq) if boot and seq is not None else None,
                             boot_id=boot, read_time_us=int(t) * 1000 if isinstance(t, (int, float)) and boot else None)

    def escalate(self, a):
        """Critical and unanswered: WhatsApp the HSE team and get Devin's second opinion.
        Returns the channels used (also kept on the alert for its incident record)."""
        out = []
        if AUTO_WHATSAPP:
            self.spawn(self.notify_alert(a["id"], "auto"))
            if self.caller and a["severity"] == "critical" and not a.get("simulated"):
                out.append("call")
            if self.whatsapp:
                out.append("whatsapp")
        if a["type"] in ("fall", "tremor", "erratic", "inactivity"):
            a["_auto_devin"] = True
            if AUTO_DEVIN:
                out.append("devin")
            if a["has_window"]:
                self.maybe_auto_devin(a)
        a["_escalated"] = sorted(set(a.get("_escalated") or []) | set(out))
        a["_escalated_once"] = True
        return out

    # ------------------------------------------------------------ incidents (heatguard.incident.v1)
    @staticmethod
    def person_of(w, device_id):
        if not w:
            return {"name": device_id}
        return {"name": w.get("name"), "worker_id": w["id"], "trade": w.get("trade"),
                "crew": w.get("crew"), "zone": w.get("zone")}

    def prepare_incident(self, inc):
        """Defaults for a POSTed incident: the person and zone of the worker wearing that device."""
        w = self.live.get(inc.device_id) or self.add_live(inc.device_id)
        upd = {}
        if inc.person is None:
            upd["person"] = self.person_of(w, inc.device_id)
        if inc.location is None:
            upd["location"] = self.location_of(w)
        return inc.model_copy(update=upd) if upd else inc

    def record_incident(self, a, w, itype=None, status=None, details=None, source="server",
                        incident_id=None, boot_id=None, read_time_us=None):
        """Write HeatGuard's own detection (or a status change of it) to the incidents table,
        linked to alert `a`. Live wearables only: simulated workers are not people."""
        if self.service is None or not w or not w.get("live"):
            return
        new = not a.get("incident_id")
        itype = itype or a.get("_incident_type") or INCIDENT_TYPE.get(a["type"], "other")
        if new:
            a["incident_id"] = incident_id or "%s-%s" % (w["device_id"], uuid.uuid4().hex[:12])
            a["_incident_type"] = itype
            self.publish("alert", public(a))
        try:
            inc = incidents_mod.IncidentIn(
                schema=incidents_mod.SCHEMA, incident_id=a["incident_id"], device_id=w["device_id"],
                type=itype, status=status, details=details,
                severity=a["severity"] if new or status not in INCIDENT_CLOSED else None,
                person=self.person_of(w, w["device_id"]) if new else None,
                location=self.location_of(w) if new else None,
                source=source if new else None, boot_id=boot_id, read_time_us=read_time_us)
        except Exception:
            log.exception("heatguard: bad incident for alert %s", a["id"])
            return
        self.spawn(self._store_incident(inc, {"alert_id": a["id"], "escalated": a.get("_escalated") or []}))

    async def _store_incident(self, inc, link):
        if self._incident_lock is None:
            self._incident_lock = asyncio.Lock()
        async with self._incident_lock:        # FIFO: a status update never overtakes its create
            try:
                await incidents_mod.record(self.service.pool, inc, self.service.registry, link=link)
            except Exception:
                log.exception("heatguard: storing incident %s failed", inc.incident_id)

    async def on_incident(self, inc, created, prev):
        """Hook for POST /v1/incidents: raise or update the alert, close it on worker_ok /
        cancelled / resolved, and escalate a critical incident once."""
        did, status, sev = inc["device_id"], inc["status"], inc["severity"]
        w = self.live.get(did) or self.add_live(did)
        a = self.alerts.get(inc.get("alert_id") or "")
        details = inc.get("details") or {}
        atype = ALERT_TYPE.get(inc["type"], "other")
        title = INCIDENT_TITLES.get(inc["type"]) or TITLES.get(atype) or "Incident reported"
        if a is None:
            text = details.get("message") or "%s reported by %s." % (title, inc.get("source") or "device")
            a = self.new_alert(w, atype, sev, title, str(text)[:300],
                               source=inc.get("source") or "device", detail=details)
            a["incident_id"], a["_incident_type"] = inc["incident_id"], inc["type"]
            if inc.get("read_time_us") is not None and inc.get("boot_id") and inc["boot_id"] == w.get("_boot"):
                a["_event_t"] = inc["read_time_us"] // 1000
                self.spawn(self.capture_window(a, w, a["_event_t"]))
        a["timeline"].append({"type": "incident_" + status, "ts": time.time()})
        if status in INCIDENT_CLOSED:
            self.update_alert(a, state="resolved", severity=sev,
                              message=a["message"] + {"worker_ok": " Worker answered they are OK.",
                                                      "cancelled": " Cancelled.", "resolved": " Resolved."}[status])
        elif status == "acknowledged":
            self.update_alert(a, state="acknowledged", severity=sev)
        elif status == "no_response":
            self.update_alert(a, state="open", severity=sev,
                              title=a["title"].split(" · ")[0] + " · NO RESPONSE")
        else:
            self.update_alert(a, severity=sev)
        escalated = []
        if sev == "critical" and status not in INCIDENT_CLOSED and not (prev or {}).get("escalated") \
                and not a.get("_escalated_once"):
            escalated = self.escalate(a)
        return {"alert_id": a["id"], "escalated": escalated}

    async def capture_window(self, a, w, t):
        await asyncio.sleep(4.5)
        samples = [s for s in list(w["_ring"]) if t - 15000 <= s[0] <= t + 4000]
        if samples:
            a["_window"] = {"samples": samples, "event_t_ms": t}
            self.update_alert(a, has_window=True)
            if a.get("_auto_devin"):
                self.maybe_auto_devin(a)

    def maybe_auto_devin(self, a):
        now = time.time()
        if not AUTO_DEVIN or a.get("devin") or a.get("simulated"):
            return
        if now - self._last_auto_devin < 90:
            return
        self._last_auto_devin = now
        self.spawn(self.analyze(a))

    # ------------------------------------------------------------ alerts
    def new_alert(self, w, typ, severity, title, message, source="device", simulated=False,
                  detail=None, event_t_ms=None, state="open"):
        self.alert_seq += 1
        now = time.time()
        a = {
            "id": "A-%06d" % self.alert_seq, "worker_id": w["id"], "worker_name": w["name"],
            "live": bool(w.get("live")), "simulated": simulated, "type": typ, "severity": severity,
            "title": title, "message": message, "ts": now, "state": state, "source": source,
            "has_window": False, "devin": None, "notified": [], "incident_id": None,
            "timeline": [{"type": typ, "ts": now}], "detail": detail or {},
            "zone": w.get("zone"), "crew": w.get("crew"), "trade": w.get("trade"),
            "_window": None, "_event_t": event_t_ms, "_resolve_at": None,
        }
        self.alerts[a["id"]] = a
        w["_alerts"].append(a["id"])
        w["last_event"] = typ
        if len(self.alerts) > 400:
            for aid in [k for k, v in self.alerts.items() if v["state"] in CLOSED][:60] or list(self.alerts)[:60]:
                self.alerts.pop(aid, None)
        self.publish("alert", public(a))
        return a

    def update_alert(self, a, **changes):
        a.update(changes)
        self.publish("alert", public(a))
        w = self.find_worker(a["worker_id"])
        if w:
            self.refresh_status(w, time.time())
            if w.get("live"):
                self.publish("worker", public(w))

    def alert_action(self, aid, action):
        a = self.alerts.get(aid)
        if not a:
            raise HTTPException(404, "no such alert")
        w = self.find_worker(a["worker_id"])
        now = time.time()
        a["timeline"].append({"type": "supervisor_" + action, "ts": now})
        if a.get("incident_id") and w:
            self.record_incident(a, w, status="acknowledged" if action == "ack" else "resolved")
        if action == "ack":
            self.update_alert(a, state="acknowledged")
            if w and w.get("live"):
                self.send_cmd(w, {"cmd": "ack", "type": a["type"], "text": "Supervisor saw your alert. Help is on the way."})
        elif action in ("resolve", "false_alarm"):
            self.update_alert(a, state="resolved" if action == "resolve" else "false_alarm")
            if w and w.get("live") and a["severity"] != "info":
                self.send_cmd(w, {"cmd": "clear"})
        return public(a)

    # ------------------------------------------------------------ Devin
    async def analyze(self, a):
        if a.get("devin") and a["devin"].get("status") in ("queued", "running"):
            return
        w = self.find_worker(a["worker_id"])
        params = (w or {}).get("_params") or {}
        feats = devin_mod.features(a.get("_window"))
        if not self.devin.configured:
            self.update_alert(a, devin={"status": "done", "engine": "local", "features": feats,
                                        **devin_mod.local_verdict(public(a), feats, params)})
            return
        self.update_alert(a, devin={"status": "queued", "engine": "devin", "features": feats})
        prompt = devin_mod.build_prompt(public(a), public(w) if w else None, self.site, params, a.get("_window"), feats)
        a["_prompt"] = prompt
        try:
            sid, url = await self.devin.create(
                prompt, "HeatGuard %s · %s · %s" % (a["id"], a["title"], a["worker_name"]), ["heatguard", a["type"]])
            a["devin"].update(status="running", session_id=sid, url=url)
            self.update_alert(a)
            deadline, idle = time.time() + 1500, 0
            while time.time() < deadline:
                await asyncio.sleep(10)
                status, out, url2 = await self.devin.get(sid)
                if url2:
                    a["devin"]["url"] = url2
                if isinstance(out, str):
                    try:
                        out = json.loads(out)
                    except ValueError:
                        out = None
                if isinstance(out, dict) and out.get("verdict"):
                    a["devin"].update(status="done", **{k: out.get(k) for k in (
                        "verdict", "confidence", "summary", "evidence", "recommended_actions",
                        "suggested_threshold_changes")})
                    self.update_alert(a)
                    return
                if status == "error":
                    raise RuntimeError("Devin session errored")
                if status in ("exit", "suspended"):
                    idle += 1
                    if idle >= 3:
                        a["devin"].update(status="done", verdict="needs_human_review", confidence=None,
                                          summary="Devin finished without a structured verdict. Open the session for its notes.")
                        self.update_alert(a)
                        return
                self.update_alert(a, devin=dict(a["devin"], devin_status=status))
            raise RuntimeError("timed out waiting for Devin")
        except Exception as e:
            loc = devin_mod.local_verdict(public(a), feats, params)
            self.update_alert(a, devin=dict(a.get("devin") or {}, status="done", engine="local",
                                            error="Devin: %s" % str(e)[:160], **loc))

    # ------------------------------------------------------------ escalation: WhatsApp + phone call
    def store_note(self, n):
        self.notifications[n["id"]] = n
        while len(self.notifications) > 100:
            self.notifications.popitem(last=False)
        self.publish("notify", n)

    async def notify_alert(self, alert_id, reason=""):
        a = self.alerts.get(alert_id)
        if not a:
            return []
        w = self.find_worker(a["worker_id"])
        pa, pw, force = public(a), (public(w) if w else None), reason == "manual"
        loc = self.location_of(w)
        if pw:
            pw["location"] = loc
        site = dict(self.site, lat=loc["lat"], lon=loc["lon"])  # messages pin the worker's zone
        jobs = []
        if self.whatsapp:
            jobs.append(self.whatsapp.send_incident(pa, pw, site, force=force))
        if self.caller and a["severity"] == "critical" and not a.get("simulated"):
            jobs.append(self.caller.call_incident(pa, pw, site, force=force))
        notes = []
        for res in await asyncio.gather(*jobs, return_exceptions=True):
            if isinstance(res, Exception):
                res = [{"id": "N-err-%d" % int(time.time() * 1000), "channel": "escalation", "status": "failed",
                        "alert_id": alert_id, "text": "", "ts": time.time(), "error": str(res)[:200]}]
            notes.extend(res)
        for n in notes:
            self.store_note(n)
            a["notified"].append(n["id"])
            if n.get("channel") == "call" and n.get("provider_id"):
                self.spawn(self.caller.follow(n, self.store_note))
        a["timeline"].append({"type": "escalated_" + (reason or "manual"), "ts": time.time()})
        self.update_alert(a)
        return notes

    # ------------------------------------------------------------ voice assistant hooks
    def worker_context(self, device_id):
        w = self.live.get(device_id)
        if not w:
            return {"worker": None, "site": self.site, "open_alerts": [], "known": False}
        return {"worker": public(w), "site": self.site, "known": True,
                "open_alerts": [public(a) for a in self.worker_alerts(w) if a["state"] not in CLOSED]}

    def publish_voice(self, entry):
        self.voice[entry["id"]] = entry
        while len(self.voice) > 120:
            self.voice.popitem(last=False)
        self.publish("voice", entry)

    def heat_status(self, w):
        s, z = self.site, self.heat_for_zone(w.get("zone"))
        return {"wbgt_c": z["wbgt_c"], "category": z["category"]["label"], "air_temp_c": s["temp_c"],
                "humidity_pct": s["rh"], "phase": w["phase"], "phase_reason": w["phase_reason"],
                "workload": w["workload"], "work_minutes_done": round(w["work_elapsed_min"], 1),
                "work_minutes_allowed": w["work_budget_min"],
                "minutes_until_break": max(0, round(w["work_budget_min"] - w["work_elapsed_min"])) if w["phase"] == "work" else 0,
                "cool_down_minutes_left": round(max(0.0, w["rest_remaining_min"]), 1),
                "midday_break": s["midday_break"], "shift_exposure_min": round(w["shift_exposure_min"])}

    async def assistant_tool(self, device_id, name, args):
        w = self.live.get(device_id)
        if not w:
            return {"ok": False, "error": "unknown wearable"}
        args = args or {}
        if name == "get_heat_status":
            return self.heat_status(w)
        if name == "start_cool_down":
            mins = max(5, min(60, int(args.get("minutes") or 15)))
            self.cmd_rest(w, mins, "Asked the voice assistant")
            return {"ok": True, "cool_down_minutes": mins}
        if name == "report_symptoms":
            sev = args.get("severity") or "moderate"
            said = str(args.get("symptoms") or "?")
            # heat symptoms are a heat-illness incident; anything else is "unwell"
            heat = any(k in said.lower() for k in ("hot", "heat", "dizz", "faint", "confus", "sweat",
                                                    "vomit", "nause", "cramp", "headache", "collapse"))
            a = self.new_alert(w, "unwell", "critical" if sev == "severe" else "warning", "Symptoms reported by voice",
                               "Worker said: %s (severity: %s). Cool-down started." % (said, sev),
                               source="voice")
            self.cmd_rest(w, 30, "Reported symptoms")
            if sev == "severe":
                self.escalate(a)
            self.record_incident(a, w, itype="heat_stroke" if heat else "unwell", source="voice",
                                 details={"symptoms": said, "severity_said": sev, "said_by": "worker",
                                          "action": "30 min cool-down started, supervisor alerted"})
            return {"ok": True, "alert_id": a["id"], "incident_id": a.get("incident_id"),
                    "cool_down_minutes": 30, "supervisor_notified": True}
        if name == "request_help":
            a = self.new_alert(w, "sos", "critical", "Help requested by voice",
                               "Worker asked for help: %s" % args.get("reason", "no reason given"), source="voice")
            notes = await self.notify_alert(a["id"], "voice") if AUTO_WHATSAPP else []
            a["_escalated"] = sorted({n.get("channel") for n in notes if n.get("channel")})
            self.record_incident(a, w, itype="manual_sos", source="voice",
                                 details={"reason": args.get("reason") or "", "said_by": "worker"})
            return {"ok": True, "alert_id": a["id"], "supervisor_notified": True, "whatsapp_messages": len(notes)}
        return {"ok": False, "error": "unknown tool " + name}

    # ------------------------------------------------------------ ticker + simulation
    async def ticker(self):
        last, n = time.time(), 0
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            dt, last, n = now - last, now, n + 1
            dt_min = dt * self.config["timewarp"] / 60.0
            if self.compute_site() or n % 30 == 0:
                self.publish("site", self.site)
            for w in list(self.live.values()):
                self.live_tick(w, now, dt_min, n)
            self.sim_tick(now, dt, dt_min)
            if n % 2 == 0:
                self.publish("fleet", self.fleet_view())
            if LAN and n % 60 == 0:
                self.ips = lan_ips()

    def live_tick(self, w, now, dt_min, n):
        offline = self.is_offline(w, now)
        if not offline:
            prev = self.heat_step(w, dt_min)
            if prev:
                self.on_phase_change(w, prev)
                self.send_plan(w)
            elif now - w["_plan_at"] >= 2:
                self.send_plan(w)
        elif now - w["_last_seen"] > 30 and not w["_offline_alert"]:
            a = self.new_alert(w, "offline", "warning", "Wearable offline",
                               "No data for 30 s. Battery, WiFi coverage, or the wearable was taken off.",
                               source="server")
            w["_offline_alert"] = a["id"]
            self.publish("config", self.config_view())
        self.refresh_status(w, now)
        self.publish("worker", public(w))

    def sim_tick(self, now, dt, dt_min):
        rng = self.rng
        for w in self.fleet:
            if w["_offline"]:
                if now > w["_offline_until"]:
                    w["_offline"] = False
                else:
                    self.refresh_status(w, now)
                    continue
            if rng.random() < 0.002 * dt:
                w["workload"] = rng.choice(("light", "moderate", "moderate", "heavy"))
            self.heat_step(w, dt_min)
            self.refresh_status(w, now)
        if self.config["sim_events"] and rng.random() < dt / SIM_RATE_S:
            self.sim_incident(now)
        for a in list(self.alerts.values()):
            if a["_resolve_at"] and now > a["_resolve_at"] and a["state"] not in CLOSED:
                a["_resolve_at"] = None
                a["timeline"].append({"type": "supervisor_resolve", "ts": now})
                self.update_alert(a, state="resolved")

    def sim_incident(self, now):
        rng = self.rng
        typ, sev, _, title, message = sim.pick_incident(rng, self.site["category"]["level"])
        pool = [w for w in self.fleet if not w["_offline"] and w["status"] != "alert"]
        if typ == "rest_violation":
            pool = [w for w in pool if w["phase"] == "rest"] or pool
        if not pool:
            return
        w = rng.choice(pool)
        if typ == "offline":
            w["_offline"], w["_offline_until"] = True, now + rng.uniform(60, 180)
        a = self.new_alert(w, typ, sev, title, message, simulated=True,
                           source="server" if typ in ("rest_violation", "heat_critical", "offline") else "device")
        a["_resolve_at"] = now + rng.uniform(90, 240)
        self.refresh_status(w, now)

    def fleet_view(self):
        counts = {k: 0 for k in STATUS_CHAR}
        tiles = []
        for w in self.fleet:
            counts[w["status"]] += 1
            tiles.append(STATUS_CHAR[w["status"]])
        counts["total"] = len(self.fleet)
        return {"counts": counts, "tiles": "".join(tiles), "ids_prefix": "W-", "id_width": 4}

    def config_view(self):
        now = time.time()
        return {
            "devin_configured": self.devin.configured, "devin_mode": self.devin.label,
            "serial_port": self.bridge.port if self.bridge else None,
            "serial_connected": bool(self.bridge and self.bridge.connected),
            "bridge_enabled": bool(self.bridge and self.bridge.enabled),
            "wearables": [{"id": w["id"], "device_id": d, "transport": w["_transport"],
                           "online": now - w["_last_seen"] <= OFFLINE_S, "ip": w["_addr"][0] if isinstance(w["_addr"], tuple) else w["_ip"],
                           "fw": w["_fw"]} for d, w in self.live.items()],
            "gateway": {"lan": LAN, "udp_port": UDP_PORT, "ips": self.ips, "http_port": HTTP_PORT,
                        "device_ws": "/v1/device/ws"},
            "fleet_size": len(self.fleet), "timewarp": self.config["timewarp"],
            "heat": heat_thresholds(),
            "sim_events": self.config["sim_events"],
            "assistant": self.assistant.describe() if self.assistant else
            {"enabled": False, "model": None, "reason": "voice service not loaded"},
            "whatsapp": self.whatsapp.describe() if self.whatsapp else
            {"provider": "none", "enabled": False, "dry_run": True, "recipients": []},
            "call": self.caller.describe() if self.caller else
            {"provider": "none", "enabled": False, "recipients": [], "reason": "call service not loaded"},
        }

    def state(self):
        return {
            "site": self.site,
            "live": [public(w) for w in self.live.values()],
            "fleet": self.fleet_view(),
            "alerts": [public(a) for a in reversed(list(self.alerts.values())[-150:])],
            "voice": list(self.voice.values())[-50:],
            "notifications": list(self.notifications.values())[-50:],
            "config": self.config_view(),
        }


core = Core()


_tasks = []


async def startup():
    """Start HeatGuard's background work (called from the telemetry app's lifespan)."""
    core.loop = asyncio.get_running_loop()
    jobs = [core.ticker()]
    if WEATHER:
        jobs.append(core.weather_loop())
    if LAN:
        core.ips = lan_ips()
        try:
            await core.udp.start()
        except OSError as e:
            log.error("heatguard: UDP gateway :%s unavailable: %s", UDP_PORT, e)
        try:
            import serial  # noqa: F401  (pyserial, the `lan` extra)
            core.bridge = SerialBridge(core.loop, core.on_device_msg, os.environ.get("HEATGUARD_PORT"))
        except ImportError:
            log.warning("heatguard: pyserial missing, no USB serial bridge (pip install '.[lan]')")
        jobs.append(beacon(HTTP_PORT))
    _tasks[:] = [asyncio.create_task(j) for j in jobs]
    core.spawn(core.setup_escalation())    # Twilio lookups run in the background
    if Assistant:
        try:
            core.assistant = Assistant(core)
            if LAN:                        # TCP :47802 voice port; otherwise voice is on the WebSocket
                await core.assistant.start()
        except Exception as e:
            log.error("voice assistant failed to start: %s", e)
            core.assistant = None
    log.info("heatguard: started (lan=%s, weather=%s, voice=%s)", LAN, WEATHER,
             core.assistant.describe() if core.assistant else None)


async def shutdown():
    """Stop HeatGuard's background work."""
    jobs = _tasks + list(core._jobs)
    _tasks.clear()
    for t in jobs:
        t.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    if core.assistant:
        try:
            await core.assistant.stop()
        except Exception:
            pass
    core.udp.stop()
    if core.bridge:
        core.bridge.stop()
        core.bridge = None


router = APIRouter(tags=["heatguard"])


# ---------------------------------------------------------------- device API
# Retired: telemetry now comes as sticks3.telemetry.v1 frames (device WebSocket /v1/device/ws or
# POST /v1/ingest/frames). The old mixed ingest endpoint is kept below for reference.
# def normalize(m):
#     if "k" in m:
#         return m
#     did, typ, d = m.get("device_id") or m.get("id"), m.get("type"), m.get("data") or {}
#     if typ == "imu":
#         return {"k": "s", "id": did, **d}
#     if typ == "status":
#         return {"k": "st", "id": did, **d}
#     if typ == "temperature":
#         if d.get("source") == "ambient":
#             core.override = core.override or None
#             core.weather = {**(core.weather or FALLBACK_WEATHER), "temp_c": d["temp_c"],
#                             "rh": d.get("rh", (core.weather or FALLBACK_WEATHER)["rh"]), "source": "sensor"}
#             core.compute_site()
#             return {"k": "noop", "id": did}
#         return {"k": "st", "id": did, "temp": d.get("temp_c")}
#     if typ == "event":
#         return {"k": "ev", "id": did, "type": d.get("type"), "detail": d.get("detail") or {},
#                 "t": d.get("t"), "seq": d.get("seq")}
#     return None
#
#
# @router.post("/api/v1/ingest")
# async def ingest(request: Request):
#     body = await request.json()
#     msgs = body if isinstance(body, list) else [body]
#     n, dev = 0, None
#     for m in msgs:
#         obj = normalize(m) if isinstance(m, dict) else None
#         if obj and obj.get("id"):
#             core.on_device_msg(obj, "http", None)
#             dev, n = obj["id"], n + 1
#     cmds = []
#     if dev and dev in core.live:
#         cmds, core.live[dev]["_pending"] = core.live[dev]["_pending"], []
#     return {"ok": True, "accepted": n, "commands": cmds}


# ---------------------------------------------------------------- dashboard API
def heat_thresholds():
    """Band temperature tiers in die °C (env-tunable) plus the air estimate offset."""
    return {"chip_offset": CHIP_OFFSET, "chip_stop": CHIP_STOP, "chip_call": CHIP_CALL,
            "air_stop": CHIP_STOP - CHIP_OFFSET, "air_call": CHIP_CALL - CHIP_OFFSET,
            "sustain_s": TEMP_SUSTAIN_S}


@router.get("/api/v1/heat")
async def get_heat_thresholds():
    return heat_thresholds()


@router.get("/api/v1/state")
async def get_state():
    return core.state()


@router.get("/api/v1/stream")
async def stream(request: Request):
    q = asyncio.Queue(maxsize=2000)
    core.subs.add(q)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    yield await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            core.subs.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/api/v1/workers")
async def list_workers(status: str | None = None, limit: int = 100):
    ws = list(core.by_wid.values()) + core.fleet
    if status:
        ws = [w for w in ws if w["status"] == status]
    ws.sort(key=lambda w: -w["risk"])
    return [public(w) for w in ws[:limit]]


@router.get("/api/v1/workers/{wid}")
async def get_worker(wid: str):
    w = core.find_worker(wid)
    if not w:
        raise HTTPException(404, "no such worker")
    out = public(w)
    out["alerts"] = [public(a) for a in core.worker_alerts(w)][-10:]
    return out


@router.post("/api/v1/workers/{wid}/command")
async def worker_command(wid: str, request: Request):
    w = core.find_worker(wid)
    if not w:
        raise HTTPException(404, "no such worker")
    c = await request.json()
    cmd = c.get("cmd")
    if cmd == "rest":
        core.cmd_rest(w, max(1, min(120, float(c.get("mins") or 15))), c.get("reason") or "Supervisor: cool-down")
    elif cmd == "resume":
        core.cmd_resume(w)
    elif cmd == "buzz":
        core.send_cmd(w, {"cmd": "buzz"})
    elif cmd == "msg":
        core.send_cmd(w, {"cmd": "msg", "text": str(c.get("text") or "")[:140]})
    else:
        raise HTTPException(400, "unknown cmd")
    core.refresh_status(w, time.time())
    if w.get("live"):
        core.publish("worker", public(w))
    return {"ok": True, "simulated": not w.get("live"), "worker": public(w)}


@router.get("/api/v1/alerts")
async def list_alerts(limit: int = 100):
    return [public(a) for a in reversed(list(core.alerts.values())[-limit:])]


@router.post("/api/v1/alerts/{aid}/{action}")
async def alert_action(aid: str, action: str):
    if action in ("ack", "resolve", "false_alarm"):
        return core.alert_action(aid, action)
    a = core.alerts.get(aid)
    if not a:
        raise HTTPException(404, "no such alert")
    if action == "devin":
        core.spawn(core.analyze(a))
        return {"ok": True, "engine": "devin" if core.devin.configured else "local"}
    if action == "notify":
        return {"ok": True, "notifications": await core.notify_alert(aid, "manual")}
    raise HTTPException(404, "unknown action")


@router.get("/api/v1/alerts/{aid}/window")
async def alert_window(aid: str):
    a = core.alerts.get(aid)
    if not a or not a.get("_window"):
        raise HTTPException(404, "no trace for this alert")
    return a["_window"]


@router.get("/api/v1/alerts/{aid}/devin_prompt", response_class=PlainTextResponse)
async def alert_prompt(aid: str):
    a = core.alerts.get(aid)
    if not a:
        raise HTTPException(404, "no such alert")
    if a.get("_prompt"):
        return a["_prompt"]
    w = core.find_worker(a["worker_id"])
    return devin_mod.build_prompt(public(a), public(w) if w else None, core.site,
                                  (w or {}).get("_params") or {}, a.get("_window"),
                                  devin_mod.features(a.get("_window")))


@router.post("/api/v1/site/override")
async def site_override(request: Request):
    c = await request.json()
    sun = bool(c.get("sun", True))
    core.override = {"temp_c": float(c.get("temp_c", 44)), "rh": float(c.get("rh", 40)),
                     "wind_ms": 2.0, "solar_wm2": 850.0 if sun else 0.0, "apparent_c": None, "source": "override"}
    core.compute_site()
    core.publish("site", core.site)
    return core.site


@router.delete("/api/v1/site/override")
async def site_override_clear():
    core.override = None
    core.compute_site()
    core.publish("site", core.site)
    return core.site


@router.post("/api/v1/demo")
async def demo(request: Request):
    c = await request.json()
    if "timewarp" in c:
        core.config["timewarp"] = max(1, min(120, int(c["timewarp"])))
    if "sim_events" in c:
        core.config["sim_events"] = bool(c["sim_events"])
    for w in core.live.values():
        core.send_plan(w)
    core.publish("config", core.config_view())
    return core.config_view()


def bridge_state():
    b = core.bridge          # None unless HEATGUARD_LAN=1 (and pyserial installed)
    return {"enabled": bool(b and b.enabled), "port": b.port if b else None,
            "connected": bool(b and b.connected)}


@router.get("/api/v1/bridge")
async def bridge_get():
    return bridge_state()


@router.post("/api/v1/bridge")
async def bridge_set(request: Request):
    c = await request.json()
    if core.bridge:
        core.bridge.enabled = bool(c.get("enabled", True))
        await asyncio.sleep(0.8 if not core.bridge.enabled else 0)
        core.publish("config", core.config_view())
    return bridge_state()


@router.get("/", include_in_schema=False)
async def index():
    f = STATIC / "index.html"
    if f.exists():
        return FileResponse(f, headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>HeatGuard</h1><p>Dashboard not built yet. API at /api/v1/state</p>")
