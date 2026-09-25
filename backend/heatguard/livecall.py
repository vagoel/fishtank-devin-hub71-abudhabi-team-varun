"""GPT-Live outbound alert calls, served by the telemetry app (APIRouter).

Rings a phone through Twilio and lets OpenAI's gpt-live-1 do the talking. Twilio
streams the call audio (G.711 u-law, 8 kHz) to WS /twilio/media; each frame is relayed
as-is to a GPT-Live WebSocket session opened with audio/pcmu, and the model's audio goes
back the same way. No transcoding in either direction.

    phone <-> Twilio <-> /twilio/media (this app) <-> wss://api.openai.com/v1/live/sessions

Twilio fetches the call's TwiML from /twilio/voice/{id} when the callee answers, reports
progress to /twilio/status/{id} and falls through to /twilio/connect-done/{id}. If the
GPT-Live leg fails, Twilio reads the alert with <Say>, so the message still gets delivered.

HeatGuard's escalation (services/voice_call.py) calls place_call() in-process.

Env (loaded by heatguard.env; real env vars win):
  OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
  HEATGUARD_PUBLIC_URL   public https base URL of this app (Cloud Run service URL) for Twilio
  LIVE_CALL_API_TOKEN    X-Api-Key for POST /v1/calls and GET /v1/calls/{id}; unset = off
  HEATGUARD_CALLS        "0" refuses every call (tests, local runs)
Optional:
  TWILIO_FROM_NUMBER     caller ID in E.164; default is the account's first voice number
  LIVE_CALL_TO           default callee for POST /v1/calls (falls back to CALL_TO / WHATSAPP_TO)
  LIVE_CALL_VOICE        GPT-Live voice, default marin
  LIVE_BACKEND_MODEL     Responses model GPT-Live delegates to, default gpt-5.6-luna
  LIVE_CALL_MAX_SECONDS  hard cap on call length, default 300
"""
import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import OrderedDict, deque
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

import httpx
import websockets
from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
TWILIO_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_FROM = os.environ.get("TWILIO_FROM_NUMBER", "").strip()
PUBLIC_URL = os.environ.get("HEATGUARD_PUBLIC_URL", "").strip().rstrip("/")
API_TOKEN = os.environ.get("LIVE_CALL_API_TOKEN", "").strip()
DEFAULT_TO = (os.environ.get("LIVE_CALL_TO") or os.environ.get("CALL_TO")
              or os.environ.get("WHATSAPP_TO", "")).split(",")[0].strip()
VOICE = os.environ.get("LIVE_CALL_VOICE", "marin")
BACKEND_MODEL = os.environ.get("LIVE_BACKEND_MODEL", "gpt-5.6-luna")
MAX_CALL_S = int(os.environ.get("LIVE_CALL_MAX_SECONDS", "300"))
CALLS_OFF = os.environ.get("HEATGUARD_CALLS", "1") == "0"

LIVE_MODEL = "gpt-live-1"
LIVE_URL = "wss://api.openai.com/v1/live/sessions"
TWILIO_API = "https://api.twilio.com/2010-04-01"
AMBULANCE = "998"  # UAE ambulance
E164 = re.compile(r"^\+[1-9]\d{7,14}$")

log = logging.getLogger("live_call")


# --- what the call is about -------------------------------------------------

@dataclass
class Incident:
    worker: str = "Rakesh"
    condition: str = "has fallen and is showing signs of heat stroke"
    location: str = "Musaffah area, Abu Dhabi"

    def summary(self) -> str:
        return f"Worker {self.worker} {self.condition}. Location: {self.location}."


def live_instructions(inc: Incident) -> str:
    """Conversation prompt for gpt-live-1: tone, facts, and what to do when asked things."""
    return (
        "You are the HeatGuard automated safety alert line, on an outbound phone call to a site "
        "emergency contact about an injured worker.\n"
        "Speak clear, calm, simple English at a steady pace. The line may be noisy and the "
        "listener may not be a native English speaker.\n\n"
        "Facts (the only facts you have):\n"
        f"- Worker: {inc.worker}\n"
        f"- What happened: {inc.condition}\n"
        f"- Location: {inc.location}\n\n"
        "Make sure the listener understands who is hurt, what happened and where, and that they "
        "will send help now.\n"
        "- You are an automated alert. Say so if asked; never claim to be a person.\n"
        "- Repeat the name and location whenever asked, and spell place names if asked.\n"
        "- If asked for anything not in the facts, say you don't have that detail. Never invent details.\n"
        f"- Tell them to call an ambulance on {AMBULANCE}. If they ask what to do meanwhile: move the "
        "worker into shade, loosen clothing, cool them with water, and give no drinks unless fully awake.\n"
        "- Once they confirm they will help, thank them and say goodbye.\n"
        "You already have every fact available, so answer directly instead of delegating."
    )


def backend_instructions(inc: Incident) -> str:
    return (
        "You support a live voice agent delivering a worker safety alert. Answer only from these "
        f"facts; if something is not covered, say it is not known.\n{inc.summary()}\n"
        f"Ambulance number in the UAE: {AMBULANCE}."
    )


def opening(inc: Incident) -> str:
    """Sent once the session is up so the model speaks first (limit: 500 tokens)."""
    return (
        "The call has just been answered. Speak now in English, without waiting for the listener. "
        f"Say this is an automated HeatGuard safety alert. Say that worker {inc.worker} {inc.condition}. "
        f"The location is {inc.location}. Ask them to send help immediately and to call an ambulance "
        f"on {AMBULANCE}. Then ask them to confirm they got this alert, and pause to listen."
    )


def fallback_say(inc: Incident) -> str:
    """Read by Twilio's own TTS if the GPT-Live leg never comes up or drops."""
    return (
        f"This is an automated HeatGuard safety alert. {inc.summary()} "
        f"Please send help immediately and call an ambulance on {' '.join(AMBULANCE)}."
    )


# --- call records -----------------------------------------------------------

@dataclass
class CallRecord:
    id: str
    to: str
    incident: Incident
    status: str = "created"      # Twilio CallStatus: queued, ringing, in-progress, completed, busy, no-answer, failed
    live: str = ""               # GPT-Live leg: connecting, active, closed, dropped, failed
    twilio_sid: str = ""
    live_session_id: str = ""
    bridged: bool = False
    created_at: float = field(default_factory=time.time)
    transcript: list = field(default_factory=list)
    usage: dict | None = None
    error: str = ""

    def heard(self, speaker: str, text: str):
        if self.transcript and self.transcript[-1]["speaker"] == speaker:
            self.transcript[-1]["text"] += text
        else:
            self.transcript.append({"speaker": speaker, "text": text})

    def public(self) -> dict:
        d = asdict(self)
        d["to"] = mask(self.to)
        return d


CALLS: "OrderedDict[str, CallRecord]" = OrderedDict()
MAX_RECORDS = 200


class CallError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def mask(number: str) -> str:
    return number[:3] + "*" * max(len(number) - 6, 0) + number[-3:] if len(number) > 6 else "***"


def normalize(number: str) -> str:
    n = re.sub(r"[\s\-().]", "", number or "")
    if not E164.match(n):
        raise CallError(422, "phone number must be E.164, e.g. +9715XXXXXXXX")
    return n


def missing_config() -> list[str]:
    need = {
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "TWILIO_ACCOUNT_SID": TWILIO_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_TOKEN,
        "HEATGUARD_PUBLIC_URL": PUBLIC_URL,
    }
    return [k for k, v in need.items() if not v]


def remember(rec: CallRecord):
    CALLS[rec.id] = rec
    while len(CALLS) > MAX_RECORDS:
        CALLS.popitem(last=False)


# --- Twilio -----------------------------------------------------------------

def attr(value: str) -> str:
    return escape(value, {'"': "&quot;"})


def say_alert(rec: CallRecord) -> str:
    return f'<Say loop="2">{escape(fallback_say(rec.incident))}</Say>'


def twiml(rec: CallRecord) -> str:
    # Twilio requests the <Connect> action URL when the stream ends or fails to start;
    # the trailing <Say> only runs if Twilio skips <Connect> altogether.
    stream_url = re.sub(r"^http", "ws", PUBLIC_URL) + "/twilio/media"
    return (
        f'<Response><Connect action="{attr(f"{PUBLIC_URL}/twilio/connect-done/{rec.id}")}">'
        f'<Stream url="{attr(stream_url)}">'
        f'<Parameter name="call_id" value="{rec.id}"/>'
        "</Stream></Connect>"
        f"{say_alert(rec)}"
        "</Response>"
    )


async def caller_id(http: httpx.AsyncClient) -> str:
    """TWILIO_FROM_NUMBER if set, else the account's first voice-capable number."""
    global TWILIO_FROM
    if not TWILIO_FROM:
        r = await http.get(f"{TWILIO_API}/Accounts/{TWILIO_SID}/IncomingPhoneNumbers.json")
        if r.status_code == 200:
            TWILIO_FROM = next((p["phone_number"] for p in r.json().get("incoming_phone_numbers", [])
                                if (p.get("capabilities") or {}).get("voice")), "")
    return TWILIO_FROM


async def place_call(to: str, incident: Incident, from_number: str | None = None) -> CallRecord:
    """Ring `to` now. `from_number` overrides the caller ID (voice_call.py finds one that also
    works on trial accounts)."""
    if CALLS_OFF:
        raise CallError(503, "phone calls are off (HEATGUARD_CALLS=0)")
    missing = missing_config()
    if missing:
        raise CallError(503, "missing config: " + ", ".join(missing))
    to = normalize(to)
    async with httpx.AsyncClient(timeout=15, auth=(TWILIO_SID, TWILIO_TOKEN)) as http:
        from_number = from_number or await caller_id(http)
        if not from_number:
            raise CallError(503, "no voice number on the Twilio account; get one in the console "
                                 "or set TWILIO_FROM_NUMBER")
        rec = CallRecord(id=secrets.token_hex(12), to=to, incident=incident)
        remember(rec)
        calls_url = f"{TWILIO_API}/Accounts/{TWILIO_SID}/Calls.json"
        data = {"To": rec.to, "From": from_number, "Url": f"{PUBLIC_URL}/twilio/voice/{rec.id}"}
        extras = {
            "Timeout": "45",
            "StatusCallback": f"{PUBLIC_URL}/twilio/status/{rec.id}",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
        }
        r = await http.post(calls_url, data=data | extras)
        if r.status_code >= 300 and "trial accounts" in r.text:
            # Trial accounts refuse most optional call parameters; ring with the bare minimum.
            log.info("call %s: trial account, retrying with To/From/Url only", rec.id)
            r = await http.post(calls_url, data=data)
    if r.status_code >= 300:
        try:
            body = r.json()
            rec.error = f"{body.get('code')}: {body.get('message')}"
        except ValueError:
            rec.error = f"HTTP {r.status_code}"
        rec.status = "failed"
        log.error("call %s to %s rejected by Twilio: %s", rec.id, mask(rec.to), rec.error)
        raise CallError(502, "twilio: " + rec.error)
    body = r.json()
    rec.twilio_sid = body.get("sid", "")
    rec.status = body.get("status", "queued")
    log.info("call %s to %s %s (twilio %s)", rec.id, mask(rec.to), rec.status, rec.twilio_sid)
    return rec


async def hangup(rec: CallRecord):
    """End the phone call through Twilio's REST API."""
    if not rec.twilio_sid:
        return
    async with httpx.AsyncClient(timeout=10, auth=(TWILIO_SID, TWILIO_TOKEN)) as http:
        r = await http.post(f"{TWILIO_API}/Accounts/{TWILIO_SID}/Calls/{rec.twilio_sid}.json",
                            data={"Status": "completed"})
    if r.status_code >= 300:
        log.warning("call %s: hang-up refused by Twilio (HTTP %s)", rec.id, r.status_code)


# --- media bridge -----------------------------------------------------------

async def twilio_start(ws: WebSocket) -> tuple[CallRecord | None, str]:
    """Read Twilio's stream preamble and return the call it belongs to.

    Only streams carrying the one-time call_id of a call this service placed are
    bridged, so nobody else can open GPT-Live sessions on this key through /media.
    """
    while True:
        msg = json.loads(await ws.receive_text())
        event = msg.get("event")
        if event == "start":
            start = msg["start"]
            rec = CALLS.get((start.get("customParameters") or {}).get("call_id", ""))
            if not rec or rec.bridged:
                log.warning("rejecting media stream with unknown or reused call id")
                return None, ""
            rec.bridged = True
            return rec, start["streamSid"]
        if event == "stop":
            return None, ""


class LiveBridge:
    """One phone call: Twilio media stream <-> GPT-Live session."""

    def __init__(self, twilio: WebSocket, rec: CallRecord, stream_sid: str):
        self.twilio = twilio
        self.rec = rec
        self.stream_sid = stream_sid
        self.started = False
        self.hung_up = False
        # Caller audio that arrives before session.started (~3 s of 20 ms frames).
        self.early: deque[str] = deque(maxlen=150)

    def session_start(self) -> dict:
        inc = self.rec.incident
        return {
            "type": "session.start",
            "event_id": "start",
            "session": {
                "model": LIVE_MODEL,
                "instructions": live_instructions(inc),
                "audio": {
                    "format": {"type": "audio/pcmu", "rate": 8000},
                    "output": {"voice": VOICE},
                },
                "delegation": {
                    "type": "responses",
                    "responses": {"model": BACKEND_MODEL, "instructions": backend_instructions(inc)},
                },
            },
        }

    async def run(self):
        rec = self.rec
        rec.live = "connecting"
        try:
            async with websockets.connect(
                LIVE_URL,
                additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                max_size=None,
            ) as live:
                await live.send(json.dumps(self.session_start()))
                caller = asyncio.create_task(self.pump_caller(live))
                model = asyncio.create_task(self.pump_live(live))
                done, _ = await asyncio.wait({caller, model}, timeout=MAX_CALL_S,
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    log.info("call %s: %d s limit reached, hanging up", rec.id, MAX_CALL_S)
                    await hangup(rec)
                if not model.done():
                    # Caller hung up first: close the session so final usage is reported.
                    self.hung_up = True
                    with suppress(websockets.ConnectionClosed):
                        await live.send(json.dumps({"type": "session.close"}))
                    await asyncio.wait({model}, timeout=15)
                    if not model.done():
                        log.warning("call %s: no session.closed within 15 s, usage unconfirmed", rec.id)
                caller.cancel()
                model.cancel()
                for result in await asyncio.gather(caller, model, return_exceptions=True):
                    if isinstance(result, Exception) and not isinstance(result, WebSocketDisconnect):
                        log.error("call %s bridge error: %r", rec.id, result)
        except (OSError, websockets.WebSocketException) as e:
            rec.live, rec.error = "failed", f"GPT-Live connection: {e}"
            log.error("call %s: %s", rec.id, rec.error)
        finally:
            if rec.live in ("connecting", "active"):
                rec.live = "dropped"
            # Ends the stream; if the call is still up Twilio moves on to the fallback <Say>.
            with suppress(Exception):
                await self.twilio.close()
            self.report()

    async def pump_caller(self, live):
        """Twilio -> GPT-Live, until the caller hangs up or the stream stops."""
        while True:
            try:
                msg = json.loads(await self.twilio.receive_text())
            except WebSocketDisconnect:
                return
            event = msg.get("event")
            if event == "media":
                payload = msg["media"]["payload"]
                if self.started:
                    await live.send(json.dumps({"type": "session.input_audio.append", "audio": payload}))
                else:
                    self.early.append(payload)
            elif event == "stop":
                return

    async def pump_live(self, live):
        """GPT-Live -> Twilio, until session.closed or OpenAI drops the socket."""
        rec = self.rec
        async for raw in live:
            ev = json.loads(raw)
            kind = ev.get("type")
            if kind == "session.output_audio.delta":
                if self.hung_up:
                    continue
                try:
                    await self.twilio.send_text(json.dumps({
                        "event": "media",
                        "streamSid": self.stream_sid,
                        "media": {"payload": ev["delta"]},
                    }))
                except (WebSocketDisconnect, RuntimeError):
                    # Caller is gone; keep reading so session.closed can still arrive.
                    self.hung_up = True
            elif kind == "session.input_transcript.delta":
                rec.heard("caller", ev.get("delta", ""))
            elif kind == "session.output_transcript.delta":
                rec.heard("agent", ev.get("delta", ""))
            elif kind == "session.started":
                rec.live_session_id = ev["session"]["id"]
                rec.live = "active"
                while self.early:
                    await live.send(json.dumps({"type": "session.input_audio.append",
                                                "audio": self.early.popleft()}))
                self.started = True
                await live.send(json.dumps({
                    "type": "session.instructions.append",
                    "event_id": "opening",
                    "delegation_id": None,
                    "content": opening(rec.incident),
                }))
                log.info("call %s: live session %s started", rec.id, rec.live_session_id)
            elif kind == "session.usage.updated":
                rec.usage = ev.get("usage")
            elif kind == "session.closed":
                rec.usage = ev.get("usage") or rec.usage
                rec.live = "closed"
                return
            elif kind == "error":
                err = ev.get("error") or {}
                rec.error = err.get("message") or json.dumps(err)
                log.error("call %s live error (%s): %s", rec.id, err.get("code"), rec.error)

    def report(self):
        rec = self.rec
        seconds = (rec.usage or {}).get("seconds")
        log.info("call %s done: live=%s, voice seconds=%s", rec.id, rec.live, seconds)
        for turn in rec.transcript:
            log.info("  %s: %s", turn["speaker"], turn["text"].strip())


# --- HTTP -------------------------------------------------------------------

router = APIRouter()


class CallRequest(BaseModel):
    to: str | None = Field(None, max_length=20)
    worker: str | None = Field(None, max_length=80)
    condition: str | None = Field(None, max_length=200)
    location: str | None = Field(None, max_length=200)


def require_token(x_api_key: str | None):
    if not API_TOKEN:
        raise HTTPException(403, "set LIVE_CALL_API_TOKEN to enable this endpoint")
    if not x_api_key or not hmac.compare_digest(x_api_key, API_TOKEN):
        raise HTTPException(401, "bad X-Api-Key")


def status() -> dict:
    """Readiness for the dashboard's call pill (no secrets)."""
    missing = missing_config()
    return {
        "ok": not missing and not CALLS_OFF,
        "missing": missing,
        "calls_off": CALLS_OFF,
        "model": LIVE_MODEL,
        "voice": VOICE,
        "backend_model": BACKEND_MODEL,
        "default_to": mask(DEFAULT_TO),
        "calls_api": bool(API_TOKEN),
    }


@router.post("/v1/calls", tags=["calls"])
async def create_call(req: CallRequest | None = None, x_api_key: str | None = Header(None)):
    require_token(x_api_key)
    req = req or CallRequest()
    incident = Incident(**{k: v for k, v in req.model_dump(exclude={"to"}).items() if v})
    try:
        rec = await place_call(req.to or DEFAULT_TO, incident)
    except CallError as e:
        raise HTTPException(e.status, str(e))
    return rec.public()


@router.get("/v1/calls/{call_id}", tags=["calls"])
def get_call(call_id: str, x_api_key: str | None = Header(None)):
    require_token(x_api_key)
    rec = CALLS.get(call_id)
    if not rec:
        raise HTTPException(404, "unknown call")
    return rec.public()


@router.post("/twilio/voice/{call_id}", include_in_schema=False)
async def twilio_voice(call_id: str):
    """TwiML Twilio fetches once the callee answers."""
    rec = CALLS.get(call_id)
    return Response(twiml(rec) if rec else "<Response><Hangup/></Response>", media_type="application/xml")


@router.post("/twilio/connect-done/{call_id}", include_in_schema=False)
async def twilio_connect_done(call_id: str, request: Request):
    """<Connect> finished. Hang up after a GPT-Live conversation, else read the alert."""
    rec = CALLS.get(call_id)
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    details = {k: v for k, v in form.items() if k.startswith(("Error", "Stream", "CallStatus"))}
    log.info("call %s: <Connect> ended, live=%s, twilio says %s", call_id, rec.live if rec else "?", details)
    if rec and rec.live in ("active", "closed"):
        return Response("<Response><Hangup/></Response>", media_type="application/xml")
    if rec:
        log.warning("call %s: GPT-Live never joined, reading the alert with Twilio TTS", call_id)
        return Response(f"<Response>{say_alert(rec)}</Response>", media_type="application/xml")
    return Response("<Response><Hangup/></Response>", media_type="application/xml")


@router.post("/twilio/status/{call_id}", include_in_schema=False)
async def twilio_status(call_id: str, request: Request):
    rec = CALLS.get(call_id)
    if rec:
        form = parse_qs((await request.body()).decode())
        status = (form.get("CallStatus") or [""])[0]
        if status:
            rec.status = status
            log.info("call %s twilio status: %s", call_id, status)
    return Response(status_code=204)


@router.websocket("/twilio/media")
async def media(ws: WebSocket):
    await ws.accept()
    try:
        rec, stream_sid = await asyncio.wait_for(twilio_start(ws), 10)
    except (TimeoutError, WebSocketDisconnect, ValueError, KeyError):
        rec, stream_sid = None, ""
    if not rec:
        with suppress(Exception):
            await ws.close()
        return
    await LiveBridge(ws, rec, stream_sid).run()
