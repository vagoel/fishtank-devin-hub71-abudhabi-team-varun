"""Shared test setup. HeatGuard runs inside the telemetry app, so keep it offline for every
test module: no .env, no real keys, no weather/Devin/Twilio/OpenAI traffic. Any attempt to
reach a non-local host is refused and recorded in BLOCKED."""

import ipaddress
import os
import socket

os.environ["HEATGUARD_ENV_FILE"] = os.devnull
for _k in (
    "OPENAI", "OPENAI_API_KEY", "DEVIN", "DEVIN_API_KEY", "TWILLIO_SID", "TWILLIO_KEY",
    "TWILIO_SID", "TWILIO_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
    "TWILIO_CALL_FROM", "WHATSAPP_TO", "CALL_TO", "LIVE_CALL_TO", "WHATSAPP_PROVIDER",
    "HEATGUARD_DEVICE_TOKEN", "HEATGUARD_PUBLIC_URL", "LIVE_CALL_API_TOKEN", "META_WA_TOKEN",
    "CALLMEBOT_APIKEY",
):
    os.environ.pop(_k, None)
os.environ.update(
    HEATGUARD_WEATHER="0", HEATGUARD_AUTO_DEVIN="0", HEATGUARD_AUTO_WHATSAPP="0",
    HEATGUARD_CALLS="0", HEATGUARD_LAN="0",
)

BLOCKED: list[str] = []
_LOCAL_NAMES = {"localhost", "testserver", ""}
_real_getaddrinfo = socket.getaddrinfo
_real_connect = socket.socket.connect


def _local(host) -> bool:
    if host is None or host in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(str(host).split("%")[0]).is_loopback
    except ValueError:
        return False


def _guarded_getaddrinfo(host, *args, **kwargs):
    if not _local(host if not isinstance(host, bytes) else host.decode()):
        BLOCKED.append(str(host))
        raise OSError(f"network access blocked in tests: {host}")
    return _real_getaddrinfo(host, *args, **kwargs)


def _guarded_connect(self, address):
    if isinstance(address, tuple) and not _local(address[0]):
        BLOCKED.append(str(address[0]))
        raise OSError(f"network access blocked in tests: {address[0]}")
    return _real_connect(self, address)


socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_connect
