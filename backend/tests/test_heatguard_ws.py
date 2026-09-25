"""HeatGuard inside the telemetry app: device WebSocket, frames into the team tables and the
live pipeline, events and alerts, incidents, voice framing, phone-call webhooks.
Requires a reachable Postgres like test_api. conftest.py keeps everything offline; Devin,
Twilio and OpenAI are stubbed here and any real network attempt is recorded in BLOCKED."""

import asyncio
import json
import sys
import time
import uuid

import pytest
from conftest import BLOCKED
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import heatguard
from telemetry.main import app

hg = sys.modules["heatguard.core"]      # the module (heatguard.core is the Core instance)
core = heatguard.core


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c
    assert not BLOCKED, f"external network calls attempted: {BLOCKED}"


def _dev() -> str:
    return f"sticks3-{uuid.uuid4().hex[:6]}"


def _wid(dev: str) -> str:
    return "LIVE-" + dev.split("-")[-1][:8]


def frames(dev, n, *, start_us=1_000_000, step_us=20_000, boot="9f3a01c2", seq0=0,
           temp_every=50):
    out = []
    for i in range(n):
        f = {
            "schema": "sticks3.telemetry.v1", "device_id": dev, "boot_id": boot,
            "sequence": seq0 + i, "read_time_us": start_us + i * step_us,
            "frame": "display_portrait_usb_down_rh",
            "fresh": {"accelerometer": True, "gyroscope": True, "temperature": i % temp_every == 0},
            "imu": {"acceleration_g": {"x": 0.01, "y": -0.03, "z": 0.99 + i / 1000},
                    "angular_velocity_dps": {"x": 0.1, "y": -0.1, "z": float(i)}},
        }
        if f["fresh"]["temperature"]:
            f["imu"]["die_temperature_c"] = 52.0
        if i == 0:
            f["configuration"] = {"accelerometer_range_g": 8, "accelerometer_odr_hz": 50,
                                  "library": "M5Unified (UiFlow2 MicroPython)",
                                  "library_version": "t"}
        out.append(f)
    return out


def recv_until(ws, pred, limit=60):
    """Read server messages until pred(message) is true; returns (match, all seen)."""
    seen = []
    for _ in range(limit):
        m = ws.receive()
        if m["type"] == "websocket.close":
            raise AssertionError(f"socket closed: {m}")
        obj = json.loads(m["text"]) if m.get("text") is not None else m.get("bytes")
        seen.append(obj)
        if pred(obj):
            return obj, seen
    raise AssertionError(f"no matching message in {seen}")


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def _wait_flushed(client, timeout=5.0):
    wait_for(lambda: client.get("/v1/stats").json()["writer"]["rows_pending"] == 0, timeout)


def is_cmd(name):
    return lambda o: isinstance(o, dict) and o.get("cmd") == name


# -- device WebSocket ---------------------------------------------------------------------


def test_ws_hello_frames_status_fall_event(client):
    dev = _dev()
    with client.websocket_connect(f"/v1/device/ws?device_id={dev}") as ws:
        ws.send_json({"k": "hello", "id": dev, "fw": "heatguard-0.5", "ip": "172.20.10.3",
                      "params": {"IMPACT_G": 1.8}})
        recv_until(ws, is_cmd("plan"))                 # new device: hello + plan come back
        ws.send_text(json.dumps(frames(dev, 10)))
        ws.send_json({"k": "st", "id": dev, "bat": 80, "chg": 1, "act": 0.02, "wl": "light",
                      "am": 1.0, "gm": 0.4, "still_s": 3, "phase": "work", "ui": "normal"})
        ws.send_json({"k": "ev", "id": dev, "seq": 100017, "type": "fall", "t": 1100,
                      "detail": {"freefall_ms": 180, "impact_g": 3.4, "still": 1}})
        ack, _ = recv_until(ws, is_cmd("evack"))
        assert ack == {"cmd": "evack", "seq": 100017}
        ws.send_json({"k": "ev", "id": dev, "seq": 100017, "type": "fall", "t": 1100})  # resend
        assert recv_until(ws, is_cmd("evack"))[0]["seq"] == 100017

    state = client.get("/api/v1/state").json()
    w = next(x for x in state["live"] if x["device_id"] == dev)
    assert w["id"] == _wid(dev) and w["transport"] == "ws" and w["battery"] == 80
    assert w["configuration"]["accelerometer_odr_hz"] == 50
    wear = next(x for x in state["config"]["wearables"] if x["device_id"] == dev)
    assert wear["transport"] == "ws" and wear["ip"] == "172.20.10.3"
    assert wear["fw"] == "heatguard-0.5"
    falls = [a for a in state["alerts"] if a["worker_id"] == _wid(dev) and a["type"] == "fall"]
    assert len(falls) == 1 and falls[0]["severity"] == "critical" and falls[0]["state"] == "open"
    assert falls[0]["incident_id"] == f"{dev}-9f3a01c2-100017"

    inc = wait_for(lambda: client.get("/v1/incidents", params={"device_id": dev}).json())
    assert inc[0]["type"] == "fall" and inc[0]["source"] == "device"
    assert inc[0]["alert_id"] == falls[0]["id"] and inc[0]["person"]["worker_id"] == _wid(dev)


def test_ws_frames_land_in_team_tables(client):
    dev = _dev()
    with client.websocket_connect(f"/v1/device/ws?device_id={dev}") as ws:
        ws.send_text(json.dumps(frames(dev, 10)))
        ws.send_json({"k": "hello", "id": dev})
        recv_until(ws, is_cmd("plan"))
    _wait_flushed(client)
    r = client.get(f"/v1/readings/accel/{dev}", params={"last": 50, "source": "database"}).json()
    assert r["count"] == 10
    assert client.get(f"/v1/readings/gyro/{dev}").json()["count"] == 10
    st = client.get(f"/v1/devices/{dev}").json()["state"]
    assert st["frames_received"] == 10 and st["boot_id"] == "9f3a01c2"


def test_ws_invalid_frames_get_error(client):
    dev = _dev()
    bad = frames(dev, 3)
    del bad[1]["imu"]["acceleration_g"]
    with client.websocket_connect(f"/v1/device/ws?device_id={dev}") as ws:
        ws.send_text(json.dumps(bad))
        err, _ = recv_until(ws, is_cmd("error"))
        assert err["what"] == "frames" and "acceleration_g" in err["detail"]
        ws.send_text(json.dumps(frames("sticks3-other0", 2)))
        err, _ = recv_until(ws, is_cmd("error"))
        assert "not this socket" in err["detail"]
        ws.send_text("{nope")
        assert recv_until(ws, is_cmd("error"))[0]["what"] == "json"
    assert client.get(f"/v1/readings/accel/{dev}").json()["count"] == 0


def test_ws_token(client, monkeypatch):
    monkeypatch.setenv("HEATGUARD_DEVICE_TOKEN", "s3cret")
    dev = _dev()
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/v1/device/ws?device_id={dev}&token=wrong"):
            pass
    assert e.value.code == 4401
    with pytest.raises(WebSocketDisconnect) as e:
        with client.websocket_connect(f"/v1/device/ws?device_id={dev}"):
            pass
    assert e.value.code == 4401
    with client.websocket_connect(f"/v1/device/ws?device_id={dev}&token=s3cret") as ws:
        ws.send_json({"k": "hello", "id": dev})
        recv_until(ws, is_cmd("plan"))
    with client.websocket_connect(f"/v1/device/ws?device_id={dev}",
                                  headers={"Authorization": "Bearer s3cret"}) as ws:
        ws.send_json({"k": "hello", "id": dev})
        recv_until(ws, is_cmd("plan"))


def test_ws_reconnect_replaces_old_socket(client):
    dev = _dev()
    with client.websocket_connect(f"/v1/device/ws?device_id={dev}") as old:
        old.send_json({"k": "hello", "id": dev})
        recv_until(old, is_cmd("plan"))
        with client.websocket_connect(f"/v1/device/ws?device_id={dev}") as new:
            new.send_json({"k": "hello", "id": dev})
            m = None
            for _ in range(20):
                m = old.receive()
                if m["type"] == "websocket.close":
                    break
            assert m["type"] == "websocket.close" and m["code"] == 4409
            core.send_cmd(core.live[dev], {"cmd": "buzz"})
            assert recv_until(new, is_cmd("buzz"))


def test_ws_voice_without_openai_key_gets_x(client):
    dev = _dev()
    assert core.assistant is not None and not core.assistant.enabled
    with client.websocket_connect(f"/v1/device/ws?device_id={dev}") as ws:
        ws.send_bytes(b"H" + json.dumps({"rate": 24000, "fmt": "pcm16"}).encode())
        ws.send_bytes(b"A" + bytes(4800))
        ws.send_bytes(b"E")
        x, _ = recv_until(ws, lambda o: isinstance(o, bytes))
        assert x[:1] == b"X" and x[1:] == b"Voice assistant offline"
        ws.send_json({"k": "hello", "id": dev})          # socket still usable
        recv_until(ws, is_cmd("plan"))


# -- live pipeline ------------------------------------------------------------------------


def test_http_frames_reach_heatguard(client):
    dev = _dev()
    r = client.post("/v1/ingest/frames", json=frames(dev, 20))
    assert r.status_code == 202
    w = core.live[dev]
    assert w["transport"] == "http" and len(w["_ring"]) == 20
    live = client.get("/api/v1/state").json()["live"]
    assert any(x["device_id"] == dev and x["transport"] == "http" for x in live)


def test_on_frames_samples_decimation_and_temperature(client):
    dev = _dev()
    q = asyncio.Queue(maxsize=10_000)
    core.subs.add(q)
    try:
        fs = frames(dev, 50)                              # 1 s at 50 Hz, temperature on frame 0
        for i in (5, 6):                                  # stale gyro: repeats the last value
            fs[i]["fresh"]["gyroscope"] = False
            del fs[i]["imu"]["angular_velocity_dps"]
        assert client.post("/v1/ingest/frames", json=fs).status_code == 202
    finally:
        core.subs.discard(q)
    w = core.live[dev]
    ring = list(w["_ring"])
    assert len(ring) == 50 and ring[0][0] == 1000.0 and ring[-1][0] == 1980.0
    assert ring[5][4:] == ring[4][4:] == ring[6][4:] and ring[7][6] == 7.0
    assert w["device_temp_c"] == 52.0 and w["wrist_temp_c"] == 17.0 and w["temp_level"] == "ok"
    assert w["configuration"]["library"].startswith("M5Unified")
    samples = []
    while not q.empty():
        msg = q.get_nowait()
        if msg.startswith("event: imu"):
            d = json.loads(msg.split("data: ", 1)[1])
            if d["id"] == _wid(dev):
                samples += d["samples"]
    ts = [s[0] for s in samples]
    assert len(ts) == 25 and all(b - a >= 40 for a, b in zip(ts, ts[1:], strict=False))


# -- incidents: alert, escalation (stubbed), closing -------------------------------------


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    def describe(self):
        return {"provider": "fake", "enabled": True, "dry_run": False, "recipients": []}

    async def send_incident(self, alert, worker, site, force=False):
        self.sent.append(alert["id"])
        return [{"id": f"N-{len(self.sent)}", "channel": "whatsapp", "status": "sent",
                 "alert_id": alert["id"], "text": "", "ts": time.time()}]


class FakeCaller:
    def __init__(self):
        self.calls = []

    def describe(self):
        return {"provider": "fake", "mode": "gpt-live", "enabled": True, "recipients": []}

    async def call_incident(self, alert, worker, site, force=False):
        self.calls.append(alert["id"])
        return [{"id": f"C-{len(self.calls)}", "channel": "call", "status": "queued",
                 "alert_id": alert["id"], "text": "", "ts": time.time(), "provider_id": None}]


def test_incident_post_raises_alert_escalates_once_and_closes(client, monkeypatch):
    wa, caller = FakeWhatsApp(), FakeCaller()
    monkeypatch.setattr(core, "whatsapp", wa)
    monkeypatch.setattr(core, "caller", caller)
    monkeypatch.setattr(hg, "AUTO_WHATSAPP", True)
    dev = _dev()
    body = {"schema": "heatguard.incident.v1", "incident_id": f"{dev}-9f3a01c2-17",
            "device_id": dev, "type": "fall", "status": "no_response",
            "details": {"impact_g": 7.5, "message": "Free-fall then impact, no movement"}}
    r = client.post("/v1/incidents", json=body)
    assert r.status_code == 201
    j = r.json()
    assert j["created"] and j["severity"] == "critical" and j["alert_id"]
    assert sorted(j["escalated"]) == ["call", "whatsapp"]
    wait_for(lambda: wa.sent and caller.calls)
    a = core.alerts[j["alert_id"]]
    assert a["incident_id"] == body["incident_id"] and a["state"] == "open"
    assert "NO RESPONSE" in a["title"]
    stored = client.get(f"/v1/incidents/{body['incident_id']}").json()
    assert stored["person"]["worker_id"] == _wid(dev) and stored["location"]["source"] == "zone"

    again = client.post("/v1/incidents", json=body)
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["alert_id"] == j["alert_id"]
    time.sleep(0.2)
    assert len(wa.sent) == 1 and len(caller.calls) == 1   # escalated once

    ok = client.post("/v1/incidents", json=dict(body, status="worker_ok"))
    assert ok.status_code == 200 and ok.json()["status"] == "worker_ok"
    assert core.alerts[j["alert_id"]]["state"] == "resolved"


def test_heat_emergency_and_voice_help_are_recorded_as_incidents(client):
    dev = _dev()
    client.post("/v1/ingest/frames", json=frames(dev, 2))
    w = core.live[dev]
    w["_temp_ema"], w["_call_since"], w["_stop_since"] = 85.0, time.time() - 60, time.time() - 60
    client.portal.call(core.temp_rules, w, 85.0)
    res = client.portal.call(core.assistant_tool, dev, "request_help", {"reason": "dizzy"})
    assert res["ok"]
    incs = wait_for(lambda: len(client.get("/v1/incidents", params={"device_id": dev}).json()) >= 2
                    and client.get("/v1/incidents", params={"device_id": dev}).json())
    by_type = {i["type"]: i for i in incs}
    assert by_type["heat_stroke"]["source"] == "server"
    assert by_type["heat_stroke"]["severity"] == "critical"
    assert by_type["manual_sos"]["source"] == "voice"


# -- phone-call webhooks ------------------------------------------------------------------


def test_twilio_webhooks_and_media_one_time_id(client, monkeypatch):
    lc = heatguard.livecall
    monkeypatch.setattr(lc, "PUBLIC_URL", "https://example.run.app")
    rec = lc.CallRecord(id="testcall1", to="+10000000000", incident=lc.Incident())
    lc.remember(rec)
    r = client.post("/twilio/voice/testcall1")
    assert r.status_code == 200 and 'url="wss://example.run.app/twilio/media"' in r.text
    assert "/twilio/connect-done/testcall1" in r.text
    assert "<Hangup/>" in client.post("/twilio/voice/unknown").text
    client.post("/twilio/status/testcall1", content=b"CallStatus=ringing",
                headers={"content-type": "application/x-www-form-urlencoded"})
    assert rec.status == "ringing"
    with client.websocket_connect("/twilio/media") as ws:   # unknown call id: closed at once
        ws.send_json({"event": "start",
                      "start": {"streamSid": "MZ1", "customParameters": {"call_id": "nope"}}})
        assert ws.receive()["type"] == "websocket.close"
    assert client.post("/v1/calls", json={}).status_code == 403   # LIVE_CALL_API_TOKEN unset


def test_dashboard_and_calls_are_off(client):
    r = client.get("/")
    assert r.status_code == 200 and "const API = '/api/v1'" in r.text
    assert "Cloud · WebSocket" in r.text
    cfg = client.get("/api/v1/state").json()["config"]
    assert cfg["gateway"]["lan"] is False and cfg["gateway"]["device_ws"] == "/v1/device/ws"
    bridge = client.get("/api/v1/bridge").json()
    assert bridge == {"enabled": False, "port": None, "connected": False}
    assert client.get("/admin").status_code == 200 and client.get("/health").status_code == 200
