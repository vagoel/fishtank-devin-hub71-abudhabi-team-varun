"""sticks3.telemetry.v1 device frame contract: validation, fan-out, clock alignment and
device state tracking. Requires a reachable Postgres like test_api."""

import copy
import itertools
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from telemetry.devices import DeviceRegistry
from telemetry.main import app
from telemetry.models import TelemetryFrame

FRAME = {
    "schema": "sticks3.telemetry.v1",
    "device_id": "sticks3-a1b2c3",
    "boot_id": "74f2c291",
    "sequence": 1842,
    "read_time_us": 10534921,
    "frame": "display_landscape_usb_right_rh",
    "fresh": {"accelerometer": True, "gyroscope": True, "temperature": True},
    "imu": {
        "acceleration_g": {"x": 0.012, "y": -0.031, "z": 0.998},
        "angular_velocity_dps": {"x": 0.18, "y": -0.06, "z": 12.45},
        "die_temperature_c": 31.74,
    },
    "configuration": {
        "accelerometer_range_g": 8,
        "accelerometer_odr_hz": 100,
        "gyroscope_range_dps": 2000,
        "gyroscope_odr_hz": 200,
        "library": "M5Unified",
        "library_version": "0.2.22",
    },
}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def frame(**overrides) -> dict:
    f = copy.deepcopy(FRAME)
    f.update(overrides)
    return f


def frames(device: str, n: int, *, start_seq: int = 0, start_us: int = 0, step_us: int = 5000):
    out = []
    for i in range(n):
        f = frame(device_id=device, sequence=start_seq + i, read_time_us=start_us + i * step_us)
        f["imu"]["angular_velocity_dps"]["z"] = float(i)
        out.append(f)
    return out


def _device() -> str:
    return f"sticks3-{uuid.uuid4().hex[:6]}"


def _wait_flushed(client: TestClient, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/v1/stats").json()["writer"]["rows_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("writer did not flush in time")


# -- contract ---------------------------------------------------------------------------


def test_contract_example_parses():
    f = TelemetryFrame.model_validate(FRAME)
    assert f.schema_id == "sticks3.telemetry.v1"
    assert f.imu.angular_velocity_dps.as_tuple() == (0.18, -0.06, 12.45)
    assert f.configuration.library == "M5Unified"
    assert f.sample_count() == 3


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.update(schema="sticks3.telemetry.v2"),
        lambda f: f.pop("schema"),
        lambda f: f.update(device_id=""),
        lambda f: f.update(sequence=-1),
        lambda f: f.update(read_time_us=-5),
        lambda f: f.update(unexpected=1),
        lambda f: f["imu"].pop("angular_velocity_dps"),
        lambda f: f["imu"].pop("acceleration_g"),
        lambda f: f["imu"].pop("die_temperature_c"),
        lambda f: f["imu"]["acceleration_g"].pop("z"),
        lambda f: f["imu"]["acceleration_g"].update(w=1),
        lambda f: f["fresh"].update(magnetometer=True),
    ],
)
def test_invalid_frames_rejected(client, mutate):
    f = frame()
    mutate(f)
    r = client.post("/v1/ingest/frames", json=f)
    assert r.status_code == 422, r.text


def test_stale_sensors_are_allowed_and_skipped(client):
    dev = _device()
    f = frame(
        device_id=dev, fresh={"accelerometer": False, "gyroscope": True, "temperature": False}
    )
    del f["imu"]["acceleration_g"]
    del f["imu"]["die_temperature_c"]
    r = client.post("/v1/ingest/frames", json=f)
    assert r.status_code == 202
    assert r.json()["accepted"] == 1 and r.json()["frames"] == 1
    _wait_flushed(client)
    assert client.get(f"/v1/analytics/temperature/{dev}").json()["count"] == 0
    assert client.get(f"/v1/analytics/accel/{dev}").json()["count"] == 0
    assert client.get(f"/v1/analytics/gyro/{dev}").json()["count"] == 1


def test_configuration_accepts_extra_firmware_fields(client):
    f = frame(device_id=_device())
    f["configuration"]["magnetometer_odr_hz"] = 50
    r = client.post("/v1/ingest/frames", json=f)
    assert r.status_code == 202
    state = client.get(f"/v1/devices/{f['device_id']}").json()["state"]
    assert state["configuration"]["magnetometer_odr_hz"] == 50


def test_empty_and_oversized_arrays_rejected(client, monkeypatch):
    assert client.post("/v1/ingest/frames", json=[]).status_code == 422
    from telemetry.config import settings

    monkeypatch.setattr(settings, "max_batch_size", 2)
    r = client.post("/v1/ingest/frames", json=frames(_device(), 3))
    assert r.status_code == 422


# -- fan-out and analytics ---------------------------------------------------------------


def test_single_frame_fans_out_to_three_sensors(client):
    dev = _device()
    r = client.post("/v1/ingest/frames", json=frame(device_id=dev))
    assert r.status_code == 202
    assert r.json()["accepted"] == 3
    assert r.json()["frames"] == 1

    _wait_flushed(client)
    t = client.get(f"/v1/analytics/temperature/{dev}").json()
    assert t["count"] == 1 and t["latest"]["value"] == pytest.approx(31.74, abs=1e-4)
    g = client.get(f"/v1/analytics/gyro/{dev}").json()
    assert g["latest"]["z"] == pytest.approx(12.45, abs=1e-4)
    a = client.get(f"/v1/analytics/accel/{dev}").json()
    assert a["gravity"]["dominant_axis"] == "+z"
    assert a["gravity"]["magnitude"] == pytest.approx(0.9986, abs=1e-3)


def test_frame_batch_keeps_device_spacing_and_persists(client):
    dev = _device()
    before = time.time()
    r = client.post("/v1/ingest/frames", json=frames(dev, 200, step_us=5000))  # 200 Hz
    assert r.status_code == 202
    assert r.json() == {"accepted": 600, "queued_rows": r.json()["queued_rows"], "frames": 200}

    g = client.get(f"/v1/analytics/gyro/{dev}", params={"last": 200}).json()
    assert g["source"] == "memory"
    assert g["count"] == 200
    assert g["window"]["duration_s"] == pytest.approx(0.995, abs=1e-5)
    assert g["window"]["sample_rate_hz"] == pytest.approx(200, abs=0.01)
    assert g["latest"]["z"] == 199
    raw = client.get(f"/v1/readings/gyro/{dev}", params={"last": 1}).json()
    assert before - 0.001 <= raw["ts"][0] <= time.time()  # newest frame stamped ~now

    _wait_flushed(client)
    for kind in ("temperature", "gyro", "accel"):
        db = client.get(f"/v1/analytics/{kind}/{dev}", params={"last": 200, "source": "database"})
        assert db.json()["count"] == 200, kind
        assert db.json()["window"] == g["window"], kind

    devices = client.get("/v1/devices").json()
    entry = next(d for d in devices if d["device_id"] == dev)
    assert entry["has_temperature"] and entry["has_gyro"] and entry["has_accel"]
    assert entry["state"]["frames_received"] == 200


def test_consecutive_requests_align_on_device_clock(client):
    dev = _device()
    a = frames(dev, 10, start_seq=0, start_us=0)
    b = frames(dev, 10, start_seq=10, start_us=50_000)
    client.post("/v1/ingest/frames", json=a)
    time.sleep(0.02)
    client.post("/v1/ingest/frames", json=b)
    raw = client.get(f"/v1/readings/gyro/{dev}", params={"last": 20}).json()
    diffs = [round(y - x, 4) for x, y in itertools.pairwise(raw["ts"])]
    assert diffs == [0.005] * 19  # continuous 200 Hz grid across both requests


def test_device_state_tracks_gaps_reboots_and_persists(client):
    dev = _device()
    client.post("/v1/ingest/frames", json=frames(dev, 5, start_seq=100, start_us=1_000_000))
    client.post("/v1/ingest/frames", json=frames(dev, 5, start_seq=108, start_us=1_040_000))
    st = client.get(f"/v1/devices/{dev}").json()["state"]
    assert st["dropped_frames"] == 3
    assert st["last_sequence"] == 112
    assert st["reboots"] == 0

    rebooted = frames(dev, 2, start_seq=0, start_us=500)
    for f in rebooted:
        f["boot_id"] = "deadbeef"
    client.post("/v1/ingest/frames", json=rebooted)
    st = client.get(f"/v1/devices/{dev}").json()["state"]
    assert st["boot_id"] == "deadbeef"
    assert st["reboots"] == 1
    assert st["dropped_frames"] == 3  # the sequence reset is not a gap
    raw = client.get(f"/v1/readings/gyro/{dev}", params={"last": 3}).json()
    assert raw["ts"] == sorted(raw["ts"])  # post-reboot frames stamped after pre-reboot ones

    assert client.get("/v1/devices/never-seen").status_code == 404


def test_device_state_survives_restart():
    dev = _device()
    with TestClient(app) as c:
        c.post("/v1/ingest/frames", json=frames(dev, 3, start_seq=41))
    with TestClient(app) as c:  # registry is empty, state comes from the devices table
        st = c.get(f"/v1/devices/{dev}").json()["state"]
        assert st["boot_id"] == FRAME["boot_id"]
        assert st["last_sequence"] == 43
        assert st["configuration"]["library"] == "M5Unified"


# -- clock alignment (unit) --------------------------------------------------------------


def _tf(**kw) -> TelemetryFrame:
    return TelemetryFrame.model_validate(frame(**kw))


def test_registry_anchor_is_fixed_per_boot():
    reg = DeviceRegistry()
    now = 1000.0
    ts = reg.observe([_tf(sequence=0, read_time_us=0), _tf(sequence=1, read_time_us=10_000)], now)
    assert ts == pytest.approx([999.99, 1000.0])  # least-delayed frame of the batch anchors
    # Later requests keep the device grid whether they arrive early (jitter) or late.
    ts = reg.observe([_tf(sequence=2, read_time_us=20_000)], now + 0.005)
    assert ts == [pytest.approx(1000.01)]
    ts = reg.observe([_tf(sequence=3, read_time_us=30_000)], now + 0.5)
    assert ts == [pytest.approx(1000.02)]
    assert reg.get(FRAME["device_id"]).clock_resyncs == 0


def test_registry_resyncs_fast_device_clock_without_going_backwards():
    reg = DeviceRegistry()
    reg.observe([_tf(sequence=0, read_time_us=0)], 1000.0)  # offset 1000
    # Device claims 2 s elapsed but only 1 s of wall time passed: clock runs fast.
    ts = reg.observe([_tf(sequence=1, read_time_us=2_000_000)], 1001.0)
    st = reg.get(FRAME["device_id"])
    assert st.clock_resyncs == 1
    assert ts == [pytest.approx(1001.0)]
    ts = reg.observe([_tf(sequence=2, read_time_us=2_005_000)], 1001.2)
    assert ts == [pytest.approx(1001.005)]  # grid continues from the new anchor

    # A re-anchor can never stamp an in-order frame before the previous one.
    reg2 = DeviceRegistry()
    reg2.observe([_tf(sequence=0, read_time_us=0), _tf(sequence=1, read_time_us=500_000)], 100.0)
    ts = reg2.observe(
        [_tf(sequence=2, read_time_us=1_000_000), _tf(sequence=3, read_time_us=2_000_000)], 100.9
    )
    assert ts == pytest.approx([100.0, 100.9])  # first frame clamped to the previous stamp
    assert reg2.get(FRAME["device_id"]).clock_resyncs == 1


def test_registry_clock_wrap_reanchors():
    reg = DeviceRegistry()
    reg.observe([_tf(sequence=0, read_time_us=4_000_000_000)], 100.0)
    ts = reg.observe(
        [_tf(sequence=1, read_time_us=1_000), _tf(sequence=2, read_time_us=6_000)], 100.1
    )
    assert ts == pytest.approx([100.095, 100.1])
    st = reg.get(FRAME["device_id"])
    assert st.reboots == 0 and st.dropped_frames == 0 and st.last_sequence == 2
