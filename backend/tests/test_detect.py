"""Server-side fall detection (heatguard/detect.py): unit tests on synthetic frame streams (no
database) and one end-to-end test through POST /v1/ingest/frames, which needs a reachable
Postgres like test_api."""

import os

os.environ.setdefault("HEATGUARD_SERVER_FALL_GRACE_S", "0")  # no backup delay in tests

import math
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from heatguard import detect, incidents
from telemetry.main import app
from telemetry.models import TelemetryFrame


class Stream:
    """Synthetic wearable: builds frames on its own monotonic clock."""

    def __init__(self, device=None, boot="b00710aa", hz=50, t_us=5_000_000):
        self.device = device or f"sticks3-{uuid.uuid4().hex[:6]}"
        self.boot, self.hz, self.t_us, self.seq = boot, hz, t_us, 0
        self.die = None  # die temperature on every frame when set

    def _frame(self, acc, gyr) -> dict:
        f = {
            "schema": "sticks3.telemetry.v1",
            "device_id": self.device,
            "boot_id": self.boot,
            "sequence": self.seq,
            "read_time_us": self.t_us,
            "fresh": {
                "accelerometer": True,
                "gyroscope": True,
                "temperature": self.die is not None,
            },
            "imu": {
                "acceleration_g": dict(zip("xyz", acc, strict=True)),
                "angular_velocity_dps": dict(zip("xyz", gyr, strict=True)),
            },
        }
        if self.die is not None:
            f["imu"]["die_temperature_c"] = self.die
        self.seq += 1
        self.t_us += 1_000_000 // self.hz
        return f

    def _run(self, seconds, fn) -> list[dict]:
        return [self._frame(*fn(i / self.hz)) for i in range(round(seconds * self.hz))]

    def still(self, s):  # lying on the ground
        return self._run(s, lambda t: ((0.01, 0.02, 0.99 + 0.005 * math.sin(40 * t)), (1, 0.5, 1)))

    def walk(self, s):
        return self._run(
            s,
            lambda t: (
                (
                    0.2 * math.sin(2 * math.pi * 1.8 * t),
                    0.1,
                    1 + 0.35 * math.sin(2 * math.pi * 3.6 * t),
                ),
                (60 * math.sin(2 * math.pi * 1.8 * t), 20, 45),
            ),
        )

    def shake(self, s):
        return self._run(
            s,
            lambda t: (
                (
                    2.0 * math.sin(2 * math.pi * 5 * t),
                    0.3,
                    1 + 0.4 * math.sin(2 * math.pi * 5 * t + 1),
                ),
                (250 * math.cos(2 * math.pi * 5 * t), 80, 120),
            ),
        )

    def freefall(self, ms):
        return self._run(ms / 1000, lambda t: ((0.02, 0.03, 0.05), (200, 150, 90)))

    def spike(self, *gs):  # one frame per value, |a| = g
        return [self._frame((0.0, 0.0, g), (300, 200, 100)) for g in gs]

    def drop(self, impact=4.0):
        return self.still(1) + self.freefall(300) + self.spike(impact, 2.2)

    def parse(self, frames):
        return [TelemetryFrame.model_validate(f) for f in frames]


def run(det, stream, frames, batch=50):
    """Feed frames in ~1 s batches like the firmware; return the confirmed falls."""
    tf = stream.parse(frames)
    out = []
    for i in range(0, len(tf), batch):
        out += det.process(tf[i : i + batch])
    return out


# -- fall rule ------------------------------------------------------------------------------


def test_drop_then_still_is_a_fall():
    det, s = detect.Detector(), Stream()
    falls = run(det, s, s.drop() + s.still(3))
    assert len(falls) == 1
    f = falls[0]
    d = f["details"]
    assert d["still"] == 1 and d["detector"] == "server" and d["impact_g"] == 4.0
    assert 280 <= d["freefall_ms"] <= 320
    assert d["post_std_g"] < 0.12 and d["post_gyro_dps"] < 30
    impact_us = 5_000_000 + (50 + 15) * 20_000  # 1 s still + 300 ms free-fall at 50 Hz
    assert f["read_time_us"] == impact_us and f["boot_id"] == s.boot
    assert f["incident_id"] == f"{s.device}-{s.boot}-fall-{impact_us // 1000}"
    st = det.status(s.device)
    assert st["falls_24h"] == 1 and st["last_fall_at"] is not None


def test_drop_then_movement_is_a_fall_not_still():
    det, s = detect.Detector(), Stream()
    falls = run(det, s, s.drop() + s.walk(3))
    assert len(falls) == 1 and falls[0]["details"]["still"] == 0


def test_hard_impact_without_freefall():
    det, s = detect.Detector(), Stream()
    assert run(det, s, s.walk(1) + s.spike(3.6) + s.walk(3)) == []  # then movement: none
    falls = run(det, s, s.walk(1) + s.spike(3.6) + s.still(3))  # then still: a fall
    assert len(falls) == 1
    assert falls[0]["details"]["freefall_ms"] == 0 and falls[0]["details"]["still"] == 1


def test_walking_and_shaking_are_not_falls():
    det, s = detect.Detector(), Stream()
    assert run(det, s, s.walk(30) + s.shake(10) + s.walk(5) + s.still(5)) == []


def test_100hz_stream():
    det, s = detect.Detector(), Stream(hz=100)
    falls = run(det, s, s.drop() + s.still(3), batch=100)
    assert len(falls) == 1 and 280 <= falls[0]["details"]["freefall_ms"] <= 320


def test_refractory_period():
    det, s = detect.Detector(), Stream()
    frames = s.drop() + s.still(3) + s.drop() + s.still(3)  # second impact ~4.3 s later
    assert len(run(det, s, frames)) == 1
    assert len(run(det, s, s.still(6) + s.drop() + s.still(3))) == 1  # > 10 s after the first


def test_new_boot_resets_state_and_late_frames_are_skipped():
    det, s = detect.Detector(), Stream()
    old = s.still(1) + s.freefall(300)
    assert run(det, s, old) == []
    s.boot, s.seq, s.t_us = "c0ffee01", 0, 2_000_000
    # a 2.5 g impact only counts right after a free-fall; the free-fall was on the old boot
    assert run(det, s, s.spike(2.5) + s.still(3)) == []
    assert run(det, s, old) == []  # late frames from the earlier boot are ignored
    assert det.status(s.device)["boot_id"] == "c0ffee01"
    falls = run(det, s, s.drop() + s.still(3))
    assert len(falls) == 1 and falls[0]["incident_id"].startswith(f"{s.device}-c0ffee01-fall-")


def test_duplicate_and_out_of_order_frames_are_skipped():
    det, s = detect.Detector(), Stream()
    frames = s.drop() + s.still(3)
    assert len(run(det, s, frames)) == 1
    assert run(det, s, frames) == []  # the same batch again
    assert run(det, s, list(reversed(s.still(2)))) == []


def test_gap_restarts_freefall_measurement():
    det, s = detect.Detector(), Stream()
    frames = s.still(1) + s.freefall(60)
    s.t_us += 500_000  # half a second lost
    frames += s.freefall(40) + s.spike(2.5) + s.still(3)  # 60 + 40 ms is not one free-fall
    assert run(det, s, frames) == []


# -- temperature (display only) ------------------------------------------------------------


def test_temperature_status_is_display_only():
    det, s = detect.Detector(), Stream()
    s.die = 40.0
    assert run(det, s, s.walk(2)) == []
    st = det.status(s.device)
    assert st["die_c"] == 40.0 and st["air_c_est"] == 40.0 - detect.CHIP_OFFSET
    assert st["heat_level"] == "ok" and "not body temperature" in st["temperature_note"]
    s.die = detect.CHIP_STOP + 1
    assert run(det, s, s.walk(10)) == []  # no heat incidents from here: Core raises those
    assert det.status(s.device)["heat_level"] == "stop_work"
    s.die = detect.CHIP_CALL + 1
    run(det, s, s.walk(10))
    assert det.status(s.device)["heat_level"] == "call"
    s.die = 45.0
    run(det, s, s.walk(10))
    assert det.status(s.device)["heat_level"] == "ok"


# -- end to end ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def hook_calls():
    calls = []

    async def hook(incident, created, previous):
        calls.append((incident, created))
        return None

    prev = incidents._hook  # the merged backend wires Core escalation here: keep it out of tests
    incidents.set_hook(hook)
    yield calls
    incidents.set_hook(prev)


def _post(client, frames, batch=50):
    for i in range(0, len(frames), batch):
        r = client.post("/v1/ingest/frames", json=frames[i : i + batch])
        assert r.status_code == 202, r.text


def _wait_stored(client, expected_falls, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = client.get("/v1/heatguard/status").json()["server_falls"]
        if c["falls"] >= expected_falls and c["falls"] == (
            c["stored"] + c["device_duplicates"] + c["errors"]
        ):
            return c
        time.sleep(0.05)
    raise AssertionError("server falls were not stored in time")


def test_ingested_frames_raise_a_server_fall(client, hook_calls):
    s = Stream()
    before = detect.DETECTOR.counters["falls"]
    _post(client, s.drop() + s.still(3))
    _wait_stored(client, before + 1)

    got = client.get("/v1/incidents", params={"device_id": s.device}).json()
    assert len(got) == 1
    inc = got[0]
    assert inc["type"] == "fall" and inc["source"] == "server"
    assert inc["severity"] == "critical" and inc["status"] == "suspected"
    assert inc["details"]["detector"] == "server" and inc["details"]["still"] == 1
    assert inc["boot_id"] == s.boot and inc["incident_id"].startswith(f"{s.device}-{s.boot}-fall-")
    assert inc["read_time_us"] == int(inc["incident_id"].rsplit("-", 1)[1]) * 1000
    assert [c[1] for c in hook_calls if c[0]["incident_id"] == inc["incident_id"]] == [True]

    st = client.get(f"/v1/heatguard/status/{s.device}").json()
    assert st["falls_24h"] == 1 and st["last_fall_at"]
    assert any(
        d["device_id"] == s.device for d in client.get("/v1/heatguard/status").json()["devices"]
    )
    assert client.get("/v1/heatguard/status/sticks3-never-seen").status_code == 404


def test_device_reported_fall_is_not_duplicated(client, hook_calls):
    s = Stream()
    body = {
        "schema": "heatguard.incident.v1",
        "incident_id": f"{s.device}-{s.boot}-7",
        "device_id": s.device,
        "type": "fall",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "details": {"impact_g": 4.1, "freefall_ms": 300, "still": 1},
        "source": "device",
    }
    assert client.post("/v1/incidents", json=body).status_code == 201
    before = dict(detect.DETECTOR.counters)
    _post(client, s.drop() + s.still(3))
    c = _wait_stored(client, before["falls"] + 1)
    assert c["device_duplicates"] == before["device_duplicates"] + 1
    got = client.get("/v1/incidents", params={"device_id": s.device}).json()
    assert [(i["incident_id"], i["source"]) for i in got] == [(body["incident_id"], "device")]
