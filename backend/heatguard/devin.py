"""Incident analysis: Devin when configured, a transparent local analyser otherwise.

Detection that keeps people safe runs on the wrist in milliseconds. Devin runs
after the fact: it gets the raw IMU trace, the heat context and the detector's
own thresholds, works the numbers in its sandbox, and returns a structured
verdict plus concrete threshold changes -- so every incident makes the
detector better.

Env:
  DEVIN_API_KEY   service-user key (cog_...) for API v3, or a legacy apk_ key
  DEVIN_ORG_ID    org-... (optional: looked up from /v3/self when missing)
  DEVIN_API_BASE  default https://api.devin.ai
  DEVIN_MAX_ACU   per-session ACU cap, default 2
  DEVIN_MODE      optional devin_mode override, e.g. "fast"
"""
import json
import math
import os

import httpx

VERDICTS = ["true_fall", "false_alarm", "tremor_or_seizure_like", "erratic_movement",
            "heat_illness_risk", "needs_human_review"]

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": VERDICTS},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "recommended_actions": {"type": "array", "items": {"type": "string"}},
        "suggested_threshold_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "parameter": {"type": "string"},
                    "current": {"type": "number"},
                    "proposed": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["parameter", "proposed", "rationale"],
            },
        },
    },
    "required": ["verdict", "confidence", "summary", "evidence", "recommended_actions"],
}


class Devin:
    def __init__(self):
        self.key = os.environ.get("DEVIN_API_KEY", "").strip()
        self.org = os.environ.get("DEVIN_ORG_ID", "").strip()
        self.base = os.environ.get("DEVIN_API_BASE", "https://api.devin.ai").rstrip("/")
        self.max_acu = int(os.environ.get("DEVIN_MAX_ACU", "2"))
        self.mode = os.environ.get("DEVIN_MODE", "").strip()
        self.v3 = self.key.startswith("cog_") or bool(self.key and self.org)
        self.v1 = bool(self.key) and not self.v3

    @property
    def configured(self):
        return bool(self.key)

    @property
    def label(self):
        return "v3" if self.v3 else ("v1" if self.v1 else "local")

    def _headers(self):
        return {"Authorization": "Bearer " + self.key, "Content-Type": "application/json"}

    async def ensure_org(self, c):
        if self.v3 and not self.org:
            r = await c.get(f"{self.base}/v3/self", headers=self._headers())
            r.raise_for_status()
            self.org = r.json()["org_id"]

    async def create(self, prompt, title, tags):
        async with httpx.AsyncClient(timeout=30) as c:
            await self.ensure_org(c)
            if self.v3:
                body = {"prompt": prompt, "title": title, "tags": tags,
                        "structured_output_schema": SCHEMA, "max_acu_limit": self.max_acu}
                if self.mode:
                    body["devin_mode"] = self.mode
                r = await c.post(f"{self.base}/v3/organizations/{self.org}/sessions",
                                 headers=self._headers(), json=body)
            else:
                r = await c.post(f"{self.base}/v1/sessions", headers=self._headers(),
                                 json={"prompt": prompt, "title": title, "tags": tags,
                                       "idempotent": False, "max_acu_limit": self.max_acu})
            r.raise_for_status()
            d = r.json()
            return d.get("session_id") or d.get("devin_id"), d.get("url")

    async def get(self, session_id):
        async with httpx.AsyncClient(timeout=30) as c:
            await self.ensure_org(c)
            if self.v3:
                r = await c.get(f"{self.base}/v3/organizations/{self.org}/sessions/{session_id}",
                                headers=self._headers())
            else:
                r = await c.get(f"{self.base}/v1/session/{session_id}", headers=self._headers())
            r.raise_for_status()
            d = r.json()
            status = d.get("status") or d.get("status_enum") or "running"
            return status, d.get("structured_output"), d.get("url")


# ---------------------------------------------------------------- prompt

def build_prompt(alert, worker, site, params, window, features):
    lines = []
    if window and window.get("samples"):
        s = window["samples"][::2]  # 25 Hz is plenty for the analyst and keeps the prompt small
        lines = ["%d,%.3f,%.3f,%.3f,%.1f,%.1f,%.1f" % tuple(x) for x in s]
    ctx = {
        "incident": {k: alert.get(k) for k in ("id", "type", "severity", "title", "message", "ts")},
        "timeline": alert.get("timeline", []),
        "device_detail": alert.get("detail", {}),
        "worker": {k: worker.get(k) for k in (
            "id", "trade", "zone", "acclimatized", "workload", "phase",
            "work_elapsed_min", "work_budget_min", "shift_exposure_min", "device_temp_c")} if worker else None,
        "site": {k: site.get(k) for k in ("temp_c", "rh", "wbgt_c", "category", "source")},
    }
    return f"""You are the on-call safety analyst for HeatGuard, a wrist wearable (M5Stack StickS3, BMI270 IMU sampled at 50 Hz; accel in g, gyro in deg/s) worn by outdoor construction workers in the UAE. The wearable already raised an alarm on-device. Your job is the second opinion: decide what actually happened, and tell us how to tune the on-device detector.

Work quantitatively. Load the CSV below in Python in your sandbox and compute at least: |a| over time, free-fall duration (|a| < 0.5 g), impact peak, post-event stillness (std of |a|, mean |gyro|), dominant gyro frequency (FFT on the highest-variance axis) and its RMS, and how these compare with the detector thresholds.

Incident and context:
```json
{json.dumps(ctx, indent=1, default=str)}
```

On-device detector parameters (these names are what `suggested_threshold_changes.parameter` must use):
```json
{json.dumps(params or {}, indent=1)}
```

Pre-computed features from the server on the full 50 Hz trace, for reference only (verify them):
```json
{json.dumps(features or {}, indent=1)}
```

IMU trace, CSV, decimated 2:1 from 50 Hz to 25 Hz to keep this prompt small, t_ms is device time; the alarm fired at t_ms = {window.get('event_t_ms') if window else 'n/a'}:
```csv
t_ms,ax,ay,az,gx,gy,gz
{chr(10).join(lines) if lines else '(no raw trace for this incident)'}
```

Deliverable: fill the structured output. `verdict` is one of {", ".join(VERDICTS)}. `evidence` are short quantitative bullet points. `recommended_actions` are what the site supervisor should do now, most urgent first. Propose `suggested_threshold_changes` only where the trace shows the detector fired too easily or nearly missed. Do not open pull requests, create repositories or contact anyone. Finish in a few minutes.
"""


# ---------------------------------------------------------------- local analysis

def features(window):
    s = (window or {}).get("samples") or []
    if len(s) < 20:
        return {}
    t0 = window.get("event_t_ms", s[len(s) // 2][0])
    a = [math.sqrt(x[1] ** 2 + x[2] ** 2 + x[3] ** 2) for x in s]
    g = [math.sqrt(x[4] ** 2 + x[5] ** 2 + x[6] ** 2) for x in s]
    dt = max(1, (s[-1][0] - s[0][0]) / max(1, len(s) - 1))
    # longest free-fall run
    ff, run = 0, 0
    for v in a:
        run = run + 1 if v < 0.5 else 0
        ff = max(ff, run)
    peak_i = max(range(len(a)), key=lambda i: a[i])
    post = [i for i, x in enumerate(s) if x[0] > s[peak_i][0] + 800]
    post = post[: int(2000 / dt)]
    if post:
        pa = [a[i] for i in post]
        m = sum(pa) / len(pa)
        post_std = math.sqrt(sum((v - m) ** 2 for v in pa) / len(pa))
        post_g = sum(g[i] for i in post) / len(post)
    else:
        post_std, post_g = None, None
    # rhythm on the most active gyro axis in the 3 s before the alarm
    pre = [x for x in s if t0 - 3000 <= x[0] <= t0]
    hz, rms = 0.0, 0.0
    if len(pre) > 20:
        best = max((4, 5, 6), key=lambda k: _var([x[k] for x in pre]))
        sig = [x[best] for x in pre]
        rms = math.sqrt(_var(sig))
        hz = _dominant_hz(sig, dt / 1000.0)
    return {
        "samples": len(s),
        "sample_ms": round(dt, 1),
        "max_accel_g": round(max(a), 2),
        "min_accel_g": round(min(a), 2),
        "freefall_ms": int(ff * dt),
        "post_impact_std_g": None if post_std is None else round(post_std, 3),
        "post_impact_gyro_dps": None if post_g is None else round(post_g, 1),
        "pre_alarm_dominant_hz": round(hz, 2),
        "pre_alarm_gyro_rms_dps": round(rms, 1),
    }


def _var(v):
    m = sum(v) / len(v)
    return sum((x - m) ** 2 for x in v) / len(v)


def _dominant_hz(sig, dt):
    n = len(sig)
    m = sum(sig) / n
    x = [v - m for v in sig]
    best_f, best_p = 0.0, 0.0
    for k in range(1, n // 2):
        re = im = 0.0
        for i, v in enumerate(x):
            ang = 2 * math.pi * k * i / n
            re += v * math.cos(ang)
            im -= v * math.sin(ang)
        p = re * re + im * im
        if p > best_p:
            best_p, best_f = p, k / (n * dt)
    return best_f


def local_verdict(alert, f, params):
    t = alert.get("type")
    tl = [e.get("type") for e in alert.get("timeline", [])]
    responded = any(x.endswith("_ok") or x.endswith("_cancel") for x in tl)
    noresp = any(x.endswith("_noresp") for x in tl)
    ev, act, changes = [], [], []
    p = params or {}
    if f:
        ev.append(f"Peak |a| {f['max_accel_g']} g, min |a| {f['min_accel_g']} g, free-fall {f['freefall_ms']} ms.")
        if f.get("post_impact_std_g") is not None:
            ev.append(f"After impact: |a| std {f['post_impact_std_g']} g, mean gyro {f['post_impact_gyro_dps']} °/s.")
        if f.get("pre_alarm_dominant_hz"):
            ev.append(f"Before alarm: dominant rhythm {f['pre_alarm_dominant_hz']} Hz at {f['pre_alarm_gyro_rms_dps']} °/s RMS.")
    if responded:
        ev.append("Worker pressed 'I'm OK' on the wearable.")
    if noresp:
        ev.append("Worker did not answer the on-device prompt.")

    if t in ("fall", "impact"):
        hard = f.get("max_accel_g", 0) >= 2.5
        ff = f.get("freefall_ms", 0)
        if responded and not hard:
            verdict, conf = "false_alarm", 0.7
            summary = "Short drop with a soft landing and the worker answered. Probably the device was knocked or dropped, not a body fall."
            act = ["Confirm with the worker by radio.", "No dispatch needed."]
            if p.get("IMPACT_G"):
                changes.append({"parameter": "IMPACT_G", "current": p["IMPACT_G"],
                                "proposed": round(max(p["IMPACT_G"], f.get("max_accel_g", 0) + 0.3), 1),
                                "rationale": "This impact was below what a body fall produces and the worker was fine."})
        elif noresp or (hard and ff >= 150):
            verdict, conf = "true_fall", 0.8 if noresp else 0.65
            summary = "Free-fall, hard impact and stillness afterwards" + (", and no response." if noresp else ".") + " Treat as a real fall."
            act = ["Send the nearest first-aider now.", "Do not move the worker if head or neck injury is possible.",
                   "Check for heat illness: move to shade, cool, give water if conscious."]
        else:
            verdict, conf = "needs_human_review", 0.5
            summary = "Impact pattern is ambiguous. A person should check."
            act = ["Radio the worker's crew lead to check in person."]
    elif t in ("tremor", "erratic"):
        hz = f.get("pre_alarm_dominant_hz", 0)
        if 3 <= hz <= 12 and not responded:
            verdict, conf = "tremor_or_seizure_like", 0.6
            summary = f"Sustained rhythmic shaking around {hz} Hz with no answer: consistent with a tremor or seizure."
            act = ["Send first-aider. Do not restrain; clear the area.", "Time the episode. Call 998 if it lasts over 5 minutes."]
        elif responded:
            verdict, conf = "false_alarm", 0.6
            summary = "Rhythmic motion but the worker answered. Likely a tool or repetitive task."
            act = ["Note the task. If recurring, tag the worker's trade for a higher shaking threshold."]
            if p.get("TREM_MIN_DPS"):
                changes.append({"parameter": "TREM_MIN_DPS", "current": p["TREM_MIN_DPS"],
                                "proposed": round(p["TREM_MIN_DPS"] * 1.25),
                                "rationale": "Task motion reached the current threshold; raise it for this trade."})
        else:
            verdict, conf = "erratic_movement", 0.5
            summary = "High-energy, irregular movement. Could be distress or vigorous work."
            act = ["Crew lead to check in person."]
    elif t in ("heat_limit", "heat_critical", "unwell", "rest_violation"):
        verdict, conf = "heat_illness_risk", 0.6
        summary = "Heat exposure and symptoms point to heat strain."
        act = ["Move to shade and start active cooling.", "Water plus electrolytes.",
               "Do not return to work this cycle. Escalate if confused or not sweating."]
    else:
        verdict, conf = "needs_human_review", 0.4
        summary = "Not enough signal for an automatic call."
        act = ["Check in person."]
    return {"verdict": verdict, "confidence": conf, "summary": summary, "evidence": ev,
            "recommended_actions": act, "suggested_threshold_changes": changes}
