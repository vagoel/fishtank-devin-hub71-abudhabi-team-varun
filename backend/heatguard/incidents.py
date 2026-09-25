"""Incidents API, schema `heatguard.incident.v1` (docs/heatguard/incidents.md).

Anything that happened to a person (a fall, suspected heat stroke, an SOS) is an incident.
Wearables POST them, HeatGuard records its own detections through `record()`, and every
incident lands in the Postgres table `incidents` (telemetry/schema.sql). Volume is tiny, so
rows are written directly with asyncpg on the telemetry app's pool, not through the COPY writer.

Re-POSTing an `incident_id` updates it (status, severity, details, ...); `on_incident` is the
hook HeatGuard's Core uses to raise/close the dashboard alert and escalate.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("heatguard.incidents")

SCHEMA = "heatguard.incident.v1"
IncidentType = Literal[
    "fall", "heat_stroke", "manual_sos", "tremor", "inactivity", "unwell", "impact", "other"
]
IncidentStatus = Literal[
    "suspected", "no_response", "worker_ok", "cancelled", "acknowledged", "resolved"
]
Severity = Literal["critical", "warning", "info"]
Source = Literal["device", "voice", "server", "dashboard", "api"]

CLOSED = ("worker_ok", "cancelled", "resolved")
DEFAULT_SEVERITY = {
    "fall": "critical", "heat_stroke": "critical", "manual_sos": "critical",
    "tremor": "warning", "inactivity": "warning", "unwell": "warning",
    "impact": "info", "other": "warning",
}


class IncidentIn(BaseModel):
    """One incident report. Unknown top-level keys are rejected; nested objects are free-form."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_id: Literal["heatguard.incident.v1"] = Field(alias="schema")
    incident_id: str | None = Field(default=None, min_length=1, max_length=200)
    device_id: str = Field(min_length=1, max_length=128)
    type: IncidentType
    status: IncidentStatus | None = Field(default=None, description="default suspected")
    severity: Severity | None = None
    person: dict[str, Any] | None = None
    occurred_at: datetime | None = None
    boot_id: str | None = Field(default=None, max_length=64)
    read_time_us: int | None = Field(default=None, ge=0)
    location: dict[str, Any] | None = None
    details: dict[str, Any] | None = None
    source: Source | None = Field(default=None, description="default device")


# -- hook -------------------------------------------------------------------------------


async def on_incident(incident: dict, created: bool, previous: dict | None) -> dict | None:
    """Called after every stored create/update. Returns {"alert_id", "escalated"} to save on
    the incident, or None. No-op until HeatGuard's Core registers itself with set_hook()."""
    return None


_hook = on_incident


def set_hook(fn) -> None:
    global _hook
    _hook = fn or on_incident


# -- storage ----------------------------------------------------------------------------

_INSERT = """
insert into incidents (incident_id, device_id, person, type, status, severity, occurred_at,
                       received_at, updated_at, location, details, source, boot_id, read_time_us,
                       escalated)
values ($1, $2, $3::jsonb, $4, $5, $6, coalesce($7, now()), now(), now(), $8::jsonb,
        $9::jsonb, $10, $11, $12,
        '[]'::jsonb)
on conflict (incident_id) do nothing
returning *
"""

_UPDATE = """
update incidents set
    status = coalesce($2, status),
    severity = coalesce($3, severity),
    person = coalesce($4::jsonb, person),
    location = coalesce($5::jsonb, location),
    details = case when $6::jsonb is null then details
                   else coalesce(details, '{}'::jsonb) || $6::jsonb end,
    updated_at = now()
where incident_id = $1
returning *
"""


def _dumps(v) -> str | None:
    return None if v is None else json.dumps(v, default=str)


def row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("person", "location", "details", "escalated"):
        if isinstance(d.get(k), str):
            d[k] = json.loads(d[k])
    for k in ("occurred_at", "received_at", "updated_at"):
        if d.get(k) is not None:
            d[k] = d[k].astimezone(timezone.utc).isoformat()
    d["escalated"] = d.get("escalated") or []
    return {"schema": SCHEMA, **d}


def occurred_at(inc: IncidentIn, registry=None) -> datetime | None:
    """Given time, else the device clock (boot_id + read_time_us) mapped to wall time the same
    way the telemetry frames are, else None (the database stamps the receive time)."""
    if inc.occurred_at is not None:
        t = inc.occurred_at
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t
    if registry is not None and inc.boot_id and inc.read_time_us is not None:
        st = registry.get(inc.device_id)
        if st is not None and st.boot_id == inc.boot_id:
            return datetime.fromtimestamp(inc.read_time_us / 1e6 + st.clock_offset_s, timezone.utc)
    return None


async def record(pool, inc: IncidentIn, registry=None) -> tuple[dict, bool]:
    """Create or update one incident, run the hook, return (stored incident, created)."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            prev = await conn.fetchrow(
                "select * from incidents where incident_id = $1 for update", inc.incident_id
            ) if inc.incident_id else None
            if prev is None:
                iid = inc.incident_id or f"{inc.device_id}-{uuid.uuid4().hex[:12]}"
                status = inc.status or "suspected"
                row = await conn.fetchrow(
                    _INSERT, iid, inc.device_id, _dumps(inc.person or {"name": inc.device_id}),
                    inc.type, status,
                    inc.severity or DEFAULT_SEVERITY[inc.type], occurred_at(inc, registry),
                    _dumps(inc.location), _dumps(inc.details), inc.source or "device",
                    inc.boot_id, inc.read_time_us,
                )
                created = row is not None
                if row is None:  # lost a race with a concurrent create: treat as an update
                    prev = await conn.fetchrow(
                        "select * from incidents where incident_id = $1", iid
                    )
            else:
                created = False
            if not created:
                row = await conn.fetchrow(
                    _UPDATE, prev["incident_id"], inc.status, inc.severity, _dumps(inc.person),
                    _dumps(inc.location), _dumps(inc.details),
                )
    incident = row_to_dict(row)
    previous = row_to_dict(prev) if prev is not None else None
    try:
        extra = await _hook(incident, created, previous)
    except Exception:  # noqa: BLE001 - storing the incident must not fail because of the hook
        log.exception("incident hook failed for %s", incident["incident_id"])
        extra = None
    if extra:
        alert_id = extra.get("alert_id") or incident.get("alert_id")
        escalated = sorted(set(incident["escalated"]) | set(extra.get("escalated") or []))
        if alert_id != incident.get("alert_id") or escalated != incident["escalated"]:
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "update incidents set alert_id = $2, escalated = $3::jsonb "
                    "where incident_id = $1 returning *",
                    incident["incident_id"], alert_id, json.dumps(escalated),
                )
            incident = row_to_dict(row)
    return incident, created


# -- HTTP -------------------------------------------------------------------------------

router = APIRouter(tags=["incidents"])
Since = Query(None, description="ISO time; only incidents that occurred at or after it")
Limit = Query(50, ge=1, le=500)


def _svc(request: Request):
    return request.app.state.service


@router.post("/v1/incidents", status_code=201)
async def post_incident(inc: IncidentIn, request: Request):
    """Create an incident (201) or update one with the same `incident_id` (200)."""
    svc = _svc(request)
    incident, created = await record(svc.pool, inc, svc.registry)
    keys = ("incident_id", "status", "severity", "alert_id", "escalated")
    body = {k: incident.get(k) for k in keys}
    body["created"] = created
    return JSONResponse(body, status_code=201 if created else 200)


@router.get("/v1/incidents")
async def list_incidents(
    request: Request,
    device_id: str | None = None,
    type: IncidentType | None = None,  # noqa: A002 - query parameter name from the contract
    status: IncidentStatus | None = None,
    since: datetime | None = Since,
    limit: int = Limit,
):
    """Newest first."""
    where, args = [], []
    for col, val in (("device_id", device_id), ("type", type), ("status", status)):
        if val is not None:
            args.append(val)
            where.append(f"{col} = ${len(args)}")
    if since is not None:
        args.append(since if since.tzinfo else since.replace(tzinfo=timezone.utc))
        where.append(f"occurred_at >= ${len(args)}")
    args.append(limit)
    sql = (
        "select * from incidents"
        + (" where " + " and ".join(where) if where else "")
        + f" order by occurred_at desc, received_at desc limit ${len(args)}"
    )
    rows = await _svc(request).pool.fetch(sql, *args)
    return [row_to_dict(r) for r in rows]


@router.get("/v1/incidents/{incident_id}")
async def get_incident(incident_id: str, request: Request):
    row = await _svc(request).pool.fetchrow(
        "select * from incidents where incident_id = $1", incident_id
    )
    if row is None:
        raise HTTPException(404, "unknown incident")
    return row_to_dict(row)
