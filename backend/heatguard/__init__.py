"""HeatGuard, mounted in the telemetry app (telemetry/main.py).

    app.include_router(heatguard.router)            # dashboard /, /api/v1/*, /v1/device/ws
    app.include_router(heatguard.livecall.router)   # /twilio/* webhooks + media, /v1/calls
    app.include_router(heatguard.incidents.router)  # /v1/incidents
    await heatguard.startup(service) / await heatguard.shutdown()   # in the lifespan
"""
from .env import load_env

load_env()  # before any module reads its settings

from fastapi import APIRouter  # noqa: E402

from . import core as _core  # noqa: E402
from . import device_ws, incidents, livecall  # noqa: E402
from .core import core  # noqa: E402

router = APIRouter()
router.include_router(_core.router)
router.include_router(device_ws.router)

_users = 0   # lifespans using HeatGuard (tests may open several TestClients on one app)


async def startup(service=None) -> None:
    """Start HeatGuard's background tasks. `service` is the telemetry TelemetryService
    (its pool and device registry back the incidents table)."""
    global _users
    _users += 1
    if _users == 1:
        core.service = service
        incidents.set_hook(core.on_incident, core.prepare_incident)
        await _core.startup()


async def shutdown() -> None:
    global _users
    _users = max(0, _users - 1)
    if _users == 0:
        await device_ws.close_all()
        await _core.shutdown()
        core.service = None


__all__ = ["core", "device_ws", "incidents", "livecall", "router", "shutdown", "startup"]
