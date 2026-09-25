"""HeatGuard voice assistant: push-to-talk relay between the wrist device and OpenAI.

Device side (TCP :47802, docs/services.md §3), one connection per question:
  device -> server  H {"id","rate","fmt"}, A (PCM16 LE mono 24 kHz, <= 4800 B), E
  server -> device  T (screen text), A (reply audio), then E (done) or X (error); close

Two engines, picked with HEATGUARD_VOICE_ENGINE:

  live (default)  GPT-Live, model gpt-live-1, wss://api.openai.com/v1/live/sessions.
                  Full duplex: the live model decides when to speak. The device is
                  half duplex (mic and speaker share I2S), so after the button is
                  released the relay keeps feeding real-time silence and treats
                  ~1.5 s without voiced output, with no backend work pending, as
                  the end of the answer (GPT-Live has no end-of-response event).
                  If it stays silent for 6 s after the release, one
                  session.instructions.append asks it to answer now.
                  Tools run through Responses delegation: the backend model's
                  function calls arrive as nested response.output_item.done events
                  inside response.event; results go back with response.item.create
                  (function_call_output) followed by response.create.

  realtime        OpenAI Realtime GA, model gpt-realtime-2.1,
                  wss://api.openai.com/v1/realtime?model=... Push-to-talk maps
                  directly: turn_detection null, input_audio_buffer.append per A
                  frame, commit + response.create on E, response.done ends the
                  answer. Tools are session functions (conversation.item.create
                  function_call_output, then response.create).

Both engines run at 24 kHz PCM16, the device's native format, so audio passes
through without resampling.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import os
import re
import struct
import sys
import time
import uuid
from array import array
from pathlib import Path
from urllib.parse import urlsplit

try:
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
except ImportError:  # the server still starts and answers every device with X
    ws_connect = None
    ConnectionClosed = InvalidStatus = WebSocketException = Exception

log = logging.getLogger("heatguard.assistant")

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
REALTIME_URL = "wss://api.openai.com/v1/realtime"
LIVE_MODEL = "gpt-live-1"
REALTIME_MODEL = "gpt-realtime-2.1"
LIVE_BACKEND = "gpt-5.6-luna"
DEFAULT_VOICE = "marin"
TRANSCRIBE_MODEL = "gpt-4o-mini-transcribe"

RATE = 24000                    # Hz, device and API
MAX_FRAME = 4800                # bytes per A frame (100 ms)
MAX_TEXT = 120                  # bytes per T / X frame
MIN_AUDIO = RATE * 2 * 3 // 10  # 0.3 s; shorter is an accidental press
CHUNK_S = 0.1                   # silence pacing after the button is released
SILENCE_B64 = base64.b64encode(bytes(int(RATE * 2 * CHUNK_S))).decode("ascii")
VOICE_LEVEL = 150               # mean |sample| above this counts as speech
T_INTERVAL = 0.35               # s between T frames
LOG_INTERVAL = 0.5              # s between streaming voice-log updates
TOOL_TIMEOUT = 10.0
MAX_TOOL_ROUNDS = 3             # realtime: tool call -> answer loops per question
MAX_SESSIONS = 6                # concurrent questions
START_TIMEOUT = 10.0            # live: session.start -> session.started
BACKEND_MAX_S = 20.0            # live: give up waiting on one delegation
TRANSCRIPT_LINGER = 2.5         # realtime: wait for the worker transcript after E

PROJECT_ROOT = Path(__file__).resolve().parents[2]

TOOLS = [
    {
        "type": "function",
        "name": "report_symptoms",
        "description": (
            "Log symptoms the worker reports (headache, dizziness, cramps, nausea, heavy "
            "sweating, weakness, feeling unwell). Alerts the supervisor and starts a "
            "cool-down. For danger signs use request_help instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symptoms": {"type": "string",
                             "description": "The symptoms in a few English words, e.g. 'dizzy, headache'."},
                "severity": {"type": "string", "enum": ["mild", "moderate", "severe"]},
            },
            "required": ["symptoms", "severity"],
        },
    },
    {
        "type": "function",
        "name": "request_help",
        "description": (
            "Call the supervisor and first aider to this worker now (critical alert and "
            "WhatsApp message with the site location). Use immediately for danger signs: "
            "confusion, stopped sweating or hot dry skin, fainting, chest pain, trouble "
            "breathing, a fall with a head injury, a seizure; or when the worker asks for help."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "description": "Short English reason, e.g. 'dizzy and confused'."},
            },
            "required": ["reason"],
        },
    },
    {
        "type": "function",
        "name": "start_cool_down",
        "description": "Put the worker on a cool-down break in the shade now.",
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {"type": "integer", "minimum": 5, "maximum": 60,
                            "description": "Break length in minutes, usually 15."},
            },
            "required": ["minutes"],
        },
    },
    {
        "type": "function",
        "name": "get_heat_status",
        "description": (
            "Latest heat status for this worker: WBGT, heat category, work/rest phase, "
            "minutes worked and minutes left before the mandatory rest, rest remaining."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
]
TOOL_NAMES = {t["name"] for t in TOOLS}

UAE_RULES = """UAE site rules you know:
- Midday break: outdoor work in the sun stops 12:30-15:00 every day from 15 June to 15 September.
- Drink water every 15-20 minutes, about one cup, even when not thirsty.
- To cool down: stop work, rest in the shade or the cooled rest area, loosen or remove heavy PPE, wet the head, neck and arms.
- Emergency ambulance: 998. Tell the supervisor about every fall, injury or feeling unwell.
- Never keep working through heat symptoms and never skip a mandatory rest."""

RELEASE_NUDGE = ("The worker has finished speaking and released the button. "
                 "Answer their question now, briefly, in their language.")

DANGER_SIGNS = ("confusion or strange behaviour, slurred speech, stopped sweating or hot dry skin, "
                "fainting or nearly fainting, chest pain, trouble breathing, a fall with a head "
                "injury, a seizure, repeated vomiting")


# ---------------------------------------------------------------- helpers

def pack(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack(">H", len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    head = await reader.readexactly(3)
    n = struct.unpack(">H", head[1:])[0]
    return head[:1], (await reader.readexactly(n) if n else b"")


def _dotenv(path: Path) -> dict:
    out = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def find_api_key() -> str:
    """OPENAI_API_KEY from the environment, else OPENAI_API_KEY or OPENAI from the project .env.

    The value is only ever used in the Authorization header; it is never logged.
    """
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if key:
        return key
    env = _dotenv(PROJECT_ROOT / ".env")
    for name in ("OPENAI_API_KEY", "OPENAI"):
        if env.get(name):
            return env[name].strip()
    return ""


def _utf8_head(s: str, n: int) -> str:
    return s.encode("utf-8")[:n].decode("utf-8", "ignore")


def _utf8_tail(s: str, n: int) -> str:
    b = s.encode("utf-8")
    return s if len(b) <= n else b[-n:].decode("utf-8", "ignore")


_NON_SPEECH = re.compile(r"\[[^\]\n]{1,24}\]")   # transcript tags like "[chuckle]"
_SENTENCE_END = re.compile(r"(?<=[.!?।॥؟۔。！？])\s+")


def screen_text(text: str, limit: int = MAX_TEXT) -> str:
    """The latest sentence or two of `text` that fits in `limit` UTF-8 bytes."""
    text = " ".join(text.split())
    if not text:
        return ""
    parts = [p for p in _SENTENCE_END.split(text) if p]
    last = parts[-1]
    if len(last.encode("utf-8")) > limit:
        tail = _utf8_tail(last, limit - 3)  # room for the 3-byte ellipsis
        cut = tail.find(" ")
        if 0 <= cut < len(tail) // 3:
            tail = tail[cut + 1:]
        return "…" + tail
    if len(parts) >= 2:
        two = parts[-2] + " " + last
        if len(two.encode("utf-8")) <= limit:
            return two
    return last


def call_text(name: str, args: dict) -> str:
    inner = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
    return f"{name}({inner})"


def _level(pcm: bytes) -> int:
    """Mean |sample| over every 4th sample of a PCM16 LE chunk."""
    n = len(pcm) // 2
    if not n:
        return 0
    a = array("h")
    a.frombytes(pcm[: n * 2])
    if sys.byteorder == "big":
        a.byteswap()
    s = a[::4]
    return sum(map(abs, s)) // len(s)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def situation(ctx: dict) -> str:
    """Plain-text summary of the worker's live state for the model's instructions."""
    w = ctx.get("worker") or {}
    s = ctx.get("site") or {}
    known = bool(w) and bool(ctx.get("known", True))
    when = s.get("local_time")
    lines = ["Live situation" + (f" (site time {when})" if when else "") + ":"]

    if known:
        who = [w.get("name"), (w.get("trade") or "").lower() or None, w.get("zone"),
               f"crew {w['crew']}" if w.get("crew") else None]
        acc = ("acclimatized to the heat" if w.get("acclimatized", True)
               else "NOT yet acclimatized to the heat, so needs longer rests")
        lines.append(f"- Worker: {', '.join(x for x in who if x)}; {acc}.")
    else:
        lines.append("- This wristband is not linked to a registered worker: you do not know "
                     "their name or personal work/rest timer, so use the site heat level.")

    heat = []
    wbgt = _num(s.get("wbgt_c"))
    cat = s.get("category") or {}
    if wbgt is not None:
        heat.append(f"WBGT {wbgt:.1f} °C")
    if cat.get("label"):
        heat.append(f"heat category \"{cat['label']}\" (level {cat.get('level', '?')} of 4)")
    temp, rh = _num(s.get("temp_c")), _num(s.get("rh"))
    if temp is not None:
        heat.append(f"air {temp:.0f} °C" + (f", humidity {rh:.0f} %" if rh is not None else ""))
    if heat:
        lines.append("- Heat now: " + ", ".join(heat) + ".")

    if known:
        phase = w.get("phase")
        reason = w.get("phase_reason")
        why = f" ({reason})" if reason else ""
        budget, done = _num(w.get("work_budget_min")), _num(w.get("work_elapsed_min")) or 0.0
        rest_req, rest_left = _num(w.get("rest_required_min")), _num(w.get("rest_remaining_min"))
        wl = w.get("workload")
        if phase == "rest":
            left = f"about {rest_left:.0f} min remaining" if rest_left is not None else "in progress"
            lines.append(f"- Work/rest: MANDATORY REST now, {left}{why}. No work until it ends.")
        elif phase == "stop" or budget == 0:
            lines.append(f"- Work/rest: STOP WORK now{why}: heat too high for this workload.")
        elif budget is not None:
            left = max(0.0, budget - done)
            rest = f" {rest_req:.0f} min" if rest_req else ""
            lines.append(f"- Work/rest: working ({wl or 'light'} workload); worked {done:.0f} of "
                         f"{budget:.0f} min allowed, about {left:.0f} min left before a "
                         f"mandatory{rest} rest in the shade.")
        status = w.get("status")
        if status:
            lines.append(f"- Wristband status: {status}.")

    mb = s.get("midday_break") or {}
    if mb.get("active"):
        lines.append("- The midday break is ACTIVE now: no outdoor work in the sun until 15:00.")
    elif mb.get("in_season"):
        lines.append("- Midday break season: outdoor work in the sun stops 12:30-15:00 today.")
    elif mb:
        lines.append("- The midday break is not in season today (it runs 15 June - 15 September).")

    alerts = ctx.get("open_alerts") or []
    if alerts:
        items = [f"{a.get('title') or a.get('type')} ({a.get('severity')}, {a.get('state')})"
                 for a in alerts[:3]]
        lines.append("- Open alerts for this worker: " + "; ".join(items) + ".")
    else:
        lines.append("- Open alerts for this worker: none.")
    return "\n".join(lines)


def realtime_instructions(ctx: dict) -> str:
    return f"""You are HeatGuard, a calm and caring safety assistant for outdoor construction workers on a site in the UAE. You speak to one worker through a small speaker on their wrist. The worker held a button, asked one question, and released it.

How to answer:
- Answer in the same language the worker spoke (Hindi, Urdu, Bengali, Malayalam, Tamil, Nepali, Tagalog, Arabic, English or any other). Judge the language from their words, not their accent. If they mix languages, mix the same way. Use simple everyday words.
- At most 2-3 short sentences, under 40 words. Most important action first. No lists, no markdown. Round numbers ("about 15 minutes").
- Use the live situation below for questions about heat, breaks and time left. Call get_heat_status only when you need fresher numbers.
- You are not a doctor: never diagnose an illness and never recommend medicine. Say what to do now and when to get help.
- Danger signs ({DANGER_SIGNS}), for the worker or a coworker: call request_help immediately, before you speak, then tell them help is coming, to stop work, get into the shade and not stay alone. Ambulance: 998.
- Other symptoms (headache, dizziness, cramps, nausea, heavy sweating, weakness): call report_symptoms with an honest severity, then tell them to stop, rest in the shade and drink water, and that the supervisor knows.
- If they ask for a break, rest or to cool down, call start_cool_down (15 minutes unless they say otherwise).
- Only say an action is done after the tool result confirms it.
- If you did not understand, ask them to press the button and say it again.

{UAE_RULES}

{situation(ctx)}"""


def live_instructions(ctx: dict) -> str:
    return f"""You are HeatGuard, a calm and caring safety assistant for outdoor construction workers on a site in the UAE. You speak to one worker through a small speaker on their wrist.
Speak calmly and clearly, at an unhurried pace, like a kind site supervisor. Be direct and practical.

How this conversation works: the worker holds a button while they ask one question, then releases it and listens. Stay silent while they speak. When they finish, answer once and then stop.
Answer in the same language the worker spoke (Hindi, Urdu, Bengali, Malayalam, Tamil, Nepali, Tagalog, Arabic, English or any other). Judge the language from their words, not their accent. If they mix languages, mix the same way. Use simple everyday words.
Keep it very short: the whole answer is at most 2 short sentences, under 30 words. Give only the one or two most important actions, not a full list. Round numbers ("about 15 minutes").
No laughter, filler words or other non-speech sounds.
You are not a doctor: never diagnose an illness and never recommend medicine. Say what to do now and when to get help.
If you did not understand, ask them to press the button and say it again.

Backchannel policy: Do not use backchannels. Stay silent while the worker is speaking.

Interruption policy: Stop speaking when the worker interrupts. Listen to what they say.

Delegation policy:
Backend tools:
- Call for help: alerts the supervisor and first aider to come to this worker now.
- Report symptoms: logs the worker's symptoms, alerts the supervisor and starts a cool-down.
- Start cool-down: puts the worker on a rest break in the shade now.
- Heat status: the latest heat level and this worker's work/rest timer.

Delegate to the backend when:
- The worker describes a danger sign for themself or a coworker: {DANGER_SIGNS}. Delegate a call for help immediately. While it runs, tell them you are calling their supervisor now and to stop work and get into the shade. In a life-threatening emergency the ambulance number is 998.
- The worker reports any symptom: headache, dizziness, cramps, nausea, heavy sweating, weakness, feeling very tired or unwell.
- The worker asks for help, the supervisor or first aid, or asks for a break, rest or to cool down.
- The worker asks about the heat or break time and the live situation below might be out of date.

Do not delegate to the backend when:
- The question is general heat-safety advice or is answered by the live situation below.
- You need a brief clarification.

Delegate before giving an answer that depends on backend work. While the backend works, say at most a few words (for example "One moment."), except for danger signs. Do not guess the result while waiting, and only say help was called or a break started after the backend confirms it. When the result arrives, say it with the most important action in at most 2 short sentences.

{UAE_RULES}

{situation(ctx)}"""


BACKEND_INSTRUCTIONS = f"""## Voice conversation context
You are the backend for HeatGuard, a voice safety assistant for outdoor construction workers on a site in the UAE. A voice model talks with one worker through their wristband and hands you tasks. Transcripts can be in any language and can contain mistakes. Use the latest request.

## Task instructions
- request_help(reason): call at once, without asking questions first, for any danger sign ({DANGER_SIGNS}), or when the worker asks for help, first aid or the supervisor.
- report_symptoms(symptoms, severity): for other symptoms such as headache, dizziness, cramps, nausea, heavy sweating or weakness. severity is mild, moderate or severe. For severe symptoms also call request_help.
- start_cool_down(minutes): when the worker asks for a break, rest or to cool down. Use 15 minutes unless they say otherwise.
- get_heat_status(): the latest WBGT, heat category, work/rest phase and minutes left before the mandatory rest.
Call each tool at most once per request. Never diagnose an illness or suggest medicine.

## Return the result
Reply in one short plain English sentence (under 25 words) with the facts the voice model should say: what was done (only if the tool confirmed it) and the single most important next step for the worker. No markdown."""


# ---------------------------------------------------------------- device side

class _Fail(Exception):
    """Ends the question with an X frame carrying str(self)."""


class _DeviceGone(Exception):
    pass


class _Device:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        self.got_end = False
        self.gone = False
        self.closed = False

    async def read(self) -> tuple[bytes, bytes]:
        try:
            return await read_frame(self.reader)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.gone = True
            raise _DeviceGone()

    async def send(self, kind: bytes, payload: bytes = b"") -> None:
        if self.gone or self.closed:
            raise _DeviceGone()
        try:
            self.writer.write(pack(kind, payload))
            await self.writer.drain()
        except (ConnectionError, OSError):
            self.gone = True
            raise _DeviceGone()

    async def reject(self, msg: str) -> None:
        """X frame, then swallow what the device is still streaming so the close is a FIN, not a RST."""
        try:
            await self.send(b"X", _utf8_head(msg, MAX_TEXT).encode("utf-8"))
        except _DeviceGone:
            return
        if self.got_end:
            return
        try:
            if self.writer.can_write_eof():
                self.writer.write_eof()
        except OSError:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 20.0
        while loop.time() < deadline:
            try:
                chunk = await asyncio.wait_for(self.reader.read(65536), 1.5)
            except (TimeoutError, ConnectionError, OSError):
                return
            if not chunk:
                return

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 2.0)
        except Exception:
            pass


# ---------------------------------------------------------------- one question

class _Turn:
    """State shared by both engines for one push-to-talk question."""

    def __init__(self, asst: "Assistant", dev: _Device):
        self.a = asst
        self.core = asst.core
        self.dev = dev
        self.turn_id = uuid.uuid4().hex[:8]
        self.device_id = ""
        self.worker_id = ""
        self.worker_name = ""
        self.audio_in = 0
        self.audio_out = 0
        self.ended = False            # E received from the device
        self.finished = False         # E or X sent to the device
        self.t_start = time.monotonic()
        self.t_end = None
        self.t_first_audio = None
        self.t_done = None
        self.reply: dict[str, str] = {}   # segment -> spoken text, in order
        self.worker_text = ""
        self.worker_final = False
        self.tool_count = 0
        self.first_ts: dict[str, float] = {}
        self.reply_final_sent = False
        self._last_t = 0.0
        self._last_t_text = ""
        self._last_log = 0.0
        self._odd_out = b""
        self._out_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()

    # -- lifecycle

    async def run(self) -> None:
        try:
            async with asyncio.timeout(self.a.timeout):
                kind, payload = await self.dev.read()
                if kind != b"H":
                    raise _Fail("Bad request")
                try:
                    hdr = json.loads(payload.decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    raise _Fail("Bad request")
                if not isinstance(hdr, dict):
                    raise _Fail("Bad request")
                self.device_id = str(hdr.get("id") or "").strip()[:64] or f"unknown-{self.dev.peer}"
                fmt = str(hdr.get("fmt", "pcm16")).lower()
                if hdr.get("rate", RATE) != RATE or fmt not in ("pcm16", "s16le", "pcm"):
                    raise _Fail("Unsupported audio format")
                ctx = self._context()
                log.info("voice %s: question from %s (%s)", self.turn_id, self.device_id, self.worker_name)
                await self.talk(ctx)
        except TimeoutError:
            log.warning("voice %s: timed out after %.0f s", self.turn_id, self.a.timeout)
            if not self.finished:
                self.finished = True
                await self.dev.reject("Sorry, that took too long. Please try again.")
        except _Fail as e:
            if not self.finished:
                self.finished = True
                await self.dev.reject(str(e))
        except _DeviceGone:
            log.info("voice %s: device %s disconnected", self.turn_id, self.device_id or self.dev.peer)
        except Exception:
            log.exception("voice %s: failed", self.turn_id)
            if not self.finished:
                self.finished = True
                await self.dev.reject("Assistant error. Please try again.")
        finally:
            await self.dev.close()
            self._final_logs()
            self._summary()

    async def talk(self, ctx: dict) -> None:
        raise NotImplementedError

    def _context(self) -> dict:
        try:
            ctx = self.core.worker_context(self.device_id) or {}
        except Exception:
            log.exception("worker_context failed for %s", self.device_id)
            ctx = {}
        w = ctx.get("worker") or {}
        self.worker_id = w.get("id") or self.device_id
        self.worker_name = w.get("name") or self.device_id
        return ctx

    def _summary(self) -> None:
        def secs(n):
            return n / (RATE * 2)
        lat = (f"{self.t_first_audio - self.t_end:.2f}s"
               if self.t_first_audio and self.t_end and self.t_first_audio >= self.t_end else "-")
        log.info("voice %s: done, in %.1fs, out %.1fs, first audio after release %s, tools %d, total %.1fs",
                 self.turn_id, secs(self.audio_in), secs(self.audio_out), lat, self.tool_count,
                 time.monotonic() - self.t_start)

    # -- OpenAI side

    async def ws_send(self, ws, event: dict) -> None:
        async with self._ws_lock:
            await ws.send(json.dumps(event, ensure_ascii=False))

    # -- device output

    async def send_audio(self, pcm: bytes) -> None:
        """Forward reply audio to the device as A frames. Caller holds _out_lock."""
        if self.finished or not pcm:
            return
        pcm = self._odd_out + pcm
        cut = len(pcm) & ~1
        self._odd_out, pcm = pcm[cut:], pcm[:cut]
        if not pcm:
            return
        if self.t_first_audio is None:
            self.t_first_audio = time.monotonic()
        for i in range(0, len(pcm), MAX_FRAME):
            await self.dev.send(b"A", pcm[i:i + MAX_FRAME])
        self.audio_out += len(pcm)

    def reply_text(self) -> str:
        text = " ".join(p for p in self.reply.values() if p.strip())
        return " ".join(_NON_SPEECH.sub(" ", text).split())

    async def show(self, force: bool = False) -> None:
        """Throttled T frame with the latest sentence or two, plus a streaming voice-log update."""
        if self.finished:
            return
        now = time.monotonic()
        if force or now - self._last_t >= T_INTERVAL:
            scr = screen_text(self.reply_text())
            if scr and scr != self._last_t_text:
                await self.dev.send(b"T", scr.encode("utf-8"))
                self._last_t_text = scr
                self._last_t = now
        if now - self._last_log >= LOG_INTERVAL and self.reply_text():
            self._last_log = now
            self.publish("assistant", self.reply_text(), False)

    async def finish(self) -> None:
        """Reply complete: last T, then E, then close the device socket."""
        async with self._out_lock:
            await self._before_finish()
            if self.finished:
                return
            try:
                await self.show(force=True)
                await self.dev.send(b"E")
            except _DeviceGone:
                pass
            self.finished = True
            self.t_done = time.monotonic()
        await self.dev.close()
        if self.reply_text():
            self.publish("assistant", self.reply_text(), False)

    async def _before_finish(self) -> None:
        pass

    # -- voice log

    def publish(self, role: str, text: str, final: bool, key: str | None = None) -> None:
        eid = f"V-{self.turn_id}-{key or role}"
        entry = {
            "id": eid, "turn_id": self.turn_id,
            "worker_id": self.worker_id or self.device_id,
            "worker_name": self.worker_name or self.device_id,
            "role": role, "text": text, "final": bool(final),
            "ts": self.first_ts.setdefault(eid, time.time()),
        }
        try:
            res = self.core.publish_voice(entry)
            if inspect.isawaitable(res):
                asyncio.ensure_future(res)
        except Exception:
            log.exception("publish_voice failed")

    def _final_logs(self) -> None:
        if self.ended and not self.worker_final:
            self.worker_final = True
            self.publish("worker", " ".join(self.worker_text.split()) or "(not transcribed)", True)
        text = self.reply_text()
        if text and not self.reply_final_sent:
            self.reply_final_sent = True
            self.publish("assistant", text, True)

    # -- tools

    async def run_tool(self, name, raw_args) -> dict:
        args = raw_args
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        name = str(name or "")
        self.tool_count += 1
        key = "tool" if self.tool_count == 1 else f"tool-{self.tool_count}"
        text = call_text(name, args)
        self.publish("tool", text, True, key=key)
        log.info("voice %s: tool %s", self.turn_id, text)
        if name not in TOOL_NAMES:
            return {"ok": False, "error": f"unknown tool {name}"}
        try:
            result = await asyncio.wait_for(self.core.assistant_tool(self.device_id, name, args), TOOL_TIMEOUT)
        except Exception:
            log.exception("voice %s: tool %s failed", self.turn_id, name)
            self.publish("tool", text + " (failed)", True, key=key)
            return {"ok": False, "error": "the site system did not respond"}
        return result if isinstance(result, dict) else {"result": result}


class _RealtimeTurn(_Turn):
    """OpenAI Realtime GA: manual turns, session tools, response.done ends the answer."""

    def __init__(self, asst, dev):
        super().__init__(asst, dev)
        self._odd_in = b""
        self.rounds = 0

    def session_update(self, ctx: dict) -> dict:
        return {"type": "session.update", "session": {
            "type": "realtime",
            "instructions": realtime_instructions(ctx),
            "output_modalities": ["audio"],
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": RATE},
                    "transcription": {"model": self.a.transcribe_model},
                    "noise_reduction": {"type": "far_field"},
                    "turn_detection": None,
                },
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": self.a.voice},
            },
            "tools": TOOLS,
            "tool_choice": "auto",
        }}

    async def talk(self, ctx: dict) -> None:
        ws = await self.a.open_ws()
        try:
            await self.ws_send(ws, self.session_update(ctx))
            up = asyncio.create_task(self._uplink(ws))
            down = asyncio.create_task(self._downlink(ws))
            try:
                await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
                if down.done():
                    down.result()
                else:
                    up.result()                # _Fail on a too-short question
                    if not self.finished:      # socket closed before the answer
                        raise _DeviceGone()
                    await down                 # reply sent; wait briefly for the worker transcript
            finally:
                for t in (up, down):
                    t.cancel()
                await asyncio.gather(up, down, return_exceptions=True)
        finally:
            await ws.close()

    async def _uplink(self, ws) -> None:
        try:
            while True:
                kind, payload = await self.dev.read()
                if self.ended:
                    continue
                if kind == b"A":
                    data = self._odd_in + payload
                    cut = len(data) & ~1
                    self._odd_in, data = data[cut:], data[:cut]
                    if data:
                        self.audio_in += len(data)
                        await self.ws_send(ws, {"type": "input_audio_buffer.append",
                                                "audio": base64.b64encode(data).decode("ascii")})
                elif kind == b"E":
                    self.ended = self.dev.got_end = True
                    self.t_end = time.monotonic()
                    if self.audio_in < MIN_AUDIO:
                        raise _Fail("I didn't hear anything. Hold the button while you speak.")
                    self.publish("worker", "…", False)   # placeholder until the transcript arrives
                    await self.ws_send(ws, {"type": "input_audio_buffer.commit"})
                    await self.ws_send(ws, {"type": "response.create"})
        except _DeviceGone:
            return

    async def _downlink(self, ws) -> None:
        loop = asyncio.get_running_loop()
        linger_until = None
        while True:
            try:
                if self.finished:
                    if self.worker_final:
                        return
                    if linger_until is None:
                        linger_until = loop.time() + TRANSCRIPT_LINGER
                    left = linger_until - loop.time()
                    if left <= 0:
                        return
                    raw = await asyncio.wait_for(ws.recv(), left)
                else:
                    raw = await ws.recv()
            except TimeoutError:
                return
            except ConnectionClosed:
                if self.finished:
                    return
                raise _Fail("Assistant disconnected. Please try again.")
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            await self._event(ws, ev)

    async def _event(self, ws, ev: dict) -> None:
        t = ev.get("type", "")
        if t in ("session.created", "session.updated"):
            self.a.last_error = None
        elif t == "error":
            err = ev.get("error") or {}
            log.warning("voice %s: realtime error %s: %s", self.turn_id, err.get("code"), err.get("message"))
            if not self.finished:
                self.a.last_error = f"Realtime error: {err.get('code') or err.get('type')}"
                raise _Fail("Assistant error. Please try again.")
        elif t in ("response.output_audio.delta", "response.audio.delta"):
            async with self._out_lock:
                await self.send_audio(base64.b64decode(ev.get("delta") or ""))
        elif t in ("response.output_audio_transcript.delta", "response.audio_transcript.delta",
                   "response.output_text.delta", "response.text.delta"):
            key = ev.get("item_id") or "_"
            self.reply[key] = self.reply.get(key, "") + (ev.get("delta") or "")
            async with self._out_lock:
                await self.show()
        elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            if ev.get("transcript"):
                self.reply[ev.get("item_id") or "_"] = ev["transcript"]
        elif t in ("response.output_text.done", "response.text.done"):
            if ev.get("text"):
                self.reply[ev.get("item_id") or "_"] = ev["text"]
        elif t == "conversation.item.input_audio_transcription.delta":
            if not self.worker_final:
                self.worker_text += ev.get("delta") or ""
        elif t == "conversation.item.input_audio_transcription.completed":
            self.worker_text = (ev.get("transcript") or self.worker_text).strip()
            self.worker_final = True
            self.publish("worker", self.worker_text or "(not transcribed)", True)
        elif t == "conversation.item.input_audio_transcription.failed":
            log.info("voice %s: input transcription failed", self.turn_id)
            self.worker_final = True
            self.publish("worker", self.worker_text or "(not transcribed)", True)
        elif t == "response.done":
            await self._response_done(ws, ev.get("response") or {})

    async def _response_done(self, ws, resp: dict) -> None:
        if self.finished:
            return
        async with self._out_lock:
            await self.show(force=True)
        status = resp.get("status")
        calls = [o for o in resp.get("output") or [] if o.get("type") == "function_call"]
        if status == "failed":
            err = (resp.get("status_details") or {}).get("error") or {}
            log.warning("voice %s: response failed: %s", self.turn_id, err.get("message") or err)
            if not self.audio_out:
                self.a.last_error = f"Realtime response failed: {err.get('code') or err.get('type') or 'unknown'}"
                raise _Fail("Assistant error. Please try again.")
        elif calls and self.rounds < MAX_TOOL_ROUNDS:
            self.rounds += 1
            for c in calls:
                result = await self.run_tool(c.get("name"), c.get("arguments"))
                await self.ws_send(ws, {"type": "conversation.item.create", "item": {
                    "type": "function_call_output", "call_id": c.get("call_id"),
                    "output": json.dumps(result, ensure_ascii=False, default=str)}})
            await self.ws_send(ws, {"type": "response.create"})
            return
        await self.finish()


class _LiveTurn(_Turn):
    """GPT-Live: full-duplex voice model, tools through Responses delegation."""

    def __init__(self, asst, dev):
        super().__init__(asst, dev)
        self.q: asyncio.Queue = asyncio.Queue()
        self.started = asyncio.Event()
        self.closing = False
        self.pending: dict[str, float] = {}        # delegation id -> start time
        self.calls: dict[str, list] = {}           # delegation id -> [(call_id, task)]
        self.jobs: set[asyncio.Task] = set()       # tool runs and result submissions
        self.pre: list | None = []                 # output received before E: (t, kind, data, voiced)
        self.reply_started = False
        self.voiced_at = None                      # last voiced output chunk (monotonic)
        self.backend_done_at = None
        self.backend_text = ""
        self.nudged = False
        self.session_id = None
        self.usage_s = None
        self.close_reason = None

    def session_start(self, ctx: dict) -> dict:
        a = self.a
        backend = {
            "model": a.backend_model,
            "instructions": BACKEND_INSTRUCTIONS,
            "tools": [dict(t, strict=False) for t in TOOLS],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }
        if a.backend_effort:
            backend["reasoning"] = {"effort": a.backend_effort}
        return {"type": "session.start", "event_id": "hg_start", "session": {
            "model": a.model,
            "instructions": live_instructions(ctx),
            "audio": {"format": {"type": "audio/pcm", "rate": RATE}, "output": {"voice": a.voice}},
            "delegation": {"type": "responses", "responses": backend},
        }}

    async def talk(self, ctx: dict) -> None:
        ws = await self.a.open_ws()
        reader = events = mic = watch = None
        wait_close = 1.0
        try:
            await self.ws_send(ws, self.session_start(ctx))
            reader = asyncio.create_task(self._device_reader())
            events = asyncio.create_task(self._events(ws))
            mic = asyncio.create_task(self._mic(ws))
            watch = asyncio.create_task(self._watch(ws))
            await asyncio.wait({reader, events, mic, watch}, return_when=asyncio.FIRST_COMPLETED)
            if watch.done():
                watch.result()              # _Fail when nothing was said
                wait_close = 3.0            # answered: let session.closed and late transcripts arrive
            elif events.done():
                events.result()
                if not self.finished:
                    log.warning("voice %s: session closed early (%s)", self.turn_id, self.close_reason)
                    raise _Fail("Assistant stopped. Please try again.")
            elif mic.done():
                mic.result()                # only ends by raising
            else:
                reader.result()             # _Fail on a too-short question
                if not self.finished:
                    raise _DeviceGone()
        finally:
            tasks = [t for t in (reader, events, mic, watch) if t]
            for t in (reader, mic, watch):
                if t:
                    t.cancel()
            await self._close_session(ws, events, wait_close)
            for t in list(self.jobs) + tasks:
                t.cancel()
            await asyncio.gather(*self.jobs, *tasks, return_exceptions=True)
            await ws.close()

    async def _close_session(self, ws, events, wait: float) -> None:
        """session.close, then read until session.closed (final usage, late transcript)."""
        self.closing = True
        if events is None or events.done() or not self.started.is_set():
            return
        try:
            await self.ws_send(ws, {"type": "session.close", "event_id": "hg_close"})
        except Exception:
            return
        await asyncio.wait({events}, timeout=wait)

    async def _device_reader(self) -> None:
        try:
            while True:
                kind, payload = await self.dev.read()
                if self.ended:
                    continue
                if kind == b"A" and payload:
                    self.audio_in += len(payload)
                    self.q.put_nowait(payload)
                elif kind == b"E":
                    self.ended = self.dev.got_end = True
                    self.t_end = time.monotonic()
                    if self.audio_in < MIN_AUDIO:
                        raise _Fail("I didn't hear anything. Hold the button while you speak.")
                    self.q.put_nowait(None)
                    self.publish("worker", " ".join(self.worker_text.split()) or "…", False)
        except _DeviceGone:
            return

    async def _mic(self, ws) -> None:
        try:
            await asyncio.wait_for(self.started.wait(), START_TIMEOUT)
        except TimeoutError:
            self.a.last_error = "GPT-Live session did not start"
            raise _Fail("Voice assistant unavailable")
        # Device audio, including anything buffered while the session was starting.
        carry = b""
        while True:
            chunk = await self.q.get()
            if chunk is None:
                break
            data = carry + chunk
            cut = len(data) & ~1
            carry = data[cut:]
            if cut:
                await self.ws_send(ws, {"type": "session.input_audio.append",
                                        "audio": base64.b64encode(data[:cut]).decode("ascii")})
        # Button released. GPT-Live expects a continuous input stream, so keep the
        # timeline moving with real-time silence while it answers.
        loop = asyncio.get_running_loop()
        nxt = loop.time()
        while True:     # cancelled by talk() once the answer is over
            await self.ws_send(ws, {"type": "session.input_audio.append", "audio": SILENCE_B64})
            nxt += CHUNK_S
            await asyncio.sleep(max(0.0, nxt - loop.time()))

    async def _watch(self, ws) -> None:
        """Decide when the spoken answer is over (GPT-Live has no end-of-response event)."""
        a = self.a
        while not self.finished:
            await asyncio.sleep(0.1)
            if not self.ended or not self.started.is_set():
                continue
            async with self._out_lock:
                await self._flush_pre()     # the answer may have started before the release
                if self.reply_started:
                    await self.show()
            now = time.monotonic()
            for d, t0 in list(self.pending.items()):
                if now - t0 > BACKEND_MAX_S:
                    log.warning("voice %s: delegation %s still running after %.0f s", self.turn_id, d, BACKEND_MAX_S)
                    self.pending.pop(d, None)
            if self.pending or self.jobs:
                continue
            if self.voiced_at is None:
                if (a.live_nudge_after_s and not self.nudged and not self.backend_done_at
                        and now - self.t_end >= a.live_nudge_after_s):
                    # Still silent: the live model may be waiting for more speech
                    # (e.g. a question that sounds unfinished). Ask it to answer.
                    self.nudged = True
                    log.info("voice %s: no answer %.1f s after release, nudging", self.turn_id, now - self.t_end)
                    await self.ws_send(ws, {"type": "session.instructions.append", "event_id": "hg_nudge",
                                            "delegation_id": None, "content": RELEASE_NUDGE})
                if now - self.t_end > a.live_no_reply_s:
                    if self.backend_text.strip():
                        # Backend acted but the voice stayed silent: show its result.
                        self.reply.setdefault("backend", self.backend_text.strip())
                        await self.finish()
                        return
                    raise _Fail("Sorry, no answer. Please try again.")
                continue
            if (self.backend_done_at and self.backend_done_at > self.voiced_at
                    and now - self.backend_done_at < a.live_after_backend_s):
                continue    # backend finished after the last speech: give the voice time to report it
            if now - self.voiced_at >= a.live_quiet_s:
                log.info("voice %s: answer over %.1f s after release (quiet %.1f s)",
                         self.turn_id, now - self.t_end, now - self.voiced_at)
                await self.finish()
                return

    async def _events(self, ws) -> None:
        while True:
            try:
                raw = await ws.recv()
            except ConnectionClosed:
                if self.closing or self.finished:
                    return
                raise _Fail("Assistant disconnected. Please try again.")
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t != "session.output_audio.delta" and log.isEnabledFor(logging.DEBUG):
                inner = (ev.get("event") or {}).get("type", "")
                rel = f"{time.monotonic() - self.t_end:+.2f}s from release" if self.t_end else "before release"
                log.debug("voice %s: %s %s %s", self.turn_id, rel, t, inner)
            if t == "session.output_audio.delta":
                await self._out_audio(base64.b64decode(ev.get("delta") or ""))
            elif t == "session.output_transcript.delta":
                await self._out_text(ev.get("delta") or "")
            elif t == "session.input_transcript.delta":
                self.worker_text += ev.get("delta") or ""
            elif t == "response.event":
                self._backend_event(ws, ev)
            elif t == "session.delegation.created":
                dlg = ev.get("delegation") or {}
                d = dlg.get("id") or "_"
                self.pending.setdefault(d, time.monotonic())
                log.info("voice %s: delegated %s to %s", self.turn_id, d, dlg.get("target"))
                if dlg.get("target") == "client":
                    # Only Responses delegation is configured; answer so the voice is not left waiting.
                    self.pending.pop(d, None)
                    await self.ws_send(ws, {"type": "session.commentary.append", "delegation_id": d,
                                            "content": "The site system cannot do that right now. "
                                                       "Tell the worker to contact their supervisor."})
            elif t == "session.started":
                self.session_id = (ev.get("session") or {}).get("id")
                self.a.last_error = None
                self.started.set()
                log.info("voice %s: live session %s started", self.turn_id, self.session_id)
            elif t == "session.usage.updated":
                self.usage_s = (ev.get("usage") or {}).get("seconds", self.usage_s)
            elif t == "session.closed":
                self.close_reason = ev.get("reason")
                self.usage_s = (ev.get("usage") or {}).get("seconds", self.usage_s)
                log.info("voice %s: live session closed (%s, %s s)", self.turn_id, self.close_reason, self.usage_s)
                return
            elif t == "error":
                err = ev.get("error") or {}
                log.warning("voice %s: live error %s: %s", self.turn_id, err.get("code"), err.get("message"))
                if not self.started.is_set() or err.get("client_event_id") == "hg_start":
                    self.a.last_error = f"GPT-Live error: {err.get('code') or err.get('type')}"
                    raise _Fail("Voice assistant unavailable")
            elif t == "info":
                log.info("voice %s: live info %s: %s", self.turn_id, ev.get("code"), ev.get("message"))

    # -- output audio and captions

    async def _out_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        now = time.monotonic()
        voiced = _level(pcm) >= VOICE_LEVEL
        if not self.ended:
            self._buffer_pre(now, "a", pcm, voiced)
            return
        async with self._out_lock:
            await self._flush_pre()
            if voiced:
                self.voiced_at = now
                self.reply_started = True
            if self.reply_started:     # leading silence is not forwarded
                await self.send_audio(pcm)

    async def _out_text(self, delta: str) -> None:
        if not delta:
            return
        if not self.ended:
            self._buffer_pre(time.monotonic(), "t", delta, False)
            return
        async with self._out_lock:
            await self._flush_pre()
            self.reply["live"] = self.reply.get("live", "") + delta
            await self.show()

    def _buffer_pre(self, now: float, kind: str, data, voiced: bool) -> None:
        if self.pre is None:
            return
        self.pre.append((now, kind, data, voiced))
        keep = now - self.a.preroll_s - 1.0
        while self.pre and self.pre[0][0] < keep:
            self.pre.pop(0)

    async def _flush_pre(self) -> None:
        """At E: forward output that started just before the button was released.

        The answer can begin while the worker is still holding the button after
        finishing their sentence. Keep the last preroll_s of output, from its
        first voiced chunk on. Caller holds _out_lock.
        """
        if self.pre is None:
            return
        items, self.pre = self.pre, None
        cutoff = (self.t_end or time.monotonic()) - self.a.preroll_s
        items = [x for x in items if x[0] >= cutoff]
        first = next((i for i, x in enumerate(items) if x[1] == "a" and x[3]), None)
        if first is None:
            return
        for i, (t, kind, data, voiced) in enumerate(items):
            if kind == "t":
                self.reply["live"] = self.reply.get("live", "") + data
            elif i >= first:
                if voiced:
                    self.voiced_at = t
                self.reply_started = True
                await self.send_audio(data)

    async def _before_finish(self) -> None:
        await self._flush_pre()

    # -- Responses delegation

    def _backend_event(self, ws, ev: dict) -> None:
        d = ev.get("delegation_id")
        if not d:
            d = next(iter(self.pending)) if len(self.pending) == 1 else "_"
        inner = ev.get("event") or {}
        it = inner.get("type", "")
        if it == "response.created":
            self.pending.setdefault(d, time.monotonic())
            self.backend_text = ""
        elif it == "response.output_item.done":
            item = inner.get("item") or {}
            if item.get("type") == "function_call":
                job = asyncio.create_task(self.run_tool(item.get("name"), item.get("arguments")))
                self._track(job)
                self.calls.setdefault(d, []).append((item.get("call_id"), job))
        elif it == "response.output_text.delta":
            self.backend_text += inner.get("delta") or ""
        elif it == "response.output_text.done":
            self.backend_text = inner.get("text") or self.backend_text
        elif it in ("response.completed", "response.done", "response.incomplete", "response.failed"):
            calls = self.calls.pop(d, [])
            if it == "response.failed":
                err = (inner.get("response") or {}).get("error") or {}
                log.warning("voice %s: backend response failed: %s", self.turn_id, err.get("message") or err)
            if calls and it != "response.failed":
                self._track(asyncio.create_task(self._submit(ws, d, calls)))
            else:
                self.pending.pop(d, None)
                self.backend_done_at = time.monotonic()
                if self.backend_text:
                    log.info("voice %s: backend result: %s", self.turn_id, self.backend_text[:200])
        elif it == "error":
            log.warning("voice %s: backend error: %s", self.turn_id, inner.get("message") or inner)

    def _track(self, job: asyncio.Task) -> None:
        self.jobs.add(job)
        job.add_done_callback(self.jobs.discard)

    async def _submit(self, ws, d: str, calls: list) -> None:
        """Return every function result for the delegated response, then continue it."""
        try:
            for call_id, job in calls:
                result = await job
                await self.ws_send(ws, {"type": "response.item.create", "item": {
                    "type": "function_call_output", "call_id": call_id,
                    "output": json.dumps(result, ensure_ascii=False, default=str)}})
            await self.ws_send(ws, {"type": "response.create"})
        except (ConnectionClosed, OSError) as e:
            log.warning("voice %s: could not return tool results: %s", self.turn_id, type(e).__name__)
            self.pending.pop(d, None)


# ---------------------------------------------------------------- server

class Assistant:
    """TCP :47802 voice server. `await Assistant(core).start()` and leave it running."""

    def __init__(self, core, api_key=None, model=None, voice=None, port=47802, url=None,
                 engine=None, host="0.0.0.0", timeout=45.0, transcribe_model=None,
                 backend_model=None):
        self.core = core
        self._key = api_key if api_key is not None else find_api_key()
        eng = (engine or os.environ.get("HEATGUARD_VOICE_ENGINE") or "live").strip().lower()
        self.engine = "realtime" if eng == "realtime" else "live"
        if self.engine == "live":
            self.model = model or os.environ.get("OPENAI_LIVE_MODEL") or LIVE_MODEL
            self.url = url or os.environ.get("OPENAI_LIVE_URL") or LIVE_URL
            self.voice = voice or os.environ.get("OPENAI_LIVE_VOICE") or DEFAULT_VOICE
        else:
            self.model = model or os.environ.get("OPENAI_REALTIME_MODEL") or REALTIME_MODEL
            self.url = url or os.environ.get("OPENAI_REALTIME_URL") or REALTIME_URL
            self.voice = voice or os.environ.get("OPENAI_REALTIME_VOICE") or DEFAULT_VOICE
        self.backend_model = backend_model or os.environ.get("HEATGUARD_LIVE_BACKEND") or LIVE_BACKEND
        self.backend_effort = os.environ.get("HEATGUARD_LIVE_BACKEND_EFFORT", "low").strip() or None
        self.transcribe_model = transcribe_model or os.environ.get("OPENAI_TRANSCRIBE_MODEL") or TRANSCRIBE_MODEL
        self.host, self.port, self.timeout = host, port, timeout
        # End-of-answer detection for the live engine (seconds).
        self.live_quiet_s = 1.5
        self.live_no_reply_s = 12.0
        self.live_after_backend_s = 8.0
        self.preroll_s = 1.5
        self.live_nudge_after_s = float(os.environ.get("HEATGUARD_LIVE_NUDGE_S", "6") or 0)
        self.server = None
        self.listen_error = None
        self.last_error = None
        self._slots = MAX_SESSIONS
        self._active = 0

    def __repr__(self):
        return f"<Assistant engine={self.engine} model={self.model} enabled={self.enabled}>"

    @property
    def enabled(self) -> bool:
        return bool(self._key) and ws_connect is not None

    @property
    def reason(self) -> str | None:
        if ws_connect is None:
            return "websockets not installed"
        if not self._key:
            return "OPENAI_API_KEY missing"
        return self.listen_error or self.last_error

    def describe(self) -> dict:
        d = {"enabled": self.enabled, "model": self.model, "reason": self.reason, "engine": self.engine}
        if self.engine == "live":
            d["backend"] = self.backend_model
        return d

    @property
    def bound_port(self) -> int | None:
        if self.server and self.server.sockets:
            return self.server.sockets[0].getsockname()[1]
        return None

    def ws_url(self) -> str:
        if self.engine == "realtime" and "model=" not in self.url:
            return self.url + ("&" if "?" in self.url else "?") + "model=" + self.model
        return self.url      # GPT-Live takes the model in session.start, no query parameters

    async def start(self) -> "Assistant":
        try:
            self.server = await asyncio.start_server(self._on_connect, self.host, self.port)
        except OSError as e:
            self.listen_error = f"voice port {self.port} unavailable: {e.strerror or e}"
            log.error("voice assistant: %s", self.listen_error)
            return self
        state = f"{self.engine} {self.model}" if self.enabled else f"offline ({self.reason})"
        log.info("voice assistant on tcp :%s, %s", self.bound_port, state)
        return self

    async def stop(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    async def open_ws(self):
        url = self.ws_url()
        host = urlsplit(url).hostname or ""
        local = host in ("localhost", "127.0.0.1", "::1")
        try:
            return await ws_connect(
                url, additional_headers={"Authorization": f"Bearer {self._key}"},
                max_size=None, max_queue=None, ping_interval=None, open_timeout=10,
                close_timeout=2, compression=None, proxy=None if local else True)
        except InvalidStatus as e:
            code = getattr(getattr(e, "response", None), "status_code", "?")
            self.last_error = f"OpenAI refused the connection (HTTP {code})"
        except (OSError, TimeoutError, WebSocketException) as e:
            self.last_error = f"cannot reach OpenAI ({type(e).__name__})"
        log.warning("voice assistant: %s", self.last_error)
        raise _Fail("Voice assistant unavailable")

    async def _on_connect(self, reader, writer) -> None:
        dev = _Device(reader, writer)
        try:
            if not self.enabled:
                await dev.reject("Voice assistant offline")
                return
            if self._active >= self._slots:
                await dev.reject("Assistant busy, try again")
                return
            self._active += 1
            try:
                turn = (_LiveTurn if self.engine == "live" else _RealtimeTurn)(self, dev)
                await turn.run()
            finally:
                self._active -= 1
        except Exception:
            log.exception("voice connection from %s failed", dev.peer)
        finally:
            await dev.close()
