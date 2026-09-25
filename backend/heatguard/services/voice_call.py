"""Phone-call escalation through Twilio Programmable Voice.

A critical incident rings the HSE officer's phone and a voice reads out who,
where, what the wearable detected, whether the worker answered, and what to
do. Calls reach UAE mobiles where SMS from international numbers does not.

Env:
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
  CALL_TO            comma-separated E.164 numbers (defaults to WHATSAPP_TO)
  TWILIO_CALL_FROM   the Twilio number to call from (looked up from the account when unset)
  CALL_VOICE         Twilio <Say> voice, default Polly.Joanna-Neural
  CALL_ARABIC        "1" to repeat the key facts in Arabic (Polly.Zeina)

Two ways to talk:
  gpt-live  (preferred) hand the call to live_call/call_service.py, which bridges the
            phone audio to OpenAI gpt-live-1 so the responder can ask questions
            ("which floor?", "is he breathing?") and get answers from the incident facts.
            Needs LIVE_CALL_URL (e.g. http://127.0.0.1:8011) and LIVE_CALL_API_TOKEN.
  say       Twilio reads the alert twice with <Say>. Used when gpt-live is not
            configured or refuses the call.

The From number is TWILIO_CALL_FROM / TWILIO_FROM_NUMBER, else the account's first
voice number, else the number this account last called the recipient from (trial
accounts get a Twilio-owned trial number that is not listed as purchased).
"""
import asyncio
import itertools
import os
import re
import time
from datetime import datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import httpx

TZ = ZoneInfo("Asia/Dubai")
API = "https://api.twilio.com/2010-04-01/Accounts/{sid}"
FINAL = ("completed", "busy", "no-answer", "failed", "canceled")
RATE_LIMIT_S = 120


def _e164(raw):
    s = re.sub(r"[\s\-().]", "", str(raw or ""))
    if s.startswith("00"):
        s = "+" + s[2:]
    if s and not s.startswith("+"):
        s = "+" + s
    return s if re.fullmatch(r"\+[1-9]\d{7,14}", s) else None


def mask(num):
    return (num[:5] + "•" * max(0, len(num) - 8) + num[-3:]) if num else "(not set)"


def _spoken(s):
    """Make text friendlier to TTS: spell out units and symbols."""
    s = str(s or "")
    s = s.replace("°/s", " degrees per second").replace("°C", " degrees").replace("°", " degrees")
    s = re.sub(r"(\d)\s*g\b", r"\1 G", s)
    s = s.replace("WBGT", "W B G T").replace("·", ",").replace("–", " to ").replace("“", "").replace("”", "")
    return s


class VoiceCall:
    def __init__(self, env=None, transport=None):
        e = os.environ if env is None else env
        get = lambda k, d="": str(e.get(k) or d).strip()
        self.sid, self.token = get("TWILIO_ACCOUNT_SID"), get("TWILIO_AUTH_TOKEN")
        self.voice = get("CALL_VOICE", "Polly.Joanna-Neural")
        self.arabic = get("CALL_ARABIC") == "1"
        self.frm = _e164(get("TWILIO_CALL_FROM") or get("TWILIO_FROM_NUMBER"))
        self.live_url = get("LIVE_CALL_URL").rstrip("/")
        self.live_token = get("LIVE_CALL_API_TOKEN")
        self.mode = "say"
        self.to = [n for n in (_e164(x) for x in (get("CALL_TO") or get("WHATSAPP_TO")).split(",")) if n]
        self._transport = transport
        self._ids = itertools.count(1)
        self._sent = {}
        self._worker_call = {}   # worker id -> last call time, so one emergency doesn't ring 5 times
        self.reason = None
        self.account_type = None

    async def setup(self):
        """Find the Twilio number to call from and check trial restrictions."""
        if not (self.sid and self.token):
            self.reason = "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing"
            return
        try:
            async with self._client() as c:
                acct = (await c.get(API.format(sid=self.sid) + ".json")).json()
                self.account_type = acct.get("type")
                if not self.frm:
                    nums = (await c.get(API.format(sid=self.sid) + "/IncomingPhoneNumbers.json")).json()
                    voice = [n["phone_number"] for n in nums.get("incoming_phone_numbers", [])
                             if (n.get("capabilities") or {}).get("voice")]
                    self.frm = voice[0] if voice else None
                for to in self.to if not self.frm else []:
                    calls = (await c.get(API.format(sid=self.sid) + "/Calls.json",
                                         params={"To": to, "PageSize": 5})).json().get("calls", [])
                    self.frm = next((_e164(x["from"]) for x in calls if _e164(x.get("from"))), None)
                    if self.frm:
                        break
        except Exception as ex:
            self.reason = "Twilio lookup failed: %s" % type(ex).__name__
            return
        if self.live_url and self.live_token:
            try:
                async with httpx.AsyncClient(timeout=5, transport=self._transport) as c:
                    h = (await c.get(self.live_url + "/health")).json()
                self.mode = "gpt-live" if h.get("ok") and h.get("calls_api") else "say"
            except Exception:
                self.mode = "say"
        missing = []
        if not self.frm and self.mode == "say":
            missing.append("a Twilio number to call from (TWILIO_CALL_FROM)")
        if not self.to:
            missing.append("CALL_TO / WHATSAPP_TO")
        self.reason = ("needs " + "; ".join(missing)) if missing else None

    @property
    def enabled(self):
        return bool(self.sid and self.token and self.to and not self.reason and (self.frm or self.mode == "gpt-live"))

    def describe(self):
        return {"provider": "twilio_voice", "mode": self.mode, "enabled": self.enabled, "from": mask(self.frm),
                "recipients": [mask(n) for n in self.to], "account": self.account_type, "reason": self.reason}

    # ------------------------------------------------------------ gpt-live facts (call_service.py Incident)
    def live_facts(self, alert, worker, site):
        """worker / condition / location strings for the GPT-Live agent, 200 chars max each."""
        a, w, s = alert or {}, worker or {}, site or {}
        d = a.get("detail") or {}
        tl = [x.get("type", "") for x in a.get("timeline") or []]
        noresp = any(t.endswith("_noresp") for t in tl)
        t = a.get("type")
        if t == "fall":
            cond = "has fallen on site (impact %.0f g)" % float(d.get("impact_g") or 0) if d.get("impact_g") else "has fallen on site"
            cond += " and did not answer the wearable's are-you-OK prompt" if noresp else ""
        elif t == "sos":
            said = re.sub(r"^Worker asked for help:\s*", "", a.get("message", ""))
            cond = "asked for help by voice: %s" % said[:120] if a.get("source") == "voice" else \
                "pressed the SOS button on the safety wearable"
        elif t in ("tremor", "erratic"):
            cond = "is shaking uncontrollably, possible seizure" + (", and is not responding" if noresp else "")
        elif t == "inactivity":
            cond = "has not moved for several minutes and is not responding, possible collapse"
        elif t in ("unwell", "heat_critical"):
            cond = "is unwell with signs of heat illness: %s" % a.get("message", "")[:120]
        else:
            cond = (a.get("title") or "has a safety emergency").lower()
        if s.get("wbgt_c") is not None:
            cond += ". Heat is %s, WBGT %s C, so heat stroke is possible" % (
                ((s.get("category") or {}).get("label") or "high").lower(), s.get("wbgt_c"))
        who = ", ".join(x for x in (w.get("name") or a.get("worker_name"), w.get("trade"),
                                    ("crew " + w["crew"]) if w.get("crew") else "") if x)
        loc = w.get("location") or {"lat": s.get("lat"), "lon": s.get("lon")}
        where = ", ".join(x for x in (w.get("zone") or a.get("zone"), (s.get("name") or "").replace(" (demo)", "")) if x)
        if loc.get("lat") is not None:
            where += ". GPS %.4f north, %.4f east" % (float(loc["lat"]), float(loc["lon"]))
        clean = lambda x: re.sub(r"\s+", " ", re.sub(r"\s*·\s*", ", ", x)).strip()[:200]
        return {"worker": clean(who) or "unknown", "condition": clean(cond), "location": clean(where)}

    def _client(self):
        return httpx.AsyncClient(timeout=15, auth=(self.sid, self.token), transport=self._transport)

    # ------------------------------------------------------------ message
    def script(self, alert, worker, site):
        a, w, s = alert or {}, worker or {}, site or {}
        tl = [x.get("type", "") for x in a.get("timeline") or []]
        answered = any(t.endswith("_ok") for t in tl)
        noresp = any(t.endswith("_noresp") for t in tl)
        who = ", ".join(x for x in (w.get("name") or a.get("worker_name"), w.get("trade"),
                                    ("crew " + w["crew"]) if w.get("crew") else "") if x)
        where = ", ".join(x for x in (w.get("zone") or a.get("zone"), s.get("name", "").replace("(demo)", "")) if x)
        loc = w.get("location") or {"lat": s.get("lat"), "lon": s.get("lon")}
        gps = ""
        if loc.get("lat") is not None and loc.get("lon") is not None:
            la, lo = float(loc["lat"]), float(loc["lon"])
            gps = "G P S: %.4f %s, %.4f %s." % (abs(la), "north" if la >= 0 else "south",
                                                abs(lo), "east" if lo >= 0 else "west")
        when = datetime.fromtimestamp(float(a.get("ts") or time.time()), TZ).strftime("%H:%M")
        cat = (s.get("category") or {}).get("label", "")
        parts = [
            "Heat Guard safety alert.",
            "%s. %s." % ((a.get("severity") or "").capitalize(), (a.get("title") or "Incident").replace("·", ",")),
            "Worker: %s." % who if who else "",
            "Location: %s." % where if where else "",
            gps,
            "Time: %s, Dubai." % when,
            _spoken(a.get("message", "")),
            "The worker did not answer the are-you-OK prompt on the wearable." if noresp else
            ("The worker answered that they are OK." if answered else ""),
            "Heat: W B G T %s degrees, %s." % (s.get("wbgt_c"), cat) if s.get("wbgt_c") is not None else "",
            "Send the nearest first aider now. If the worker is unresponsive, call 9 9 8 for an ambulance.",
            "The map pin and details are on the Heat Guard dashboard and in WhatsApp.",
        ]
        text = _spoken(" ".join(p for p in parts if p))
        text = re.sub(r"(\d) ?ms\b", r"\1 milliseconds", text)
        text = re.sub(r"\s+([,.])", r"\1", text).replace("..", ".").replace(",,", ",")
        ar = ("تنبيه سلامة من هيت جارد. %s في %s. أرسلوا المسعف فوراً. اتصلوا بالإسعاف 998 إذا لم يستجب."
              % ("سقوط عامل" if a.get("type") == "fall" else "حالة طارئة لعامل", w.get("zone") or "الموقع"))
        return text, ar

    def twiml(self, text, ar):
        say = '<Say voice="%s">%s</Say>' % (self.voice, escape(text))
        out = ['<Response>', say, '<Pause length="1"/>', '<Say voice="%s">Repeating.</Say>' % self.voice, say]
        if self.arabic:
            out.append('<Say voice="Polly.Zeina" language="arb">%s</Say>' % escape(ar))
        out.append('</Response>')
        return "".join(out)

    # ------------------------------------------------------------ calls
    def _note(self, a, to, status, text, error=None, sid=None):
        return {"id": "C-%06d" % next(self._ids), "channel": "call", "provider": "twilio_voice",
                "to": mask(to), "status": status, "alert_id": a.get("id"), "text": text,
                "location": None, "ts": time.time(), "error": error, "provider_id": sid}

    async def call_incident(self, alert, worker, site, force=False):
        """Ring every recipient. Returns notification dicts; never raises."""
        a = alert or {}
        text, ar = self.script(a, worker, site)
        now = time.time()
        wid = a.get("worker_id")
        if not force and wid and now - self._worker_call.get(wid, 0) < RATE_LIMIT_S * 1.5:
            return []           # this worker's emergency is already on the phone
        todo = []
        for to in (self.to or [None]):
            key = (a.get("id"), to)
            if not force and now - self._sent.get(key, 0) < RATE_LIMIT_S:
                continue
            self._sent[key] = now
            todo.append(to)
        if not self.enabled:
            return [self._note(a, to, "dry_run", text, error=self.reason) for to in todo]
        notes = []
        if wid and todo and self.enabled:
            self._worker_call[wid] = now
        if self.mode == "gpt-live":
            facts = self.live_facts(a, worker, site)
            brief = "GPT-Live call. Worker %s %s. Location: %s." % (facts["worker"], facts["condition"], facts["location"])
            async with httpx.AsyncClient(timeout=20, transport=self._transport) as c:
                for to in list(todo):
                    try:
                        r = await c.post(self.live_url + "/calls", json=dict(facts, to=to),
                                         headers={"X-Api-Key": self.live_token})
                        j = r.json()
                        if r.status_code < 300:
                            n = self._note(a, to, j.get("status") or "queued", brief, sid=j.get("id"))
                            n["provider"], n["live"] = "gpt-live", j.get("live") or ""
                            notes.append(n)
                            todo.remove(to)
                    except Exception:
                        pass  # falls through to the plain Twilio call below
            if not todo or not self.frm:
                return notes
        async with self._client() as c:
            for to in todo:
                try:
                    r = await c.post(API.format(sid=self.sid) + "/Calls.json",
                                     data={"To": to, "From": self.frm, "Twiml": self.twiml(text, ar)})
                    j = r.json()
                    if r.status_code >= 400:
                        notes.append(self._note(a, to, "failed", text,
                                                error="Twilio HTTP %d: %s %s" % (r.status_code, j.get("code", ""),
                                                                                 (j.get("message") or "")[:160])))
                        self._sent.pop((a.get("id"), to), None)
                    else:
                        notes.append(self._note(a, to, j.get("status") or "queued", text, sid=j.get("sid")))
                except Exception as ex:
                    notes.append(self._note(a, to, "failed", text, error=type(ex).__name__))
        return notes

    async def follow(self, note, on_update, every=4.0, limit=150):
        """Poll a call until it finishes, calling on_update(note) on each status change."""
        if not note.get("provider_id"):
            return
        if note.get("provider") == "gpt-live":
            return await self._follow_live(note, on_update, every=3.0, limit=360)
        url = API.format(sid=self.sid) + "/Calls/%s.json" % note["provider_id"]
        t0 = time.time()
        async with self._client() as c:
            while time.time() - t0 < limit:
                await asyncio.sleep(every)
                try:
                    j = (await c.get(url)).json()
                except Exception:
                    continue
                st = j.get("status")
                if st and st != note["status"]:
                    note["status"] = st
                    if j.get("duration"):
                        note["duration_s"] = int(j["duration"])
                    on_update(note)
                if st in FINAL:
                    return

    async def _follow_live(self, note, on_update, every, limit):
        """Poll call_service.py for Twilio status, the GPT-Live leg and the running transcript."""
        url = self.live_url + "/calls/" + note["provider_id"]
        brief = note["text"].split("\n\nTranscript")[0]
        t0, last = time.time(), None
        async with httpx.AsyncClient(timeout=10, transport=self._transport) as c:
            while time.time() - t0 < limit:
                await asyncio.sleep(every)
                try:
                    j = (await c.get(url, headers={"X-Api-Key": self.live_token})).json()
                except Exception:
                    continue
                if j.get("twilio_sid") and (j.get("status") in ("created", "queued", "") or not j.get("status")) \
                        or (j.get("twilio_sid") and time.time() - t0 > 20 and j.get("status") not in FINAL):
                    # trial accounts get no status callbacks, so ask Twilio directly
                    try:
                        tw = (await c.get(API.format(sid=self.sid) + "/Calls/%s.json" % j["twilio_sid"],
                                          auth=(self.sid, self.token))).json()
                        j["status"] = tw.get("status") or j.get("status")
                        if tw.get("duration"):
                            note["duration_s"] = int(tw["duration"])
                    except Exception:
                        pass
                turns = ["%s: %s" % (x.get("speaker"), (x.get("text") or "").strip()) for x in j.get("transcript") or []]
                sig = (j.get("status"), j.get("live"), len(turns), turns[-1] if turns else "")
                if sig != last:
                    last = sig
                    note["status"], note["live"] = j.get("status") or note["status"], j.get("live") or ""
                    note["error"] = j.get("error") or None
                    note["text"] = brief + ("\n\nTranscript (GPT-Live):\n" + "\n".join(turns) if turns else "")
                    on_update(note)
                if j.get("status") in FINAL and j.get("live") not in ("connecting", "active"):
                    return
