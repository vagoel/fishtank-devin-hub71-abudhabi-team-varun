"""Self-test for server/services/whatsapp.py. Sends nothing real.

Every provider test runs against httpx.MockTransport and a private env dict,
so real credentials in the shell or .env are never read or used.

    .venv/bin/python scripts/test_whatsapp.py
"""
import asyncio
import base64
import json
import logging
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import httpx  # noqa: E402

from services import whatsapp as wa  # noqa: E402
from services.whatsapp import WhatsApp, mask_phone  # noqa: E402

PHONE = "+971501234123"
PHONE2 = "+971559876543"
MASKED = "+9715•••••123"
FAKE_TOKEN = "fake-token-0123456789abcdef"
FAKE_SID = "AC00000000000000000000000000000000"
FAKE_KEY = "fake-callmebot-key-987"

T0 = 1790000000.0  # alert time used everywhere below

ALERT = {
    "id": "A-000042", "worker_id": "LIVE-7ce8b1e3", "worker_name": "Ramesh Kumar",
    "live": True, "simulated": False, "type": "fall", "severity": "critical",
    "title": "Fall detected · NO RESPONSE",
    "message": "Free-fall 180 ms, impact 3.4 g, then no movement. No answer to the on-wrist prompt. Send help.",
    "ts": T0, "state": "open", "source": "device", "has_window": True, "devin": None,
    "timeline": [{"type": "fall", "ts": T0}, {"type": "fall_noresp", "ts": T0 + 15},
                 {"type": "whatsapp_auto", "ts": T0 + 15}],
    "detail": {"impact_g": 3.4, "freefall_ms": 180},
}
WORKER = {"id": "LIVE-7ce8b1e3", "name": "Ramesh Kumar", "trade": "Steel fixer",
          "zone": "Tower B · Level 4", "crew": "B-2", "live": True}
SITE = {"name": "Dubai South · Tower B site (demo)", "lat": 24.8962, "lon": 55.1602,
        "temp_c": 41.2, "rh": 48, "wbgt_c": 32.4,
        "category": {"level": 4, "label": "Extreme", "color": "#b91c1c"},
        "midday_break": {"active": False, "in_season": True, "window": "12:30–15:00"}}

LOGS = []


class Capture(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


for _h in list(wa.log.handlers):  # keep the test output to the composed message
    wa.log.removeHandler(_h)
wa.log.addHandler(Capture())
wa.log.setLevel(logging.INFO)
wa.log.propagate = False

passed = 0


def check(cond, what):
    global passed
    if not cond:
        raise AssertionError(what)
    passed += 1


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def no_leaks(obj, *extra):
    blob = json.dumps(obj, ensure_ascii=False)
    for s in (PHONE, PHONE.lstrip("+"), PHONE2, PHONE2.lstrip("+"), FAKE_TOKEN, FAKE_KEY) + extra:
        check(s not in blob, "leaked %r in %s" % (s[:6] + "…", blob[:120]))


def recorder(responder):
    seen = []

    def handler(request):
        seen.append(request)
        return responder(request)
    return seen, httpx.MockTransport(handler)


async def main():
    # 1. masking + compose ------------------------------------------------
    check(mask_phone(PHONE) == MASKED, "mask_phone: %s" % mask_phone(PHONE))
    check(wa.normalize_phone("whatsapp:+971 50-123 4123") == PHONE, "normalize whatsapp: prefix")
    check(wa.normalize_phone("00971501234123") == PHONE, "normalize 00 prefix")
    check(wa.normalize_phone("hello") is None, "reject junk")

    w = WhatsApp(env={"HEATGUARD_PUBLIC_URL": "http://10.0.0.2:8000/"})
    text, loc = w.compose(ALERT, WORKER, SITE)
    print("=" * 64)
    print(text)
    print("=" * 64)
    print("location:", json.dumps(loc, ensure_ascii=False))
    for s in ("*CRITICAL: Fall detected · NO RESPONSE*", "Ramesh Kumar", "Steel fixer", "Crew B-2",
              "Tower B · Level 4", "Dubai time", "Free-fall 180 ms", "*NO ANSWER*", "WBGT 32.4 °C",
              "Extreme", "Call 998 (Ambulance) if unresponsive", "https://maps.google.com/?q=24.8962,55.1602",
              "http://10.0.0.2:8000/#alert=A-000042", "تنبيه سلامة", "سقوط عامل", "998", "A-000042"):
        check(s in text, "compose missing %r" % s)
    check("|" not in text and "```" not in text, "no markdown tables/code in WhatsApp text")
    check(loc == {"lat": 24.8962, "lon": 55.1602, "label": "Tower B · Level 4 · Crew B-2 · Dubai South · Tower B site (demo)",
                  "maps_url": "https://maps.google.com/?q=24.8962,55.1602"}, "location dict: %r" % loc)
    check(len(text) < 1600, "fits one Twilio message (%d chars)" % len(text))
    # 1 PM on a 25 Sep in Dubai is 09:00 UTC; check the timezone conversion.
    t_dubai, _ = w.compose(dict(ALERT, ts=1790326800.0), WORKER, SITE)  # 2026-09-25 09:00 UTC
    check("13:00 Dubai time" in t_dubai, "Asia/Dubai time")
    ok_alert = dict(ALERT, timeline=[{"type": "fall"}, {"type": "fall_ok"}], severity="info",
                    title="Fall detected · worker OK", state="resolved")
    t_ok, _ = w.compose(ok_alert, WORKER, SITE)
    check("I'M OK" in t_ok and "أكد العامل أنه بخير" in t_ok, "worker-OK response line")
    t_pending, _ = w.compose(dict(ALERT, timeline=[{"type": "fall"}]), None, {})
    check("Waiting for an answer" in t_pending and "WBGT not available" in t_pending, "pending + no site data")

    # 2. dry run ----------------------------------------------------------
    clock = Clock(T0 + 20)
    d = WhatsApp(env={"WHATSAPP_TO": PHONE + ", " + PHONE2}, clock=clock)
    check(d.describe() == {"provider": "dryrun", "enabled": False, "dry_run": True,
                           "recipients": [MASKED, "+9715•••••543"],
                           "reason": "dry run (WHATSAPP_PROVIDER not set)"}, "describe: %r" % d.describe())
    notes = await d.send_incident(ALERT, WORKER, SITE)
    check(len(notes) == 2 and all(n["status"] == "dry_run" for n in notes), "dry run statuses")
    n = notes[0]
    check(set(n) == {"id", "channel", "provider", "to", "status", "alert_id", "text", "location", "ts",
                     "error", "provider_id"}, "notification keys")
    check(n["id"] == "N-000001" and n["channel"] == "whatsapp" and n["to"] == MASKED
          and n["alert_id"] == "A-000042" and n["provider"] == "dryrun", "notification fields: %r" % n)
    no_leaks(notes)

    # 3. rate limit: same alert + recipient once per 60 s unless severity rises
    warn = dict(ALERT, severity="warning")
    d2 = WhatsApp(env={"WHATSAPP_TO": PHONE}, clock=clock)
    check(len(await d2.send_incident(warn, WORKER, SITE)) == 1, "first send")
    clock.t += 10
    check(await d2.send_incident(warn, WORKER, SITE) == [], "duplicate within 60 s suppressed")
    check(len(await d2.send_incident(ALERT, WORKER, SITE)) == 1, "severity went up -> sent")
    clock.t += 30
    check(await d2.send_incident(ALERT, WORKER, SITE) == [], "same severity again -> suppressed")
    check(len(await d2.send_incident(ALERT, WORKER, SITE, force=True)) == 1, "force bypasses guard")
    clock.t += 61
    check(len(await d2.send_incident(ALERT, WORKER, SITE)) == 1, "after 60 s -> sent")
    check(len(await d2.send_incident(dict(ALERT, id="A-000043"), WORKER, SITE)) == 1, "other alert -> sent")
    none = WhatsApp(env={}, clock=clock)
    nn = await none.send_incident(ALERT, WORKER, SITE)
    check(len(nn) == 1 and nn[0]["to"] == "(not set)" and nn[0]["status"] == "dry_run", "dry run w/o recipients")

    # 4. missing credentials fall back to dry run -------------------------
    half = WhatsApp(env={"WHATSAPP_PROVIDER": "twilio", "WHATSAPP_TO": PHONE, "TWILIO_ACCOUNT_SID": FAKE_SID})
    check(not half.enabled and half.describe()["dry_run"], "twilio w/o token is dry run")
    check("TWILIO_AUTH_TOKEN" in half.describe()["reason"], "reason names missing key")
    check((await half.send_incident(ALERT, WORKER, SITE))[0]["status"] == "dry_run", "half-configured -> dry_run")

    # 5. Twilio request building -----------------------------------------
    seen, tr = recorder(lambda r: httpx.Response(201, json={"sid": "SMabc123", "status": "queued"}))
    tw = WhatsApp(env={"WHATSAPP_PROVIDER": "twilio", "WHATSAPP_TO": PHONE, "TWILIO_ACCOUNT_SID": FAKE_SID,
                       "TWILIO_AUTH_TOKEN": FAKE_TOKEN, "TWILIO_WHATSAPP_FROM": "+14155238886"}, transport=tr)
    check(tw.enabled and tw.provider == "twilio", "twilio enabled")
    notes = await tw.send_incident(ALERT, WORKER, SITE)
    check(len(seen) == 1, "one Twilio request")
    req = seen[0]
    check(req.method == "POST" and str(req.url) ==
          "https://api.twilio.com/2010-04-01/Accounts/%s/Messages.json" % FAKE_SID, "twilio url %s" % req.url)
    basic = "Basic " + base64.b64encode(("%s:%s" % (FAKE_SID, FAKE_TOKEN)).encode()).decode()
    check(req.headers["authorization"] == basic, "twilio basic auth")
    check(req.headers["content-type"].startswith("application/x-www-form-urlencoded"), "twilio form encoding")
    form = dict(urllib.parse.parse_qsl(req.content.decode()))
    check(form["From"] == "whatsapp:+14155238886" and form["To"] == "whatsapp:" + PHONE, "twilio From/To")
    check("https://maps.google.com/?q=24.8962,55.1602" in form["Body"] and "*CRITICAL" in form["Body"], "twilio body")
    check(notes[0]["status"] == "sent" and notes[0]["provider_id"] == "SMabc123" and notes[0]["to"] == MASKED,
          "twilio note %r" % notes[0])
    no_leaks(notes, FAKE_SID)

    seen, tr = recorder(lambda r: httpx.Response(400, json={
        "code": 21211, "message": "Invalid 'To' Phone Number: %s" % PHONE, "status": 400}))
    tw_bad = WhatsApp(env={"WHATSAPP_PROVIDER": "twilio", "WHATSAPP_TO": PHONE, "TWILIO_ACCOUNT_SID": FAKE_SID,
                           "TWILIO_AUTH_TOKEN": FAKE_TOKEN, "TWILIO_WHATSAPP_FROM": "whatsapp:+14155238886"},
                      transport=tr, clock=Clock(T0))
    bad = await tw_bad.send_incident(ALERT, WORKER, SITE)
    check(bad[0]["status"] == "failed" and "21211" in bad[0]["error"] and MASKED in bad[0]["error"],
          "twilio failure %r" % bad[0]["error"])
    no_leaks(bad, FAKE_SID)
    bad2 = await tw_bad.send_incident(ALERT, WORKER, SITE)
    check(len(bad2) == 1, "a failed send doesn't block a retry")

    # 6. Meta Cloud API: text, then native location -----------------------
    ids = iter(["wamid.TEXT1", "wamid.LOC1"])
    seen, tr = recorder(lambda r: httpx.Response(200, json={
        "messaging_product": "whatsapp", "contacts": [{"input": "x", "wa_id": "x"}],
        "messages": [{"id": next(ids)}]}))
    me = WhatsApp(env={"WHATSAPP_PROVIDER": "meta", "WHATSAPP_TO": PHONE, "META_WA_TOKEN": FAKE_TOKEN,
                       "META_WA_PHONE_ID": "123456789012345"}, transport=tr)
    notes = await me.send_incident(ALERT, WORKER, SITE)
    check(len(seen) == 2, "meta sends text + location (%d requests)" % len(seen))
    for r in seen:
        check(str(r.url) == "https://graph.facebook.com/v21.0/123456789012345/messages", "meta url %s" % r.url)
        check(r.headers["authorization"] == "Bearer " + FAKE_TOKEN, "meta bearer auth")
    b1, b2 = (json.loads(r.content) for r in seen)
    check(b1["messaging_product"] == "whatsapp" and b1["to"] == PHONE.lstrip("+") and b1["type"] == "text",
          "meta text envelope")
    check("*NO ANSWER*" in b1["text"]["body"] and b1["text"]["preview_url"] is False, "meta text body")
    check(b2["type"] == "location" and b2["location"]["latitude"] == "24.8962"
          and b2["location"]["longitude"] == "55.1602", "meta location coords")
    check("Ramesh Kumar" in b2["location"]["name"] and "Tower B · Level 4" in b2["location"]["address"],
          "meta location name/address")
    check(notes[0]["status"] == "sent" and notes[0]["provider_id"] == "wamid.TEXT1" and notes[0]["error"] is None,
          "meta note %r" % notes[0])
    no_leaks(notes)

    # template instead of free text (business-initiated, outside the 24 h window)
    seen, tr = recorder(lambda r: httpx.Response(200, json={"messages": [{"id": "wamid.T"}]}))
    mt = WhatsApp(env={"WHATSAPP_PROVIDER": "meta", "WHATSAPP_TO": PHONE, "META_WA_TOKEN": FAKE_TOKEN,
                       "META_WA_PHONE_ID": "123456789012345", "META_WA_TEMPLATE": "heatguard_incident",
                       "META_WA_TEMPLATE_LANG": "en",
                       "META_WA_TEMPLATE_PARAMS": "headline,worker,where,response,maps"}, transport=tr)
    await mt.send_incident(ALERT, WORKER, SITE)
    t1 = json.loads(seen[0].content)
    check(t1["type"] == "template" and t1["template"]["name"] == "heatguard_incident"
          and t1["template"]["language"] == {"code": "en"}, "meta template envelope")
    params = t1["template"]["components"][0]["parameters"]
    check(len(params) == 5 and params[1]["text"] == "Ramesh Kumar"
          and params[4]["text"] == "https://maps.google.com/?q=24.8962,55.1602", "template params %r" % params)
    check(all("\n" not in p["text"] and "    " not in p["text"] for p in params), "template params single-line")
    check("text" not in t1, "no free text when a template is set")

    # location pin failing doesn't fail the whole notification
    calls = {"n": 0}

    def half_ok(r):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"messages": [{"id": "wamid.OK"}]})
        return httpx.Response(400, json={"error": {"message": "Re-engagement message", "code": 131047}})
    _, tr = recorder(half_ok)
    mp = WhatsApp(env={"WHATSAPP_PROVIDER": "meta", "WHATSAPP_TO": PHONE, "META_WA_TOKEN": FAKE_TOKEN,
                       "META_WA_PHONE_ID": "1"}, transport=tr)
    part = await mp.send_incident(ALERT, WORKER, SITE)
    check(part[0]["status"] == "sent" and "location pin failed" in part[0]["error"], "partial meta %r" % part[0])

    # 7. CallMeBot --------------------------------------------------------
    seen, tr = recorder(lambda r: httpx.Response(200, text="<p>Message queued. You will receive it in a few seconds.</p>"))
    cb = WhatsApp(env={"WHATSAPP_PROVIDER": "callmebot", "WHATSAPP_TO": PHONE + "," + PHONE2,
                       "CALLMEBOT_APIKEY": FAKE_KEY}, transport=tr)
    check(cb.recipients_masked == [MASKED], "callmebot: single recipient")
    notes = await cb.send_incident(ALERT, WORKER, SITE)
    q = dict(urllib.parse.parse_qsl(seen[0].url.query.decode()))
    check(seen[0].method == "GET" and seen[0].url.host == "api.callmebot.com"
          and seen[0].url.path == "/whatsapp.php", "callmebot url")
    check(q["phone"] == PHONE and q["apikey"] == FAKE_KEY and "تنبيه سلامة" in q["text"], "callmebot params")
    check(notes[0]["status"] == "sent", "callmebot sent")
    _, tr = recorder(lambda r: httpx.Response(200, text="APIKey is invalid. Please check %s" % FAKE_KEY))
    cbad = WhatsApp(env={"WHATSAPP_PROVIDER": "callmebot", "WHATSAPP_TO": PHONE, "CALLMEBOT_APIKEY": FAKE_KEY},
                    transport=tr)
    notes = await cbad.send_incident(ALERT, WORKER, SITE)
    check(notes[0]["status"] == "failed" and "invalid" in notes[0]["error"].lower(), "callmebot failure")
    no_leaks(notes)

    # 8. never raises -----------------------------------------------------
    def boom(r):
        raise httpx.ConnectError("connect failed for %s?apikey=%s&phone=%s" % (r.url.host, FAKE_KEY, PHONE))
    _, tr = recorder(boom)
    down = WhatsApp(env={"WHATSAPP_PROVIDER": "callmebot", "WHATSAPP_TO": PHONE, "CALLMEBOT_APIKEY": FAKE_KEY},
                    transport=tr)
    notes = await down.send_incident(ALERT, WORKER, SITE)
    check(notes[0]["status"] == "failed" and "ConnectError" in notes[0]["error"], "network error -> failed")
    no_leaks(notes)
    weird = await down.send_incident(None, None, None)
    check(isinstance(weird, list), "garbage input doesn't raise")
    weird2 = await d.send_incident({"id": "A-9", "ts": "not-a-time", "category": 5}, {"name": None}, {"lat": "x"})
    check(isinstance(weird2, list), "odd field types don't raise")

    # 9. nothing sensitive in the log --------------------------------------
    check(LOGS, "log captured")
    no_leaks(LOGS, FAKE_SID)

    print("\nOK: %d checks passed" % passed)


if __name__ == "__main__":
    asyncio.run(main())
