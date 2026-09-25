"""WhatsApp escalation for critical incidents (docs/services.md §5).

One message per recipient that a site HSE officer or medic can act on in five
seconds: what happened, who, where (site pin + zone, since the wearable has no
GPS), whether the worker answered the on-wrist "are you OK?", the heat
conditions, and what to do now -- plus the same key facts in Arabic.

Providers (WHATSAPP_PROVIDER):
  dryrun     default. Composes the message and writes it to the log only.
  twilio     Twilio Programmable Messaging, WhatsApp channel.
  meta       WhatsApp Business Cloud API: text (or approved template), then a
             native location pin.
  callmebot  CallMeBot free personal API, single recipient.

Env (all optional; anything missing means dry run):
  WHATSAPP_PROVIDER, WHATSAPP_TO (comma-separated E.164)
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM (whatsapp:+14155238886)
  META_WA_TOKEN, META_WA_PHONE_ID, META_WA_TEMPLATE (approved template name)
    META_WA_TEMPLATE_LANG    template language code, default en_US
    META_WA_TEMPLATE_PARAMS  comma-separated facts for the template body
                             {{1}}, {{2}}, ... e.g. "headline,worker,where,response,maps"
    META_WA_API_VERSION      default v21.0
  CALLMEBOT_APIKEY
  HEATGUARD_PUBLIC_URL (dashboard link to include)

Business-initiated WhatsApp messages outside the 24 h customer-care window
must use an approved template (Meta) or Content template (Twilio); free text
is only delivered inside that window or in the Twilio sandbox.

Phone numbers are masked in everything this module returns or logs. Secrets
are never logged. send_incident() never raises. The same alert and recipient
get at most one message per 60 s unless the severity went up; suppressed
recipients are left out of the returned list and noted in the log.
"""
import asyncio
import json
import itertools
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger("heatguard.whatsapp")
if not log.handlers and not logging.getLogger().handlers:
    # uvicorn only configures its own loggers; without this the dry-run log would be silent.
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)
    log.propagate = False

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Dubai")
except Exception:  # no tz database on this box: the UAE has no DST, so a fixed offset is exact
    TZ = timezone(timedelta(hours=4), "GST")

PROVIDERS = ("dryrun", "twilio", "meta", "callmebot")
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
RATE_LIMIT_S = 60.0
TIMEOUT_S = 10.0
NO_RECIPIENT = "(not set)"

TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
META_URL = "https://graph.facebook.com/{ver}/{phone_id}/messages"
CALLMEBOT_URL = "https://api.callmebot.com/whatsapp.php"

# Alert types that ask the worker "are you OK?" on the wrist before escalating.
PROMPTED = ("fall", "impact", "tremor", "erratic", "inactivity")

TYPE_AR = {
    "fall": "سقوط عامل",
    "impact": "صدمة قوية",
    "tremor": "ارتجاف غير طبيعي",
    "erratic": "حركة غير طبيعية",
    "inactivity": "عدم حركة",
    "sos": "نداء استغاثة SOS",
    "unwell": "عامل يشعر بتوعك",
    "heat_limit": "تجاوز حد التعرض للحرارة",
    "heat_critical": "خطر إجهاد حراري",
    "rest_violation": "عمل أثناء فترة الراحة",
    "offline": "انقطاع اتصال السوار",
}
SEVERITY_AR = {"critical": "حرج", "warning": "تحذير", "info": "معلومة"}
CATEGORY_AR = {"Low": "منخفض", "Moderate": "متوسط", "High": "مرتفع",
               "Very high": "مرتفع جدًا", "Extreme": "شديد"}

ACTIONS = {
    "fall": "Send the nearest first-aider now. Don't move them if a head or neck injury is possible.",
    "impact": "Crew lead to check on the worker in person.",
    "sos": "Send the nearest first-aider now and radio the crew lead.",
    "tremor": "Send a first-aider. Don't restrain; clear the area and time the episode.",
    "erratic": "Crew lead to check on the worker in person now.",
    "inactivity": "Crew lead to check on the worker in person now.",
    "unwell": "Shade, active cooling, water with electrolytes. No return to work this cycle.",
    "heat_critical": "Move to shade now, start active cooling, water if conscious.",
    "heat_limit": "Enforce the cool-down: shade, water, rest.",
    "rest_violation": "Crew lead: stop the task and enforce the cool-down.",
    "offline": "Radio the worker; check wearable battery and coverage.",
}

_E164 = re.compile(r"\+\d{7,15}")
_LONG_NUMBER = re.compile(r"\+?\d{8,15}")


def normalize_phone(raw):
    """'whatsapp:+971 50-123 4123' / '00971...' / '971...' -> '+971501234123', or None."""
    s = str(raw or "").strip()
    if s.lower().startswith("whatsapp:"):
        s = s[9:]
    s = re.sub(r"[\s\-().]", "", s)
    if s.startswith("00"):
        s = "+" + s[2:]
    if not s.startswith("+"):
        s = "+" + s
    return s if _E164.fullmatch(s) else None


def mask_phone(num):
    """'+971501234123' -> '+9715•••••123'."""
    s = re.sub(r"[^\d+]", "", str(num or ""))
    if not s:
        return NO_RECIPIENT
    if len(s) <= 7:
        return s[:2] + "•" * max(3, len(s) - 2)
    head = s[:5] if s.startswith("+") else s[:4]
    return head + "•" * max(3, len(s) - len(head) - 3) + s[-3:]


def _fmt_coord(v):
    return ("%.6f" % float(v)).rstrip("0").rstrip(".")


def _one_line(s, limit=900):
    """Template parameters may not contain newlines, tabs or 4+ spaces in a row."""
    s = re.sub(r"[\r\n\t]+", " · ", str(s or "-"))
    s = re.sub(r" {2,}", " ", s).strip()
    return (s or "-")[:limit]


class WhatsApp:
    def __init__(self, env=None, transport=None, clock=None):
        """env: mapping to read config from (default os.environ).
        transport: optional httpx transport (tests inject httpx.MockTransport).
        clock: optional time source for the rate limiter (tests)."""
        e = os.environ if env is None else env

        def get(k, default=""):
            return str(e.get(k) or default).strip()

        self._transport = transport
        self._clock = clock or time.time
        self._ids = itertools.count(1)
        self._sent = {}  # (alert_id, recipient) -> (ts, severity rank)
        self.public_url = get("HEATGUARD_PUBLIC_URL").rstrip("/")

        provider = get("WHATSAPP_PROVIDER", "dryrun").lower()
        self.reason = None
        if provider not in PROVIDERS:
            self.reason = "unknown WHATSAPP_PROVIDER '%s'" % provider[:20]
            provider = "dryrun"
        self.provider = provider

        self.recipients = []
        bad = 0
        for raw in get("WHATSAPP_TO").split(","):
            if not raw.strip():
                continue
            n = normalize_phone(raw)
            if n and n not in self.recipients:
                self.recipients.append(n)
            elif not n:
                bad += 1
        if bad:
            log.warning("whatsapp: ignored %d malformed WHATSAPP_TO entr%s (need E.164, e.g. +9715XXXXXXXX)",
                        bad, "y" if bad == 1 else "ies")

        self.twilio_sid = get("TWILIO_ACCOUNT_SID")
        self.twilio_token = get("TWILIO_AUTH_TOKEN")
        frm = get("TWILIO_WHATSAPP_FROM")
        if frm and not frm.lower().startswith("whatsapp:"):
            frm = "whatsapp:" + (normalize_phone(frm) or frm)
        self.twilio_from = frm
        # Trial accounts may only send Twilio-provided templates: set the HX sid and which
        # facts fill {{1}}, {{2}}, ... (same fact names as META_WA_TEMPLATE_PARAMS).
        self.twilio_content_sid = get("TWILIO_CONTENT_SID")
        self.twilio_content_params = [p.strip() for p in get("TWILIO_CONTENT_PARAMS").split(",") if p.strip()]
        self.meta_token = get("META_WA_TOKEN")
        self.meta_phone_id = get("META_WA_PHONE_ID")
        self.meta_template = get("META_WA_TEMPLATE")
        self.meta_template_lang = get("META_WA_TEMPLATE_LANG", "en_US")
        self.meta_template_params = [p.strip() for p in get("META_WA_TEMPLATE_PARAMS").split(",") if p.strip()]
        self.meta_version = get("META_WA_API_VERSION", "v21.0")
        self.callmebot_key = get("CALLMEBOT_APIKEY")

        need = {
            "dryrun": [],
            "twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM"],
            "meta": ["META_WA_TOKEN", "META_WA_PHONE_ID"],
            "callmebot": ["CALLMEBOT_APIKEY"],
        }[provider]
        have = {"TWILIO_ACCOUNT_SID": self.twilio_sid, "TWILIO_AUTH_TOKEN": self.twilio_token,
                "TWILIO_WHATSAPP_FROM": self.twilio_from, "META_WA_TOKEN": self.meta_token,
                "META_WA_PHONE_ID": self.meta_phone_id, "CALLMEBOT_APIKEY": self.callmebot_key}
        missing = [k for k in need if not have[k]]
        if provider != "dryrun" and not self.recipients:
            missing.append("WHATSAPP_TO")
        if missing and not self.reason:
            self.reason = ", ".join(missing) + " missing"
        self.enabled = provider != "dryrun" and not missing

        if provider == "callmebot" and len(self.recipients) > 1:
            log.warning("whatsapp: CallMeBot sends to one number only; using the first WHATSAPP_TO entry")
            self.recipients = self.recipients[:1]
        self.recipients_masked = [mask_phone(r) for r in self.recipients]
        self._secrets = [s for s in (self.twilio_token, self.twilio_sid, self.meta_token,
                                     self.callmebot_key) if s and len(s) >= 4]
        log.info("whatsapp: provider=%s enabled=%s recipients=%s%s", self.provider, self.enabled,
                 self.recipients_masked, (" (%s)" % self.reason) if self.reason else "")

    # ------------------------------------------------------------ public API
    def describe(self):
        d = {"provider": self.provider, "enabled": self.enabled, "dry_run": not self.enabled,
             "recipients": list(self.recipients_masked)}
        if not self.enabled:
            d["reason"] = self.reason or "dry run (WHATSAPP_PROVIDER not set)"
        return d

    def compose(self, alert, worker, site):
        """Returns (text, location). location = {'lat','lon','label','maps_url'}."""
        f = self.facts(alert, worker, site)
        lines = [
            "*%s*" % f["headline"],
            "*Worker:* %s" % " · ".join(x for x in (f["worker"], f["trade"], f["crew_label"]) if x),
            "*Where:* %s" % f["where"],
            "*When:* %s" % f["time_long"],
            "*Detected:* %s" % f["detected"],
        ]
        if f["response"]:
            lines.append("*Response:* %s" % f["response"])
        lines.append("*Heat:* %s" % f["heat"])
        if f["devin"]:
            lines.append("*Analysis:* %s" % f["devin"])
        lines.append("*Do now:* %s" % f["action"])
        lines.append("*Call 998 (Ambulance) if unresponsive.*")
        lines.append("")
        if f["maps"]:
            lines.append("*Map* (site pin + zone; the wearable has no GPS):")
            lines.append(f["maps"])
        if f["dashboard"]:
            lines.append("*Dashboard:* %s" % f["dashboard"])
        lines.append("")
        lines.append(f["arabic"])
        lines.append("")
        lines.append("HeatGuard · ref %s" % f["alert_id"])
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)), f["location"]

    async def send_incident(self, alert, worker, site, force=False):
        """One notification per recipient. Never raises; failures come back as
        status 'failed'. force=True skips the 60 s duplicate guard."""
        a = alert if isinstance(alert, dict) else {}
        try:
            return await self._send_incident(a, worker if isinstance(worker, dict) else None,
                                             site if isinstance(site, dict) else {}, force)
        except Exception as e:  # last line of defence: the caller must never see an exception
            err = "internal error: %s" % self._scrub("%s: %s" % (type(e).__name__, e))
            log.error("whatsapp: %s (alert %s)", err, a.get("id"))
            return [self._note(a, m, "failed", "", {}, self.provider, error=err)
                    for m in (self.recipients_masked or [NO_RECIPIENT])]

    # ------------------------------------------------------------ composing
    def facts(self, alert, worker, site):
        a = alert or {}
        w = worker or {}
        s = site or {}
        atype = str(a.get("type") or "alert")
        sev = str(a.get("severity") or "warning").lower()
        title = str(a.get("title") or atype.replace("_", " ").capitalize())
        state = str(a.get("state") or "open")
        head = "%s: %s" % (sev.upper(), title)
        if a.get("simulated"):
            head = "SIMULATED · " + head
        if state not in ("open", ""):
            head += " (%s)" % state.replace("_", " ")

        name = w.get("name") or a.get("worker_name") or a.get("worker_id") or "Unknown worker"
        trade = w.get("trade") or a.get("trade") or ""
        crew = w.get("crew") or a.get("crew") or ""
        zone = w.get("zone") or a.get("zone") or ""
        site_name = s.get("name") or "Site"
        where = " · ".join(x for x in (zone, site_name) if x)

        try:
            ts = float(a.get("ts") or self._clock())
        except (TypeError, ValueError):
            ts = self._clock()
        dt = datetime.fromtimestamp(ts, TZ)
        hhmm = dt.strftime("%H:%M")
        time_long = "%s Dubai time (%s)" % (hhmm, dt.strftime("%a %d %b").replace(" 0", " "))

        detected = str(a.get("message") or title).strip()
        resp, resp_ar = self._response(a, atype)

        wbgt = s.get("wbgt_c")
        cat = (s.get("category") or {}).get("label") if isinstance(s.get("category"), dict) else None
        if wbgt is not None:
            heat = "WBGT %.1f °C" % float(wbgt) + (" · %s" % cat if cat else "")
            if s.get("temp_c") is not None:
                heat += " (air %.0f °C)" % float(s["temp_c"])
        else:
            heat = "WBGT not available"
        if (s.get("midday_break") or {}).get("active"):
            heat += " · midday break in force (%s)" % s["midday_break"].get("window", "12:30–15:00")

        loc = self._location(w, s, zone, crew, site_name)
        aid = str(a.get("id") or "-")
        dashboard = ("%s/#alert=%s" % (self.public_url, aid)) if self.public_url else ""

        dv = a.get("devin") if isinstance(a.get("devin"), dict) else {}
        devin = ""
        if dv.get("status") == "done" and dv.get("verdict"):
            conf = dv.get("confidence")
            devin = str(dv["verdict"]).replace("_", " ")
            if isinstance(conf, (int, float)):
                devin += " (%d%%)" % round(conf * 100)
            if dv.get("summary"):
                devin += ". " + str(dv["summary"])[:160]

        ar = ["تنبيه سلامة (%s): %s – %s" % (SEVERITY_AR.get(sev, sev), TYPE_AR.get(atype, title), name)]
        if zone:
            ar.append("، " + zone)
        ar.append("، الساعة %s." % hhmm)
        if resp_ar:
            ar.append(" " + resp_ar)
        if wbgt is not None:
            ar.append(" مؤشر WBGT ‏%.1f°م%s." % (float(wbgt), (" (%s)" % CATEGORY_AR.get(cat, cat)) if cat else ""))
        ar.append(" اتصلوا بالإسعاف 998 إذا لم يستجب.")

        return {
            "alert_id": aid, "type": atype, "severity": sev, "title": title, "headline": head,
            "worker": name, "trade": trade, "crew": crew, "crew_label": ("Crew " + crew) if crew else "",
            "zone": zone, "site": site_name, "where": where, "time": hhmm, "time_long": time_long,
            "detected": detected, "response": resp, "heat": heat, "wbgt": "" if wbgt is None else "%.1f" % float(wbgt),
            "category": cat or "", "action": ACTIONS.get(atype, "Check on the worker in person."),
            "maps": loc["maps_url"] or "", "location": loc, "location_label": loc["label"],
            "dashboard": dashboard, "devin": devin, "arabic": "".join(ar),
        }

    @staticmethod
    def _response(a, atype):
        """Did the worker answer the on-wrist 'are you OK?' prompt? From alert.timeline."""
        last = None
        for e in a.get("timeline") or []:
            t = e.get("type") if isinstance(e, dict) else e
            t = str(t or "")
            if t.endswith(("_ok", "_noresp", "_cancel")) and not t.startswith(("whatsapp_", "supervisor_")):
                last = t.rsplit("_", 1)[1]
        if last == "ok":
            return "Worker answered *I'M OK* on the wearable.", "أكد العامل أنه بخير."
        if last == "noresp":
            return ("*NO ANSWER* to the \"Are you OK?\" prompt on the wearable.",
                    "لم يرد على سؤال «هل أنت بخير؟» على السوار.")
        if last == "cancel":
            return "Worker cancelled the alarm on the wearable.", "ألغى العامل التنبيه."
        if atype == "sos":
            return "Worker pressed SOS on the wearable.", "ضغط العامل زر الاستغاثة."
        if atype == "unwell":
            return "Worker reported feeling unwell.", "أبلغ العامل أنه يشعر بتوعك."
        if atype in PROMPTED:
            return "Waiting for an answer to \"Are you OK?\" on the wearable.", "بانتظار رد العامل."
        return "", ""

    @staticmethod
    def _location(w, s, zone, crew, site_name):
        """The StickS3 has no GPS: site coordinates + zone/crew as the label.
        A worker-level lat/lon is used if the core ever provides one."""
        lat, lon = w.get("lat"), w.get("lon")
        if lat is None or lon is None:
            lat, lon = s.get("lat"), s.get("lon")
        label = " · ".join(x for x in (zone, ("Crew " + crew) if crew else "", site_name) if x)
        try:
            lat, lon = float(lat), float(lon)
            maps = "https://maps.google.com/?q=%s,%s" % (_fmt_coord(lat), _fmt_coord(lon))
        except (TypeError, ValueError):
            lat = lon = None
            maps = None
        return {"lat": lat, "lon": lon, "label": label, "maps_url": maps}

    # ------------------------------------------------------------ sending
    async def _send_incident(self, a, w, s, force):
        text, loc = self.compose(a, w, s)
        aid = str(a.get("id") or "")
        sev = SEVERITY_RANK.get(str(a.get("severity") or "").lower(), 1)
        now = self._clock()
        self._prune(now)

        targets = self.recipients if self.enabled else (self.recipients or [None])
        todo = []
        for to in targets:
            key = (aid, to or "")
            prev = self._sent.get(key)
            if not force and prev and now - prev[0] < RATE_LIMIT_S and sev <= prev[1]:
                log.info("whatsapp: suppressed duplicate for %s to %s (sent %.0f s ago, severity unchanged)",
                         aid, mask_phone(to), now - prev[0])
                continue
            self._sent[key] = (now, sev)  # reserve now so concurrent calls don't double-send
            todo.append((to, key, prev))
        if not todo:
            return []

        if not self.enabled:
            notes = []
            for to, _, _ in todo:
                n = self._note(a, mask_phone(to), "dry_run", text, loc, "dryrun")
                log.info("whatsapp[dry-run] %s -> %s for %s:\n%s", n["id"], n["to"], aid, text)
                notes.append(n)
            return notes

        send = {"twilio": self._twilio, "meta": self._meta, "callmebot": self._callmebot}[self.provider]
        async with httpx.AsyncClient(timeout=TIMEOUT_S, transport=self._transport) as c:
            results = await asyncio.gather(*(send(c, to, text, loc, a, w, s) for to, _, _ in todo),
                                           return_exceptions=True)
        notes = []
        for (to, key, prev), res in zip(todo, results):
            if isinstance(res, Exception):
                status, pid, err = "failed", None, self._exc(res)
            else:
                status, pid, err = res
            if status == "failed":  # release the slot so a retry isn't blocked
                if prev:
                    self._sent[key] = prev
                else:
                    self._sent.pop(key, None)
            n = self._note(a, mask_phone(to), status, text, loc, self.provider, error=err, provider_id=pid)
            if status == "failed":
                log.warning("whatsapp: %s %s -> %s failed: %s", n["id"], self.provider, n["to"], err)
            else:
                log.info("whatsapp: %s %s -> %s %s (%s)%s", n["id"], self.provider, n["to"], status, pid,
                         (" note: %s" % err) if err else "")
            notes.append(n)
        return notes

    async def _twilio(self, c, to, text, loc, a, w, s):
        url = TWILIO_URL.format(sid=self.twilio_sid)
        data = {"From": self.twilio_from, "To": "whatsapp:" + to}
        if self.twilio_content_sid:
            data["ContentSid"] = self.twilio_content_sid
            if self.twilio_content_params:
                f = self.facts(a, w, s)
                data["ContentVariables"] = json.dumps(
                    {str(i + 1): _one_line(f.get(k, ""))[:1000] for i, k in enumerate(self.twilio_content_params)})
        else:
            data["Body"] = text[:1600]
        r = await c.post(url, data=data, auth=(self.twilio_sid, self.twilio_token))
        j = self._json(r)
        if r.status_code >= 400:
            return "failed", None, self._scrub("Twilio HTTP %d: %s %s" % (
                r.status_code, j.get("code", ""), j.get("message") or r.text[:200]))
        if j.get("status") in ("failed", "undelivered"):
            return "failed", j.get("sid"), self._scrub("Twilio %s: %s %s" % (
                j.get("status"), j.get("error_code", ""), j.get("error_message") or ""))
        return "sent", j.get("sid"), None

    async def _meta(self, c, to, text, loc, a, w, s):
        url = META_URL.format(ver=self.meta_version, phone_id=self.meta_phone_id)
        headers = {"Authorization": "Bearer " + self.meta_token}
        r = await c.post(url, json=self.meta_payload(to, text, a, w, s), headers=headers)
        ok, pid, err = self._meta_result(r)
        if not ok:
            return "failed", None, err
        note = None
        if loc.get("lat") is not None:
            try:
                r2 = await c.post(url, json=self.meta_location_payload(to, loc, a, w), headers=headers)
                ok2, _, err2 = self._meta_result(r2)
                if not ok2:
                    note = "text sent; location pin failed: " + (err2 or "")
            except Exception as e:
                note = "text sent; location pin failed: " + self._exc(e)
        return "sent", pid, note

    def meta_payload(self, to, text, a, w, s):
        base = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to.lstrip("+")}
        if self.meta_template:
            tpl = {"name": self.meta_template, "language": {"code": self.meta_template_lang}}
            if self.meta_template_params:
                f = self.facts(a, w, s)
                tpl["components"] = [{"type": "body", "parameters": [
                    {"type": "text", "text": _one_line(f.get(k, ""))} for k in self.meta_template_params]}]
            return dict(base, type="template", template=tpl)
        return dict(base, type="text", text={"preview_url": False, "body": text[:4096]})

    def meta_location_payload(self, to, loc, a, w):
        name = w.get("name") if w else a.get("worker_name")
        return {
            "messaging_product": "whatsapp", "recipient_type": "individual", "to": to.lstrip("+"),
            "type": "location",
            "location": {
                "latitude": _fmt_coord(loc["lat"]), "longitude": _fmt_coord(loc["lon"]),
                "name": ("HeatGuard: %s · %s" % (a.get("title") or "Incident", name or "worker"))[:100],
                "address": (loc.get("label") or "")[:200],
            },
        }

    def _meta_result(self, r):
        j = self._json(r)
        if r.status_code >= 400 or "error" in j:
            e = j.get("error") if isinstance(j.get("error"), dict) else {}
            detail = (e.get("error_data") or {}).get("details") if isinstance(e.get("error_data"), dict) else ""
            return False, None, self._scrub("Meta HTTP %d: %s %s %s" % (
                r.status_code, e.get("code", ""), e.get("message") or r.text[:200], detail or ""))
        msgs = j.get("messages") or [{}]
        return True, (msgs[0] or {}).get("id"), None

    async def _callmebot(self, c, to, text, loc, a, w, s):
        r = await c.get(CALLMEBOT_URL, params={"phone": to, "text": text, "apikey": self.callmebot_key})
        body = re.sub(r"<[^>]+>", " ", r.text or "")
        body = re.sub(r"\s+", " ", body).strip()
        low = body.lower()
        if r.status_code >= 400 or ("queued" not in low and re.search(r"error|invalid|not allowed|not active", low)):
            return "failed", None, self._scrub("CallMeBot HTTP %d: %s" % (r.status_code, body[:200]))
        return "sent", None, None

    # ------------------------------------------------------------ helpers
    def _note(self, a, to_masked, status, text, loc, provider, error=None, provider_id=None):
        return {"id": "N-%06d" % next(self._ids), "channel": "whatsapp", "provider": provider,
                "to": to_masked, "status": status, "alert_id": a.get("id"), "text": text,
                "location": loc, "ts": self._clock(), "error": error, "provider_id": provider_id}

    def _prune(self, now):
        if len(self._sent) > 500:
            for k in [k for k, v in self._sent.items() if now - v[0] > RATE_LIMIT_S * 10]:
                self._sent.pop(k, None)

    @staticmethod
    def _json(r):
        try:
            j = r.json()
            return j if isinstance(j, dict) else {}
        except Exception:
            return {}

    def _exc(self, e):
        # Don't echo exception text blindly: httpx errors can carry the request URL,
        # which for CallMeBot includes the API key and the phone number.
        return self._scrub("%s: %s" % (type(e).__name__, e))[:200]

    def _scrub(self, text):
        s = str(text or "")
        for secret in self._secrets:
            s = s.replace(secret, "***")
        for r in self.recipients:
            m = mask_phone(r)
            s = s.replace(r, m).replace(r.lstrip("+"), m).replace("%2B" + r[1:], m)
        s = _LONG_NUMBER.sub(lambda m: mask_phone(m.group()), s)
        return re.sub(r"\s+", " ", s).strip()[:300]
