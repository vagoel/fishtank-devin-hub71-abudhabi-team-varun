#!/usr/bin/env python3
"""Fake HeatGuard wearable for the voice assistant.

Speaks a text prompt with macOS `say`, converts it to 24 kHz mono PCM16 LE with
`afconvert`, streams it as H/A/E frames at real-time pace (like a held button),
prints the T/X frames it gets back and saves the reply audio as a .wav file.

Device WebSocket (docs/heatguard/device-ws.md; one binary message per frame, first byte = type):
  python scripts/heatguard/voice_client.py --url "ws://127.0.0.1:8100/v1/device/ws?device_id=sticks3-test01" \
      --text "How hot is it right now and when is my next break?"
LAN mode TCP :47802 (HEATGUARD_LAN=1; type | 2-byte length | payload):
  python scripts/heatguard/voice_client.py --port 47802 --voice Lekha --text "मुझे चक्कर आ रहा है, क्या करूँ?"
"""
import argparse
import asyncio
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave

RATE = 24000
FRAME = 4800  # bytes = 100 ms of PCM16 at 24 kHz


def pack(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack(">H", len(payload)) + payload


def to_pcm(path: str, workdir: str) -> bytes:
    """Any audio file macOS can read -> raw 24 kHz mono PCM16 LE."""
    out = os.path.join(workdir, "hg_question_24k.wav")
    subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", path, out],
                   check=True, capture_output=True)
    with wave.open(out, "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, RATE):
            raise SystemExit(f"unexpected format after afconvert: {w.getparams()}")
        return w.readframes(w.getnframes())


def synth(text: str, voice: str | None, workdir: str) -> bytes:
    aiff = os.path.join(workdir, "hg_question.aiff")
    cmd = ["say", "-o", aiff] + (["-v", voice] if voice else []) + [text]
    subprocess.run(cmd, check=True)
    return to_pcm(aiff, workdir)


def save_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


async def ask(host: str, port: int, dev_id: str, pcm: bytes, speed: float = 1.0,
              timeout: float = 60.0, quiet: bool = False) -> dict:
    """Send one question; return {'frames': [(kind, payload)], 'audio', 'texts', 'error', 'ended', timings}."""
    out = {"frames": [], "audio": bytearray(), "texts": [], "error": None, "ended": False,
           "t_release": None, "t_first_audio": None, "t_end": None}
    reader, writer = await asyncio.open_connection(host, port)
    t0 = time.monotonic()

    def say(msg):
        if not quiet:
            print(f"[{time.monotonic() - t0:6.2f}s] {msg}", flush=True)

    async def receive():
        while True:
            try:
                head = await reader.readexactly(3)
                n = struct.unpack(">H", head[1:])[0]
                payload = await reader.readexactly(n) if n else b""
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            kind = head[:1]
            out["frames"].append((kind, payload))
            if kind == b"A":
                if out["t_first_audio"] is None:
                    out["t_first_audio"] = time.monotonic()
                    say("first reply audio")
                out["audio"] += payload
            elif kind == b"T":
                text = payload.decode("utf-8", "replace")
                out["texts"].append(text)
                say(f"T {text}")
            elif kind == b"X":
                out["error"] = payload.decode("utf-8", "replace")
                say(f"X {out['error']}")
            elif kind == b"E":
                out["ended"] = True
                out["t_end"] = time.monotonic()
                say("E reply finished")

    rx = asyncio.create_task(receive())
    try:
        writer.write(pack(b"H", json.dumps({"id": dev_id, "rate": RATE, "fmt": "pcm16"}).encode()))
        await writer.drain()
        say(f"H sent, streaming {len(pcm) / (RATE * 2):.1f} s of audio")
        start = time.monotonic()
        for i, off in enumerate(range(0, len(pcm), FRAME)):
            if rx.done():
                break  # server answered early (X) and closed
            writer.write(pack(b"A", pcm[off:off + FRAME]))
            await writer.drain()
            if speed > 0:
                delay = start + (i + 1) * FRAME / (RATE * 2) / speed - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
        if not rx.done():
            writer.write(pack(b"E"))
            await writer.drain()
            out["t_release"] = time.monotonic()
            say("E sent (button released)")
    except ConnectionError:
        pass
    try:
        await asyncio.wait_for(rx, timeout)
    except TimeoutError:
        say("timed out waiting for the reply")
    writer.close()
    return out


async def ask_ws(url: str, pcm: bytes, speed: float = 1.0, timeout: float = 60.0,
                 quiet: bool = False) -> dict:
    """Same as ask(), over the device WebSocket. JSON commands on the socket are ignored."""
    from websockets.asyncio.client import connect

    out = {"frames": [], "audio": bytearray(), "texts": [], "error": None, "ended": False,
           "t_release": None, "t_first_audio": None, "t_end": None}
    t0 = time.monotonic()

    def say(msg):
        if not quiet:
            print(f"[{time.monotonic() - t0:6.2f}s] {msg}", flush=True)

    async with connect(url, max_size=None, compression=None) as ws:
        async def receive():
            async for m in ws:
                if isinstance(m, str):
                    continue                  # {"cmd": ...} for the wearable UI
                kind, payload = m[:1], m[1:]
                out["frames"].append((kind, payload))
                if kind == b"A":
                    if out["t_first_audio"] is None:
                        out["t_first_audio"] = time.monotonic()
                        say("first reply audio")
                    out["audio"] += payload
                elif kind == b"T":
                    out["texts"].append(payload.decode("utf-8", "replace"))
                    say(f"T {out['texts'][-1]}")
                elif kind in (b"X", b"E"):
                    if kind == b"X":
                        out["error"] = payload.decode("utf-8", "replace")
                        say(f"X {out['error']}")
                    else:
                        out["ended"] = True
                        out["t_end"] = time.monotonic()
                        say("E reply finished")
                    return

        rx = asyncio.create_task(receive())
        await ws.send(b"H" + json.dumps({"rate": RATE, "fmt": "pcm16"}).encode())
        say(f"H sent, streaming {len(pcm) / (RATE * 2):.1f} s of audio")
        start = time.monotonic()
        for i, off in enumerate(range(0, len(pcm), FRAME)):
            if rx.done():
                break
            await ws.send(b"A" + pcm[off:off + FRAME])
            if speed > 0:
                delay = start + (i + 1) * FRAME / (RATE * 2) / speed - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
        if not rx.done():
            await ws.send(b"E")
            out["t_release"] = time.monotonic()
            say("E sent (button released)")
        try:
            await asyncio.wait_for(rx, timeout)
        except TimeoutError:
            say("timed out waiting for the reply")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", help="device WebSocket URL, ws://host:port/v1/device/ws?device_id=...")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=47802)
    ap.add_argument("--id", default="stick-test", help="device id sent in the H frame")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", help="question to speak with macOS `say`")
    src.add_argument("--wav", help="use this audio file instead of `say`")
    ap.add_argument("--voice", help="`say` voice, e.g. Lekha (Hindi), Majed (Arabic)")
    ap.add_argument("--out", help="where to save the reply .wav (default: temp dir)")
    ap.add_argument("--speed", type=float, default=1.0, help="send pace, 1 = real time, 0 = no pacing")
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--play", action="store_true", help="play the reply with afplay")
    args = ap.parse_args()

    workdir = tempfile.mkdtemp(prefix="hg_voice_")
    pcm = synth(args.text, args.voice, workdir) if args.text else to_pcm(args.wav, workdir)
    if args.url:
        res = asyncio.run(ask_ws(args.url, pcm, args.speed, args.timeout))
    else:
        res = asyncio.run(ask(args.host, args.port, args.id, pcm, args.speed, args.timeout))

    secs = len(res["audio"]) / (RATE * 2)
    print(f"reply audio: {secs:.1f} s in {sum(1 for k, _ in res['frames'] if k == b'A')} A frames")
    if res["t_release"] and res["t_first_audio"]:
        print(f"first audio {res['t_first_audio'] - res['t_release']:.2f} s after release")
    if res["texts"]:
        print(f"last screen text: {res['texts'][-1]}")
    if res["audio"]:
        out = args.out or os.path.join(tempfile.gettempdir(), f"heatguard_reply_{args.id}.wav")
        save_wav(out, bytes(res["audio"]))
        print(f"saved {out}")
        if args.play:
            subprocess.run(["afplay", out])
    if res["error"]:
        return 1
    return 0 if res["ended"] and res["audio"] else 2


if __name__ == "__main__":
    sys.exit(main())
