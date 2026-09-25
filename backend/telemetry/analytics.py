"""Vectorised statistics over the last N samples of a device."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np


def _iso(epoch_s: float) -> str:
    return datetime.fromtimestamp(float(epoch_s), tz=timezone.utc).isoformat()


def _f(x) -> float:
    return round(float(x), 6)


def _window(ts: np.ndarray) -> dict:
    duration = float(ts[-1] - ts[0]) if len(ts) > 1 else 0.0
    return {
        "from": _iso(ts[0]),
        "to": _iso(ts[-1]),
        "duration_s": round(duration, 6),
        "sample_rate_hz": round((len(ts) - 1) / duration, 3) if duration > 0 else None,
    }


def _series_stats(x: np.ndarray) -> dict:
    p5, p50, p95 = np.percentile(x, [5, 50, 95])
    return {
        "mean": _f(x.mean()),
        "std": _f(x.std()),
        "min": _f(x.min()),
        "max": _f(x.max()),
        "median": _f(p50),
        "p05": _f(p5),
        "p95": _f(p95),
        "rms": _f(np.sqrt(np.mean(np.square(x, dtype=np.float64)))),
    }


def _slope_per_min(ts: np.ndarray, x: np.ndarray) -> float | None:
    """Least-squares slope of x over time, in units per minute."""
    if len(ts) < 2 or ts[-1] == ts[0]:
        return None
    t = (ts - ts[0]) / 60.0
    t_c = t - t.mean()
    denom = float(np.dot(t_c, t_c))
    if denom == 0:
        return None
    return _f(np.dot(t_c, x.astype(np.float64) - x.mean()) / denom)


def temperature_analytics(ts: np.ndarray, vals: np.ndarray) -> dict:
    """`ts` epoch seconds (chronological), `vals` shape (n, 1)."""
    x = vals[:, 0].astype(np.float64)
    n = len(x)
    stats = _series_stats(x)
    # Exponentially weighted mean gives a smoothed "current" temperature
    weights = np.exp(np.linspace(-3.0, 0.0, n))
    stats["ewma"] = _f(np.average(x, weights=weights))
    return {
        "count": n,
        "window": _window(ts),
        "latest": {"ts": _iso(ts[-1]), "value": _f(x[-1])},
        "stats": stats,
        "trend": {
            "slope_per_min": _slope_per_min(ts, x),
            "delta": _f(x[-1] - x[0]),
        },
        "anomalies": _anomalies(ts, x),
    }


def _anomalies(ts: np.ndarray, x: np.ndarray, z_threshold: float = 3.0) -> dict:
    std = x.std()
    if std == 0 or len(x) < 3:
        return {"count": 0, "indices": [], "timestamps": []}
    z = np.abs((x - x.mean()) / std)
    idx = np.flatnonzero(z > z_threshold)
    idx = idx[-20:]  # cap payload size
    return {
        "count": int((z > z_threshold).sum()),
        "z_threshold": z_threshold,
        "timestamps": [_iso(t) for t in ts[idx]],
        "values": [_f(v) for v in x[idx]],
    }


def accel_analytics(ts: np.ndarray, vals: np.ndarray) -> dict:
    """`vals` shape (n, 3) acceleration in g on x/y/z.

    The mean vector over the window estimates gravity (hence orientation); the deviation
    of the magnitude from its mean is the dynamic (vibration/shock) component.
    """
    v = vals.astype(np.float64)
    n = len(v)
    mag = np.linalg.norm(v, axis=1)
    gravity = v.mean(axis=0)
    g_norm = float(np.linalg.norm(gravity))
    gx, gy, gz = gravity
    dominant = int(np.abs(gravity).argmax())
    dyn = mag - mag.mean()
    peak_i = int(np.abs(dyn).argmax())

    return {
        "count": n,
        "window": _window(ts),
        "latest": {
            "ts": _iso(ts[-1]),
            "x": _f(v[-1, 0]),
            "y": _f(v[-1, 1]),
            "z": _f(v[-1, 2]),
            "magnitude": _f(mag[-1]),
        },
        "axes": {
            "x": _series_stats(v[:, 0]),
            "y": _series_stats(v[:, 1]),
            "z": _series_stats(v[:, 2]),
        },
        "magnitude": _series_stats(mag),
        "gravity": {
            "x": _f(gx),
            "y": _f(gy),
            "z": _f(gz),
            "magnitude": _f(g_norm),
            "pitch_deg": _f(np.degrees(np.arctan2(-gx, np.hypot(gy, gz)))),
            "roll_deg": _f(np.degrees(np.arctan2(gy, gz))),
            "dominant_axis": ("-" if gravity[dominant] < 0 else "+") + "xyz"[dominant],
        },
        "vibration": {
            "rms_g": _f(np.sqrt(np.mean(np.square(dyn)))),
            "peak_g": _f(np.abs(dyn[peak_i])),
            "peak_ts": _iso(ts[peak_i]),
        },
    }


def gyro_analytics(ts: np.ndarray, vals: np.ndarray, motion_threshold: float = 0.05) -> dict:
    """`vals` shape (n, 3) angular velocity on x/y/z."""
    v = vals.astype(np.float64)
    n = len(v)
    mag = np.linalg.norm(v, axis=1)
    peak_i = int(mag.argmax())
    duration = float(ts[-1] - ts[0]) if n > 1 else 0.0

    # Integrated angle (rad or deg depending on input units) over the window, per axis.
    if n > 1:
        dt = np.diff(ts)
        rotation = ((v[1:] + v[:-1]) / 2 * dt[:, None]).sum(axis=0)
    else:
        rotation = np.zeros(3)

    return {
        "count": n,
        "window": _window(ts),
        "latest": {
            "ts": _iso(ts[-1]),
            "x": _f(v[-1, 0]),
            "y": _f(v[-1, 1]),
            "z": _f(v[-1, 2]),
            "magnitude": _f(mag[-1]),
        },
        "axes": {
            "x": _series_stats(v[:, 0]),
            "y": _series_stats(v[:, 1]),
            "z": _series_stats(v[:, 2]),
        },
        "magnitude": _series_stats(mag),
        "peak": {"ts": _iso(ts[peak_i]), "magnitude": _f(mag[peak_i])},
        "rotation": {"x": _f(rotation[0]), "y": _f(rotation[1]), "z": _f(rotation[2])},
        "motion": {
            "threshold": motion_threshold,
            "active_fraction": _f((mag > motion_threshold).mean()),
            "active_seconds": round(float((mag > motion_threshold).mean() * duration), 3),
            "is_moving": bool(mag[-min(n, 10) :].mean() > motion_threshold),
        },
    }
