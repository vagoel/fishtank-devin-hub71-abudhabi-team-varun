"""Device WebSocket `/v1/device/ws` (docs/heatguard/device-ws.md).

One socket per wearable carries everything:
  text  JSON array   sticks3.telemetry.v1 frames -> the telemetry service (same call as
                     POST /v1/ingest/frames: Postgres, ring buffers, analytics) -> Core.on_frames
  text  JSON object  HeatGuard message with "k" (hello, st, ev) -> Core.on_device_msg
  binary             push-to-talk voice frame, first byte = type (H, A, E) -> the assistant
Server -> device: JSON commands ({"cmd": ...}) and binary voice frames (T, A, E, X), all through
one outbound queue per socket drained by its own sender task.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from telemetry.config import settings
from telemetry.models import TelemetryFrame
from telemetry.writer import QueueFull

from .core import core
from .services.assistant import DeviceGone

log = logging.getLogger("heatguard.device_ws")

router = APIRouter()

FRAMES = TypeAdapter(list[TelemetryFrame])
MAX_OUTBOUND = 4000          # queued messages before a device counts as stuck
CLOSE_BAD_TOKEN = 4401
CLOSE_BAD_REQUEST = 4400
CLOSE_REPLACED = 4409

links: dict[str, DeviceLink] = {}   # device_id -> its current socket


class _Close:
    def __init__(self, code: int, reason: str = ""):
        self.code, self.reason = code, reason


class _Voice:
    """One push-to-talk question on this socket."""

    def __init__(self, link: DeviceLink):
        self.link = link
        self.q: asyncio.Queue = asyncio.Queue()
        self.alive = True
        self.task: asyncio.Task | None = None

    async def recv(self):
        item = await self.q.get()
        if item is None:
            raise DeviceGone()
        return item

    async def send(self, kind: bytes, payload: bytes = b"") -> None:
        if not self.alive or not self.link.enqueue(kind + payload):
            raise DeviceGone()


class DeviceLink:
    """A connected wearable. Core.send_cmd(w, obj) ends up in send()."""

    def __init__(self, ws: WebSocket, device_id: str):
        self.ws = ws
        self.device_id = device_id
        self.open = True
        self.q: asyncio.Queue = asyncio.Queue()
        self.voice: _Voice | None = None

    # -- outbound (never blocks; the sender task writes in order)
    def enqueue(self, msg) -> bool:
        if not self.open:
            return False
        if self.q.qsize() >= MAX_OUTBOUND:
            log.warning("device %s: outbound queue full, closing", self.device_id)
            self.close(1013, "outbound queue full")
            return False
        self.q.put_nowait(msg)
        return True

    def send(self, obj: dict) -> bool:
        return self.enqueue(json.dumps(obj, separators=(",", ":"), default=str))

    def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.open:
            return
        self.open = False
        self.cancel_voice()
        self.q.put_nowait(_Close(code, reason))

    async def run_sender(self) -> None:
        try:
            while True:
                m = await self.q.get()
                if isinstance(m, _Close):
                    await self.ws.close(m.code, m.reason)
                    return
                if isinstance(m, bytes):
                    await self.ws.send_bytes(m)
                else:
                    await self.ws.send_text(m)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - socket gone
            log.debug("device %s: send failed: %s", self.device_id, e)
            self.open = False

    # -- voice
    def start_voice(self, header: bytes) -> None:
        """H: a new question. One at a time per device: it cancels the reply in progress."""
        self.cancel_voice()
        v = _Voice(self)
        v.q.put_nowait((b"H", header))
        self.voice = v
        v.task = asyncio.create_task(self._run_voice(v))

    async def _run_voice(self, v: _Voice) -> None:
        if core.assistant is None:
            await v.send(b"X", b"Voice assistant offline")
            return
        await core.assistant.run_session(self.device_id, v.recv, v.send, peer=self.device_id)

    def feed_voice(self, kind: bytes, payload: bytes) -> None:
        v = self.voice
        if v and v.alive and v.task and not v.task.done():
            v.q.put_nowait((kind, payload))
        # else: A / E after the question ended (e.g. after an X): ignored

    def cancel_voice(self) -> None:
        v, self.voice = self.voice, None
        if not v:
            return
        v.alive = False
        v.q.put_nowait(None)
        if v.task and not v.task.done():
            v.task.cancel()
        # drop reply frames still queued so they can't be mistaken for the next answer
        keep = []
        while not self.q.empty():
            m = self.q.get_nowait()
            if not isinstance(m, bytes):
                keep.append(m)
        for m in keep:
            self.q.put_nowait(m)


def _token_ok(ws: WebSocket) -> bool:
    want = os.environ.get("HEATGUARD_DEVICE_TOKEN", "").strip()
    if not want:
        return True                      # open socket (demo)
    got = ws.query_params.get("token") or ""
    auth = ws.headers.get("authorization") or ""
    if not got and auth[:7].lower() == "bearer ":
        got = auth[7:].strip()
    return hmac.compare_digest(got.encode(), want.encode())


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ()))
    return (f"{loc}: {err.get('msg')}" if loc else str(err.get("msg")))[:300]


@router.websocket("/v1/device/ws")
async def device_socket(ws: WebSocket):
    device_id = (ws.query_params.get("device_id") or "").strip()
    if not _token_ok(ws):
        await ws.close(code=CLOSE_BAD_TOKEN)     # before accept
        return
    if not device_id or len(device_id) > 128:
        await ws.close(code=CLOSE_BAD_REQUEST)
        return
    await ws.accept()
    link = DeviceLink(ws, device_id)
    old = links.get(device_id)
    links[device_id] = link
    if old is not None:
        old.close(CLOSE_REPLACED, "replaced by a newer connection")
    w = core.live.get(device_id)
    if w is not None and w.get("_transport") == "ws":
        w["_addr"] = link                        # commands go to the new socket right away
    log.info("device %s connected", device_id)
    sender = asyncio.create_task(link.run_sender())
    svc = getattr(ws.app.state, "service", None)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("text") is not None:
                _on_text(link, svc, msg["text"])
            elif msg.get("bytes"):
                data = msg["bytes"]
                if data[:1] == b"H":
                    link.start_voice(data[1:])
                else:
                    link.feed_voice(data[:1], data[1:])
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        link.open = False
        link.cancel_voice()
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)
        if links.get(device_id) is link:
            del links[device_id]
        log.info("device %s disconnected", device_id)


def _on_text(link: DeviceLink, svc, text: str) -> None:
    try:
        data = json.loads(text)
    except ValueError as e:
        link.send({"cmd": "error", "what": "json", "detail": str(e)[:200]})
        return
    if isinstance(data, list):
        _on_frames(link, svc, data)
    elif isinstance(data, dict) and "k" in data:
        if data.get("id") != link.device_id:
            data["id"] = link.device_id          # the socket's identity wins
        core.on_device_msg(data, "ws", link)
    else:
        link.send({"cmd": "error", "what": "message",
                   "detail": 'expected a JSON array of frames or an object with "k"'})


def _on_frames(link: DeviceLink, svc, data: list) -> None:
    if not data or len(data) > settings.max_batch_size:
        link.send({"cmd": "error", "what": "frames",
                   "detail": "empty frame array" if not data else
                   f"too many frames (max {settings.max_batch_size})"})
        return
    try:
        frames = FRAMES.validate_python(data)
    except ValidationError as e:
        link.send({"cmd": "error", "what": "frames", "detail": _first_error(e)})
        return
    other = next((f.device_id for f in frames if f.device_id != link.device_id), None)
    if other is not None:
        link.send({"cmd": "error", "what": "frames",
                   "detail": f"frame device_id {other!r} is not this socket's device_id"})
        return
    if svc is not None:
        try:
            svc.ingest_frames(frames)            # same path as POST /v1/ingest/frames
        except QueueFull:
            link.send({"cmd": "slow", "retry_ms": 1000})
            return
    core.on_frames(frames, transport="ws", addr=link)
    from . import detect  # server-side falls: a backup to the wearable's own detection
    detect.feed(link.ws.app, frames)


async def close_all() -> None:
    for link in list(links.values()):
        link.close(1012, "server restart")
    await asyncio.sleep(0)
