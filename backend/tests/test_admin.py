"""Admin dashboard: ingest meter + overview endpoint + static page."""

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from telemetry.main import app
from telemetry.metrics import IngestMeter


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_meter_counts_per_device_and_second():
    m = IngestMeter(window_s=10)
    m.record("gyro", "a", 5, now=100.2)
    m.record("accel", "a", 5, now=100.7)
    m.record("gyro", "b", 2, now=101.1)
    snap = m.snapshot(now=101.5)
    assert snap["devices"]["a"] == {"rows": {"gyro": 5, "accel": 5}, "last_ingest": 100.7}
    assert snap["devices"]["b"]["rows"] == {"gyro": 2}
    assert snap["history"] == [{"t": 100, "rows": {"a": 10}}, {"t": 101, "rows": {"b": 2}}]
    assert m.device("zzz") is None


def test_meter_prunes_old_seconds():
    m = IngestMeter(window_s=5)
    for s in range(20):
        m.record("temperature", "a", 1, now=float(s))
    snap = m.snapshot(now=19.0)
    assert [h["t"] for h in snap["history"]] == list(range(14, 20))
    assert snap["devices"]["a"]["rows"] == {"temperature": 20}


def test_overview_reflects_ingest(client):
    dev = f"admin-{uuid.uuid4().hex[:8]}"
    now = time.time()
    r = client.post(
        "/v1/ingest/gyro",
        json={
            "device_id": dev,
            "samples": [[0.1, 0.2, 0.3]] * 40,
            "start_ts": now - 4,
            "interval_ms": 100,
        },
    )
    assert r.status_code == 202
    o = client.get("/v1/admin/overview").json()
    assert {"now", "writer", "frames", "devices", "rate"} <= o.keys()
    assert "queue_max_rows" in o["writer"]
    d = next(x for x in o["devices"] if x["device_id"] == dev)
    assert d["rows"] == {"temperature": 0, "gyro": 40, "accel": 0}
    assert d["buffered"]["gyro"] == 40
    assert abs(d["last_ingest"] - now) < 5
    sec = int(d["last_ingest"])
    hist = {h["t"]: h["rows"] for h in o["rate"]["history"]}
    assert hist[sec][dev] == 40
    assert o["rate"]["window_s"] > 0


def test_admin_page_served(client):
    r = client.get("/admin")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/v1/admin/overview" in r.text
    assert "rateChart" in r.text
