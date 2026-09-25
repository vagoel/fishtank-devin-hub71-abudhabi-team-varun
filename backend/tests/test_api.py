"""End-to-end API tests. Require a reachable Postgres (DATABASE_URL, defaults to the
docker-compose instance)."""

import math
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from telemetry.config import settings
from telemetry.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _device() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _wait_flushed(client: TestClient, timeout: float = 5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if client.get("/v1/stats").json()["writer"]["rows_pending"] == 0:
            return
        time.sleep(0.05)
    raise AssertionError("writer did not flush in time")


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["database"] is True


def test_temperature_roundtrip_memory_and_database(client):
    dev = _device()
    now = time.time()
    values = [20 + math.sin(i / 10) for i in range(300)]
    r = client.post(
        "/v1/ingest/temperature",
        json={"device_id": dev, "values": values, "start_ts": now - 30, "interval_ms": 100},
    )
    assert r.status_code == 202
    assert r.json()["accepted"] == 300

    mem = client.get(f"/v1/analytics/temperature/{dev}", params={"last": 100}).json()
    assert mem["source"] == "memory"
    assert mem["count"] == 100
    assert mem["latest"]["value"] == pytest.approx(values[-1], abs=1e-4)
    assert mem["stats"]["min"] == pytest.approx(min(values[-100:]), abs=1e-4)

    _wait_flushed(client)
    db = client.get(
        f"/v1/analytics/temperature/{dev}", params={"last": 100, "source": "database"}
    ).json()
    assert db["source"] == "database"
    assert db["count"] == 100
    assert db["stats"]["mean"] == pytest.approx(mem["stats"]["mean"], abs=1e-4)
    assert db["window"] == mem["window"]

    # Asking for more than is buffered falls back to the database transparently.
    more = client.get(f"/v1/analytics/temperature/{dev}", params={"last": 1000}).json()
    assert more["source"] == "database"
    assert more["count"] == 300


def test_gyro_roundtrip_and_readings(client):
    dev = _device()
    samples = [[math.sin(i / 5), 0.0, 0.5] for i in range(200)]
    ts = [1_700_000_000_000 + i * 20 for i in range(200)]  # epoch ms, 50 Hz
    r = client.post(
        "/v1/ingest/gyro", json={"device_id": dev, "samples": samples, "timestamps": ts}
    )
    assert r.status_code == 202

    a = client.get(f"/v1/analytics/gyro/{dev}", params={"last": 50}).json()
    assert a["count"] == 50
    assert a["window"]["sample_rate_hz"] == pytest.approx(50, abs=0.5)
    assert a["axes"]["z"]["mean"] == pytest.approx(0.5)
    assert a["motion"]["is_moving"] is True

    _wait_flushed(client)
    raw = client.get(f"/v1/readings/gyro/{dev}", params={"last": 3, "source": "database"}).json()
    assert raw["count"] == 3
    assert raw["samples"][-1] == pytest.approx(samples[-1], abs=1e-5)
    assert raw["ts"][-1] == pytest.approx(ts[-1] / 1000)

    devices = client.get("/v1/devices").json()
    entry = next(d for d in devices if d["device_id"] == dev)
    assert entry["has_gyro"] and not entry["has_temperature"]


def test_out_of_order_timestamps_are_sorted(client):
    dev = _device()
    r = client.post(
        "/v1/ingest/temperature",
        json={"device_id": dev, "values": [3, 1, 2], "timestamps": [30, 10, 20]},
    )
    assert r.status_code == 202
    raw = client.get(f"/v1/readings/temperature/{dev}", params={"last": 3}).json()
    assert raw["ts"] == [10, 20, 30]
    assert raw["values"] == [1, 2, 3]


def test_late_batch_memory_matches_database(client):
    dev = _device()
    base = 1_767_225_600
    client.post(
        "/v1/ingest/temperature",
        json={"device_id": dev, "values": [20, 30], "timestamps": [base + 2, base + 3]},
    )
    client.post(
        "/v1/ingest/temperature",
        json={"device_id": dev, "values": [0, 10], "timestamps": [base, base + 1]},
    )
    _wait_flushed(client)
    mem = client.get(f"/v1/readings/temperature/{dev}", params={"last": 2, "source": "memory"})
    db = client.get(f"/v1/readings/temperature/{dev}", params={"last": 2, "source": "database"})
    assert mem.json()["values"] == db.json()["values"] == [20, 30]
    a = client.get(f"/v1/analytics/temperature/{dev}", params={"last": 4}).json()
    assert a["source"] == "memory"
    assert a["latest"]["value"] == 30
    assert a["window"]["duration_s"] == 3


def test_mixed_envelope(client):
    dev = _device()
    r = client.post(
        "/v1/ingest",
        json={
            "temperature": [{"device_id": dev, "values": [1, 2]}],
            "gyro": [{"device_id": dev, "samples": [[0, 0, 1]]}],
        },
    )
    assert r.status_code == 202
    assert r.json()["accepted"] == 3
    assert client.post("/v1/ingest", json={}).status_code == 422


def test_unknown_device(client):
    r = client.get("/v1/analytics/temperature/does-not-exist").json()
    assert r["count"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"device_id": "d", "values": []},
        {"device_id": "d", "values": [1, 2], "timestamps": [1]},
        {"device_id": "d", "values": [1], "start_ts": 1},
        {"device_id": "d", "values": [1], "interval_ms": 0, "start_ts": 1},
        {"device_id": "", "values": [1]},
        {"device_id": "d", "values": [1], "unexpected": 1},
        {"device_id": "d", "samples": [[1, 2]]},
    ],
)
def test_validation_errors(client, payload):
    path = "/v1/ingest/gyro" if "samples" in payload else "/v1/ingest/temperature"
    assert client.post(path, json=payload).status_code == 422


def test_backpressure_when_queue_full(monkeypatch):
    monkeypatch.setattr(settings, "queue_max_rows", 5)
    monkeypatch.setattr(settings, "flush_interval_ms", 60_000)
    with TestClient(app) as c:
        ok = c.post("/v1/ingest/temperature", json={"device_id": _device(), "values": [1, 2, 3]})
        assert ok.status_code == 202
        full = c.post("/v1/ingest/temperature", json={"device_id": _device(), "values": [1, 2, 3]})
        assert full.status_code == 503
        assert full.headers["Retry-After"] == "1"
        assert c.get("/v1/stats").json()["writer"]["rejected_rows"] == 3

        # An envelope that does not fit is rejected as a whole, nothing is partially queued.
        env = c.post(
            "/v1/ingest",
            json={
                "temperature": [{"device_id": "a", "values": [1]}],
                "gyro": [{"device_id": "a", "samples": [[0, 0, 0]] * 5}],
            },
        )
        assert env.status_code == 503
        assert c.get("/v1/stats").json()["writer"]["rows_pending"] == 3
