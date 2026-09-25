"""Incidents API (heatguard.incident.v1): create, idempotent re-POST, status updates, filters,
validation. Requires a reachable Postgres like test_api."""

import uuid

import pytest
from fastapi.testclient import TestClient

from telemetry.main import app


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def incident(**overrides) -> dict:
    dev = overrides.pop("device_id", f"sticks3-{uuid.uuid4().hex[:6]}")
    body = {
        "schema": "heatguard.incident.v1",
        "incident_id": f"{dev}-9f3a01c2-{uuid.uuid4().hex[:4]}",
        "device_id": dev,
        "person": {"name": "Ravi Kumar", "worker_id": "W-0042", "trade": "Steel fixer"},
        "type": "fall",
        "status": "suspected",
        "occurred_at": "2026-09-25T12:41:07Z",
        "details": {"impact_g": 7.5, "freefall_ms": 361, "still": 1},
    }
    body.update(overrides)
    return body


def test_create_duplicate_and_status_update(client):
    body = incident()
    r = client.post("/v1/incidents", json=body)
    assert r.status_code == 201, r.text
    j = r.json()
    assert j["created"] is True and j["incident_id"] == body["incident_id"]
    assert j["status"] == "suspected" and j["severity"] == "critical"  # default for a fall

    again = client.post("/v1/incidents", json=body)
    assert again.status_code == 200 and again.json()["created"] is False

    upd = client.post(
        "/v1/incidents", json=dict(body, status="worker_ok", details={"answered_s": 4})
    )
    assert upd.status_code == 200 and upd.json()["status"] == "worker_ok"
    got = client.get(f"/v1/incidents/{body['incident_id']}").json()
    assert got["status"] == "worker_ok"
    assert got["details"] == {"impact_g": 7.5, "freefall_ms": 361, "still": 1, "answered_s": 4}
    assert got["person"]["name"] == "Ravi Kumar" and got["source"] == "device"
    assert got["occurred_at"].startswith("2026-09-25T12:41:07")
    assert {"received_at", "updated_at", "alert_id", "escalated"} <= got.keys()


def test_generated_id_defaults_and_filters(client):
    dev = f"sticks3-{uuid.uuid4().hex[:6]}"
    minimal = {"schema": "heatguard.incident.v1", "device_id": dev, "type": "tremor"}
    r = client.post("/v1/incidents", json=minimal)
    assert r.status_code == 201
    assert r.json()["incident_id"].startswith(dev) and r.json()["severity"] == "warning"
    client.post("/v1/incidents", json=incident(device_id=dev, type="impact"))
    client.post("/v1/incidents", json=incident(type="impact"))  # another device

    mine = client.get("/v1/incidents", params={"device_id": dev}).json()
    assert {i["type"] for i in mine} == {"tremor", "impact"}
    impacts = client.get("/v1/incidents", params={"device_id": dev, "type": "impact"}).json()
    assert len(impacts) == 1 and impacts[0]["severity"] == "info"
    later = client.get(
        "/v1/incidents", params={"device_id": dev, "since": "2026-09-25T12:41:08Z"}
    ).json()
    assert [i["type"] for i in later] == ["tremor"]  # occurred now; the impact is back in 12:41:07
    assert client.get("/v1/incidents", params={"limit": 1}).json().__len__() == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(type="explosion"),
        lambda b: b.pop("device_id"),
        lambda b: b.update(unexpected=1),
        lambda b: b.update(schema="heatguard.incident.v2"),
        lambda b: b.update(status="maybe"),
    ],
)
def test_validation(client, mutate):
    body = incident()
    mutate(body)
    assert client.post("/v1/incidents", json=body).status_code == 422


def test_unknown_incident(client):
    assert client.get("/v1/incidents/nope-nope").status_code == 404
    assert client.get("/v1/incidents", params={"type": "explosion"}).status_code == 422
