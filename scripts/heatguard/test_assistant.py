#!/usr/bin/env python3
"""Self-test for server/services/assistant.py.

Runs the voice relay end to end against local mock OpenAI servers (no network,
no key needed):

  - no key: every connection gets X "Voice assistant offline"
  - GPT-Live engine: session.start, audio streaming, real-time silence after the
    release, Responses delegation with a get_heat_status call, answer audio
    forwarded, end of answer detected; also an answer that starts before the
    button is released, a model that stays silent until nudged, a device that
    disconnects, a rejected key (HTTP 401), and a too-short question
  - Realtime engine (GA and beta event names): session.update, append/commit,
    function call round trip, response.done; plus the 45 s overall timeout

  .venv/bin/python scripts/test_assistant.py            # mock tests
  .venv/bin/python scripts/test_assistant.py --live "It is very hot and I feel dizzy, what should I do?"
      # one real call: Assistant + fake Core on a spare port, driven by voice_client.py
"""
import argparse
import asyncio
import base64
import json
import logging
import math
import os
import sys
import tempfile
import time
from array import array
from http import HTTPStatus
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # repo root
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts" / "heatguard"))

from websockets.asyncio.server import serve  # noqa: E402

from heatguard.services.assistant import MAX_FRAME, MAX_TEXT, RATE, Assistant, _level, screen_text  # noqa: E402
from voice_client import ask  # noqa: E402

DEVICE = "stick-test"
TOOL_NAMES = {"report_symptoms", "request_help", "start_cool_down", "get_heat_status"}


def tone(seconds: float, freq: float = 220.0, amp: int = 8000) -> bytes:
    n = int(RATE * seconds)
    a = array("h", (int(amp * math.sin(2 * math.pi * freq * (i + 0.5) / RATE)) for i in range(n)))
    if sys.byteorder == "big":
        a.byteswap()
    return a.tobytes()


def silence(seconds: float) -> bytes:
    return bytes(int(RATE * 2 * seconds))


class FakeCore:
    def __init__(self):
        self.voice: dict[str, dict] = {}
        self.voice_log: list[dict] = []
        self.tools: list[tuple] = []

    def worker_context(self, device_id):
        return {
            "known": True,
            "worker": {"id": "LIVE-7ce8b1e3", "name": "Ramesh Kumar", "trade": "Steel fixer",
                       "zone": "Block C", "crew": "C-3", "live": True, "acclimatized": True,
                       "status": "caution", "workload": "light", "phase": "work", "phase_reason": "",
                       "work_elapsed_min": 12.5, "work_budget_min": 30, "rest_remaining_min": 0,
                       "rest_required_min": 30, "shift_exposure_min": 184.0, "open_alerts": 0},
            "site": {"name": "Dubai - Site 7 (demo)", "lat": 25.2, "lon": 55.27, "temp_c": 38.4,
                     "rh": 52, "wbgt_c": 32.1,
                     "category": {"level": 3, "label": "Very high", "color": "#ef4444"},
                     "midday_break": {"active": False, "in_season": True, "window": "12:30–15:00",
                                      "season": "15 Jun – 15 Sep"},
                     "local_time": "10:05"},
            "open_alerts": [],
        }

    def publish_voice(self, entry):
        self.voice_log.append(dict(entry))
        self.voice[entry["id"]] = dict(entry)

    async def assistant_tool(self, device_id, name, args):
        self.tools.append((device_id, name, dict(args)))
        if name == "get_heat_status":
            return {"ok": True, "wbgt_c": 32.1, "category": "Very high", "phase": "work",
                    "work_elapsed_min": 12.5, "work_left_min": 17.5, "rest_remaining_min": 0}
        if name == "request_help":
            return {"ok": True, "alert_id": "A-000001", "message": "Supervisor alerted, help is on the way"}
        if name == "report_symptoms":
            return {"ok": True, "alert_id": "A-000002", "cool_down_min": 15}
        if name == "start_cool_down":
            return {"ok": True, "rest_min": args.get("minutes", 15)}
        return {"ok": False}

    def by_role(self, role):
        return [e for e in self.voice.values() if e["role"] == role]

    async def wait_final(self, role, timeout=5.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            done = [e for e in self.by_role(role) if e["final"]]
            if done:
                return done
            await asyncio.sleep(0.05)
        return []


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_frames(res, expect_audio: bytes | None = None):
    kinds = [k for k, _ in res["frames"]]
    check(res["error"] is None, f"unexpected X frame: {res['error']}")
    check(res["ended"] and kinds and kinds[-1] == b"E", f"reply must end with E, got {kinds[-5:]}")
    check(set(kinds[:-1]) <= {b"A", b"T"}, f"only A/T before E, got {set(kinds)}")
    for k, p in res["frames"]:
        if k == b"A":
            check(0 < len(p) <= MAX_FRAME and len(p) % 2 == 0, f"bad A frame size {len(p)}")
        if k == b"T":
            check(len(p) <= MAX_TEXT, f"T frame too long ({len(p)} bytes)")
    check(res["texts"], "no T frames")
    if expect_audio is not None:
        got = bytes(res["audio"])
        check(got[:len(expect_audio)] == expect_audio,
              f"reply audio mismatch ({len(got)} bytes, expected {len(expect_audio)} first)")
        check(not any(got[len(expect_audio):]), "only silence may follow the answer audio")


def check_log(core: FakeCore, worker_text: str, assistant_text: str, tool_text: str):
    workers = [e for e in core.by_role("worker") if e["final"]]
    assistants = [e for e in core.by_role("assistant") if e["final"]]
    tools = core.by_role("tool")
    check(len(workers) == 1 and workers[0]["text"] == worker_text, f"worker entry: {workers}")
    check(len(assistants) == 1 and assistants[0]["text"] == assistant_text, f"assistant entry: {assistants}")
    check(len(tools) == 1 and tools[0]["text"] == tool_text and tools[0]["final"], f"tool entry: {tools}")
    turns = {e["turn_id"] for e in core.voice_log}
    check(len(turns) == 1, f"one turn_id per question, got {turns}")
    turn = turns.pop()
    check({e["id"] for e in core.voice_log} == {f"V-{turn}-worker", f"V-{turn}-assistant", f"V-{turn}-tool"},
          f"entry ids: {sorted({e['id'] for e in core.voice_log})}")
    for e in core.voice_log:
        check(e["worker_id"] == "LIVE-7ce8b1e3" and e["worker_name"] == "Ramesh Kumar", f"worker fields: {e}")
        check(isinstance(e["ts"], float), "ts must be float")


# ---------------------------------------------------------------- mock GPT-Live

LIVE_ANSWER = [("Let me check. [chuckle] ", tone(0.2, 440)),
               ("It is very hot, WBGT 32. ", tone(0.3, 330)),
               ("Drink water now and rest in about 17 minutes.", tone(0.3, 550))]


class MockLive:
    """Speaks the Live primary WebSocket protocol for one scripted question."""

    def __init__(self, continuous: bool = True, lazy: bool = False):
        self.continuous = continuous      # keep sending silent output audio after the answer
        self.lazy = lazy                  # wait for an instructions.append before answering
        self.start = None
        self.headers = None
        self.path = None
        self.events: list[dict] = []
        self.audio = bytearray()
        self.continued = asyncio.Event()
        self.closed = asyncio.Event()
        self.t_turn = None

    async def send(self, ws, ev):
        await ws.send(json.dumps(ev))

    async def handler(self, ws):
        self.path, self.headers = ws.request.path, ws.request.headers
        turn = None
        try:
            self.start = json.loads(await ws.recv())
            self.events.append(self.start)
            await self.send(ws, {"type": "session.started", "event_id": "ev_start",
                                 "client_event_id": self.start.get("event_id"),
                                 "session": {"id": "live_mock", "status": "active",
                                             "model": self.start["session"]["model"]}})
            voice_ms = silent_ms = 0.0
            partial = False
            async for raw in ws:
                ev = json.loads(raw)
                t = ev["type"]
                if t != "session.input_audio.append":
                    self.events.append(ev)
                if t == "session.input_audio.append":
                    pcm = base64.b64decode(ev["audio"])
                    self.audio += pcm
                    ms = len(pcm) / (RATE * 2) * 1000
                    if _level(pcm) > 100:
                        voice_ms, silent_ms = voice_ms + ms, 0.0
                    elif voice_ms:
                        silent_ms += ms
                    if voice_ms >= 300 and not partial:
                        partial = True
                        await self.send(ws, {"type": "session.input_transcript.delta", "event_id": "ev_in1",
                                             "delta": "How hot is it", "start_ms": 100, "end_ms": 400})
                    if voice_ms and silent_ms >= 400 and turn is None and not self.lazy:
                        await self.send(ws, {"type": "session.input_transcript.delta", "event_id": "ev_in2",
                                             "delta": " right now?", "start_ms": 400, "end_ms": 900})
                        turn = asyncio.create_task(self.answer(ws))
                elif t == "response.create":
                    self.continued.set()
                elif t == "session.instructions.append":
                    await self.send(ws, {"type": "session.instructions.appended", "event_id": "ev_ia",
                                         "client_event_id": ev.get("event_id"), "start_ms": 0, "end_ms": 0})
                    if turn is None:
                        await self.send(ws, {"type": "session.input_transcript.delta", "event_id": "ev_in2",
                                             "delta": " right now?", "start_ms": 400, "end_ms": 900})
                        turn = asyncio.create_task(self.answer(ws))
                elif t == "session.close":
                    await self.send(ws, {"type": "session.closed", "event_id": "ev_closed",
                                         "client_event_id": ev.get("event_id"), "reason": "close_requested",
                                         "usage": {"seconds": round(len(self.audio) / (RATE * 2), 1)}})
                    break
        finally:
            if turn:
                turn.cancel()
            self.closed.set()

    async def answer(self, ws):
        self.t_turn = time.monotonic()
        text, audio = LIVE_ANSWER[0]
        await self.send(ws, {"type": "session.output_transcript.delta", "event_id": "o1", "delta": text,
                             "start_ms": 1000, "end_ms": 1200})
        await self.send(ws, {"type": "session.output_audio.delta", "delta": base64.b64encode(audio).decode()})
        await self.send(ws, {"type": "session.delegation.created", "event_id": "d1", "offset_ms": 1000,
                             "delegation": {"id": "del_1", "type": "delegation", "target": "responses",
                                            "response_id": "resp_1"}})

        async def backend(ev):
            await self.send(ws, {"type": "response.event", "event_id": "r", "delegation_id": "del_1", "event": ev})

        await backend({"type": "response.created", "response": {"id": "resp_1", "status": "in_progress", "output": []}})
        await backend({"type": "response.output_item.done", "output_index": 0,
                       "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                                "name": "get_heat_status", "arguments": "{}", "status": "completed"}})
        await backend({"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": []}})
        await asyncio.wait_for(self.continued.wait(), 5)
        await backend({"type": "response.created", "response": {"id": "resp_2", "status": "in_progress", "output": []}})
        await backend({"type": "response.output_text.delta", "item_id": "msg_1", "delta": "WBGT 32.1, 17 min left."})
        await backend({"type": "response.completed", "response": {"id": "resp_2", "status": "completed", "output": []}})
        await asyncio.sleep(0.2)
        for text, audio in LIVE_ANSWER[1:]:
            await self.send(ws, {"type": "session.output_transcript.delta", "event_id": "o", "delta": text,
                                 "start_ms": 2000, "end_ms": 2500})
            await self.send(ws, {"type": "session.output_audio.delta", "delta": base64.b64encode(audio).decode()})
        while self.continuous:
            await self.send(ws, {"type": "session.output_audio.delta", "delta": base64.b64encode(silence(0.1)).decode()})
            await asyncio.sleep(0.1)


async def run_live(early: bool, continuous: bool, lazy: bool = False):
    mock = MockLive(continuous=continuous, lazy=lazy)
    async with serve(mock.handler, "127.0.0.1", 0) as srv:
        mport = srv.sockets[0].getsockname()[1]
        core = FakeCore()
        a = Assistant(core, api_key="test-key-not-real", engine="live", port=0,
                      url=f"ws://127.0.0.1:{mport}/v1/live/sessions")
        a.live_quiet_s = 0.6
        a.live_nudge_after_s = 0.8
        await a.start()
        try:
            question = tone(1.0) + (silence(0.7) if early else b"")
            res = await ask("127.0.0.1", a.bound_port, DEVICE, question, speed=1.0, timeout=20, quiet=True)
            await asyncio.wait_for(mock.closed.wait(), 5)
            await core.wait_final("worker")
            await asyncio.sleep(0.1)
        finally:
            await a.stop()

    full = "".join(t for t, _ in LIVE_ANSWER).replace(" [chuckle]", "").strip()
    check_frames(res, b"".join(a_ for _, a_ in LIVE_ANSWER))
    check(res["texts"][-1] == screen_text(full), f"last T: {res['texts'][-1]!r}")
    if early:
        check(mock.t_turn and res["t_release"] and mock.t_turn < res["t_release"],
              "mock answer should start before the button release")

    s = mock.start["session"]
    check(mock.path == "/v1/live/sessions", f"path {mock.path}")
    check(mock.headers.get("Authorization") == "Bearer test-key-not-real", "Authorization header")
    check(mock.start["type"] == "session.start" and s["model"] == "gpt-live-1", "session.start model")
    check(s["audio"] == {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": "marin"}}, s["audio"])
    d = s["delegation"]
    check(d["type"] == "responses" and d["responses"]["model"] == a.backend_model, f"delegation {d}")
    check({t["name"] for t in d["responses"]["tools"]} == TOOL_NAMES, "backend tools")
    check("32.1" in s["instructions"] and "Ramesh Kumar" in s["instructions"], "live context in instructions")

    check(bytes(mock.audio[:len(question)]) == question, "device audio forwarded unchanged")
    check(len(mock.audio) > len(question) and not any(mock.audio[len(question):]),
          "silence must follow the question")
    types = [e["type"] for e in mock.events]
    items = [e for e in mock.events if e["type"] == "response.item.create"]
    check(len(items) == 1 and items[0]["item"]["type"] == "function_call_output"
          and items[0]["item"]["call_id"] == "call_1"
          and json.loads(items[0]["item"]["output"])["wbgt_c"] == 32.1, f"tool output {items}")
    check(types.index("response.item.create") < types.index("response.create"), "result before response.create")
    check("session.close" in types, "session.close sent")
    nudges = [e for e in mock.events if e["type"] == "session.instructions.append"]
    if lazy:
        check(len(nudges) == 1 and nudges[0]["delegation_id"] is None, f"nudge {nudges}")
    else:
        check(not nudges, f"no nudge when the model answers by itself: {nudges}")

    check(core.tools == [(DEVICE, "get_heat_status", {})], f"tools {core.tools}")
    check_log(core, "How hot is it right now?", full, "get_heat_status()")


async def test_live():
    await run_live(early=False, continuous=True)


async def test_live_answer_before_release():
    await run_live(early=True, continuous=False)


async def test_live_nudge_when_silent():
    await run_live(early=False, continuous=True, lazy=True)


async def test_live_device_disconnect():
    mock = MockLive()
    async with serve(mock.handler, "127.0.0.1", 0) as srv:
        mport = srv.sockets[0].getsockname()[1]
        core = FakeCore()
        a = Assistant(core, api_key="test-key-not-real", engine="live", port=0,
                      url=f"ws://127.0.0.1:{mport}/v1/live/sessions")
        await a.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", a.bound_port)
            writer.write(b"H" + len(b'{"id":"stick-test"}').to_bytes(2, "big") + b'{"id":"stick-test"}')
            pcm = tone(0.5)
            for off in range(0, len(pcm), MAX_FRAME):
                chunk = pcm[off:off + MAX_FRAME]
                writer.write(b"A" + len(chunk).to_bytes(2, "big") + chunk)
                await writer.drain()
                await asyncio.sleep(0.1)
            writer.close()   # wrist drops off WiFi mid-question
            await asyncio.wait_for(mock.closed.wait(), 5)
            await asyncio.sleep(0.2)
            check(a._active == 0, "turn still active after disconnect")
        finally:
            await a.stop()
    check("session.close" in [e["type"] for e in mock.events], "live session closed after device left")
    check(not core.tools, "no tools on a dropped question")


async def test_live_rejected_key():
    async def deny(connection, request):
        return connection.respond(HTTPStatus.UNAUTHORIZED, "invalid key\n")

    async def never(ws):
        pass

    async with serve(never, "127.0.0.1", 0, process_request=deny) as srv:
        mport = srv.sockets[0].getsockname()[1]
        a = Assistant(FakeCore(), api_key="test-key-not-real", engine="live", port=0,
                      url=f"ws://127.0.0.1:{mport}/v1/live/sessions")
        await a.start()
        try:
            res = await ask("127.0.0.1", a.bound_port, DEVICE, tone(0.5), speed=0, timeout=10, quiet=True)
        finally:
            await a.stop()
    check(res["error"] == "Voice assistant unavailable", f"X frame: {res['error']}")
    check("401" in (a.describe()["reason"] or ""), f"describe: {a.describe()}")


async def test_short_question():
    mock = MockLive()
    async with serve(mock.handler, "127.0.0.1", 0) as srv:
        mport = srv.sockets[0].getsockname()[1]
        a = Assistant(FakeCore(), api_key="test-key-not-real", engine="live", port=0,
                      url=f"ws://127.0.0.1:{mport}/v1/live/sessions")
        await a.start()
        try:
            res = await ask("127.0.0.1", a.bound_port, DEVICE, tone(0.1), speed=0, timeout=10, quiet=True)
        finally:
            await a.stop()
    check(res["error"] and res["error"].startswith("I didn't hear anything"), f"X frame: {res['error']}")


# ---------------------------------------------------------------- mock Realtime

RT_ANSWER = [("It is very hot, ", tone(0.15, 440)), ("WBGT is 32. ", tone(0.15, 330)),
             ("Drink water now and rest in about 17 minutes.", tone(0.2, 550))]


class MockRealtime:
    def __init__(self, beta: bool = False, stall: bool = False):
        self.beta, self.stall = beta, stall
        self.events: list[dict] = []
        self.audio = bytearray()
        self.path = self.headers = None
        self.closed = asyncio.Event()

    async def send(self, ws, ev):
        await ws.send(json.dumps(ev))

    async def handler(self, ws):
        self.path, self.headers = ws.request.path, ws.request.headers
        await self.send(ws, {"type": "session.created", "session": {"type": "realtime"}})
        n = 0
        try:
            async for raw in ws:
                ev = json.loads(raw)
                t = ev["type"]
                self.events.append(ev if t != "input_audio_buffer.append" else {"type": t})
                if t == "session.update":
                    await self.send(ws, {"type": "session.updated", "session": ev["session"]})
                elif t == "input_audio_buffer.append":
                    self.audio += base64.b64decode(ev["audio"])
                elif t == "input_audio_buffer.commit":
                    await self.send(ws, {"type": "input_audio_buffer.committed", "item_id": "item_user"})
                elif t == "response.create" and not self.stall:
                    n += 1
                    await (self.tool_call(ws) if n == 1 else self.answer(ws))
        finally:
            self.closed.set()

    async def transcript(self, ws):
        await self.send(ws, {"type": "conversation.item.input_audio_transcription.delta",
                             "item_id": "item_user", "delta": "How hot"})
        await self.send(ws, {"type": "conversation.item.input_audio_transcription.completed",
                             "item_id": "item_user", "transcript": "How hot is it right now?"})

    async def tool_call(self, ws):
        if self.beta:
            await self.transcript(ws)   # beta variant: transcript before the answer
        call = {"type": "function_call", "id": "item_fc", "call_id": "call_1",
                "name": "get_heat_status", "arguments": "{}", "status": "completed"}
        await self.send(ws, {"type": "response.created", "response": {"id": "resp_1"}})
        await self.send(ws, {"type": "response.output_item.added", "item": dict(call, arguments="")})
        await self.send(ws, {"type": "response.function_call_arguments.done", "call_id": "call_1",
                             "item_id": "item_fc", "arguments": "{}"})
        await self.send(ws, {"type": "response.done",
                             "response": {"id": "resp_1", "status": "completed", "output": [call]}})

    async def answer(self, ws):
        audio_t = "response.audio.delta" if self.beta else "response.output_audio.delta"
        text_t = "response.audio_transcript" if self.beta else "response.output_audio_transcript"
        await self.send(ws, {"type": "response.created", "response": {"id": "resp_2"}})
        for text, audio in RT_ANSWER:
            await self.send(ws, {"type": f"{text_t}.delta", "item_id": "item_asst", "delta": text})
            await self.send(ws, {"type": audio_t, "item_id": "item_asst",
                                 "delta": base64.b64encode(audio).decode()})
        full = "".join(t for t, _ in RT_ANSWER)
        await self.send(ws, {"type": f"{text_t}.done", "item_id": "item_asst", "transcript": full})
        await self.send(ws, {"type": "response.done", "response": {"id": "resp_2", "status": "completed",
                             "output": [{"type": "message", "role": "assistant"}]}})
        if not self.beta:
            await asyncio.sleep(0.3)
            await self.transcript(ws)   # GA variant: transcript arrives after the answer


async def run_realtime(beta: bool):
    mock = MockRealtime(beta=beta)
    async with serve(mock.handler, "127.0.0.1", 0) as srv:
        mport = srv.sockets[0].getsockname()[1]
        core = FakeCore()
        a = Assistant(core, api_key="test-key-not-real", engine="realtime", port=0,
                      url=f"ws://127.0.0.1:{mport}/v1/realtime")
        await a.start()
        try:
            question = tone(1.0)
            res = await ask("127.0.0.1", a.bound_port, DEVICE, question, speed=0, timeout=10, quiet=True)
            await asyncio.wait_for(mock.closed.wait(), 5)
            await core.wait_final("worker")
        finally:
            await a.stop()

    full = "".join(t for t, _ in RT_ANSWER)
    check_frames(res, b"".join(a_ for _, a_ in RT_ANSWER))
    check(len(res["audio"]) == sum(len(a_) for _, a_ in RT_ANSWER), "exact reply audio")
    check(res["texts"][-1] == screen_text(full), f"last T: {res['texts'][-1]!r}")
    check(mock.path == "/v1/realtime?model=gpt-realtime-2.1", f"path {mock.path}")
    check(mock.headers.get("Authorization") == "Bearer test-key-not-real", "Authorization header")
    types = [e["type"] for e in mock.events]
    su = mock.events[0]
    check(su["type"] == "session.update", "session.update first")
    s = su["session"]
    check(s["type"] == "realtime" and s["output_modalities"] == ["audio"], "session type")
    check(s["audio"]["input"]["turn_detection"] is None, "server VAD off")
    check(s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}, "input format")
    check(s["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}, "output format")
    check(s["audio"]["input"]["transcription"]["model"] == a.transcribe_model, "input transcription")
    check({t["name"] for t in s["tools"]} == TOOL_NAMES, "session tools")
    check("32.1" in s["instructions"] and "998" in s["instructions"], "context in instructions")
    check(bytes(mock.audio) == question, "device audio forwarded unchanged")
    i_commit = types.index("input_audio_buffer.commit")
    check(set(types[1:i_commit]) == {"input_audio_buffer.append"}, "appends before commit")
    check(types[i_commit + 1] == "response.create", "response.create after commit")
    item = mock.events[i_commit + 2]
    check(item["type"] == "conversation.item.create" and item["item"]["type"] == "function_call_output"
          and item["item"]["call_id"] == "call_1", f"function_call_output: {item}")
    check(types[i_commit + 3] == "response.create", "response.create after the tool output")
    check(core.tools == [(DEVICE, "get_heat_status", {})], f"tools {core.tools}")
    check_log(core, "How hot is it right now?", full, "get_heat_status()")


async def test_realtime_ga():
    await run_realtime(beta=False)


async def test_realtime_beta_names():
    await run_realtime(beta=True)


async def test_realtime_timeout():
    mock = MockRealtime(stall=True)
    async with serve(mock.handler, "127.0.0.1", 0) as srv:
        mport = srv.sockets[0].getsockname()[1]
        a = Assistant(FakeCore(), api_key="test-key-not-real", engine="realtime", port=0, timeout=2.0,
                      url=f"ws://127.0.0.1:{mport}/v1/realtime")
        await a.start()
        try:
            t0 = time.monotonic()
            res = await ask("127.0.0.1", a.bound_port, DEVICE, tone(0.5), speed=0, timeout=10, quiet=True)
            took = time.monotonic() - t0
        finally:
            await a.stop()
    check(res["error"] and res["error"].startswith("Sorry, that took too long"), f"X frame: {res['error']}")
    check(took < 4.0, f"timeout took {took:.1f}s")


# ---------------------------------------------------------------- misc

async def test_offline():
    core = FakeCore()
    a = Assistant(core, api_key="", port=0)
    await a.start()
    try:
        d = a.describe()
        check(d["enabled"] is False and d["reason"] == "OPENAI_API_KEY missing" and d["model"], f"describe {d}")
        res = await ask("127.0.0.1", a.bound_port, DEVICE, tone(0.5), speed=0, timeout=10, quiet=True)
    finally:
        await a.stop()
    check([k for k, _ in res["frames"]] == [b"X"] and res["error"] == "Voice assistant offline",
          f"frames {res['frames']}")
    check(not core.voice_log and not core.tools, "offline must not log or call tools")


async def test_screen_text():
    check(screen_text("") == "", "empty")
    check(screen_text("Drink water. Rest now.") == "Drink water. Rest now.", "two sentences")
    long = "Stop work now and sit in the shade. " * 6
    out = screen_text(long)
    check(len(out.encode()) <= MAX_TEXT, "limit")
    hindi = "काम रोकिए और छाँव में बैठिए। " * 3 + "पानी पीजिए और सुपरवाइज़र को बताइए कि आपको चक्कर आ रहा है और सिर में दर्द है।"
    out = screen_text(hindi)
    check(len(out.encode()) <= MAX_TEXT and out.encode().decode() == out, f"utf-8 safe: {out!r}")


TESTS = [test_screen_text, test_offline, test_live, test_live_answer_before_release, test_live_nudge_when_silent,
         test_live_device_disconnect, test_live_rejected_key, test_short_question,
         test_realtime_ga, test_realtime_beta_names, test_realtime_timeout]


async def run_tests() -> int:
    failed = 0
    for fn in TESTS:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(fn(), 60)
            print(f"PASS  {fn.__name__} ({time.monotonic() - t0:.1f}s)")
        except Exception as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(TESTS) - failed}/{len(TESTS)} passed")
    return 1 if failed else 0


# ---------------------------------------------------------------- one real call

async def live_call(text: str, voice: str | None, port: int, engine: str | None, out: str) -> int:
    core = FakeCore()
    a = Assistant(core, port=port, engine=engine)
    print("assistant:", a.describe())
    if not a.enabled:
        return 1
    await a.start()
    try:
        cmd = [sys.executable, str(ROOT / "scripts" / "heatguard" / "voice_client.py"), "--port", str(a.bound_port),
               "--id", "stick-live", "--text", text, "--out", out]
        if voice:
            cmd += ["--voice", voice]
        proc = await asyncio.create_subprocess_exec(*cmd)
        rc = await proc.wait()
        await asyncio.sleep(4)   # session.close + final voice-log entries
    finally:
        await a.stop()
    print("\ntool calls:")
    for dev, name, args in core.tools:
        print(f"  {name}({json.dumps(args, ensure_ascii=False)})")
    print("voice log:")
    for e in core.voice.values():
        print(f"  [{e['role']}{'' if e['final'] else ' partial'}] {e['text']}")
    print("assistant after call:", a.describe())
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", metavar="TEXT", help="one real call through OpenAI with this question")
    ap.add_argument("--voice", help="macOS `say` voice for --live, e.g. Lekha for Hindi")
    ap.add_argument("--engine", choices=["live", "realtime"], help="override HEATGUARD_VOICE_ENGINE for --live")
    ap.add_argument("--port", type=int, default=47912, help="spare TCP port for --live")
    ap.add_argument("--out", default=os.path.join(tempfile.gettempdir(), "heatguard_live_reply.wav"))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--debug", action="store_true", help="log every non-audio OpenAI event")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO if (args.verbose or args.live) else logging.CRITICAL,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.debug:
        logging.getLogger("heatguard.assistant").setLevel(logging.DEBUG)
    if args.live:
        return asyncio.run(live_call(args.live, args.voice, args.port, args.engine, args.out))
    return asyncio.run(run_tests())


if __name__ == "__main__":
    sys.exit(main())
