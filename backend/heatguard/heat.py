"""Heat-stress rules: WBGT estimate, risk category, work/rest policy, UAE midday break."""
import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Dubai")

# ACGIH TLV heat-stress screening criteria, WBGT in °C.
# Rows are work/rest allocations per hour, taken at the conservative end of each
# band (75-100% -> 45 min work, 50-75% -> 30, 25-50% -> 15, 0-25% -> 10).
# None = that workload is not allowed at that allocation.
WORK_REST = [(45, 15), (30, 30), (15, 45), (10, 50)]
TLV = {  # acclimatized
    "light":    [31.0, 31.0, 32.0, 32.5],
    "moderate": [28.0, 29.0, 30.0, 31.5],
    "heavy":    [None, 27.5, 29.0, 30.5],
}
ACTION_LIMIT = {  # unacclimatized (first ~2 weeks on site)
    "light":    [28.0, 28.5, 29.5, 30.0],
    "moderate": [25.0, 26.0, 27.0, 29.0],
    "heavy":    [None, 24.0, 25.5, 28.0],
}
# Below every limit: normal hour with a hydration break.
EASY = (50, 10)

CATEGORIES = [  # (upper bound, label, colour)
    (27.0, "Low", "#22c55e"),
    (29.4, "Moderate", "#eab308"),
    (31.1, "High", "#f97316"),
    (32.2, "Very high", "#ef4444"),
    (99.0, "Extreme", "#b91c1c"),
]

WORKLOADS = ("light", "moderate", "heavy")


def wet_bulb_c(temp_c, rh):
    """Psychrometric wet-bulb temperature, Stull (2011). Valid 5-99 % RH, -20..50 °C."""
    t, r = temp_c, max(5.0, min(99.0, rh))
    return (t * math.atan(0.151977 * math.sqrt(r + 8.313659))
            + math.atan(t + r) - math.atan(r - 1.676331)
            + 0.00391838 * r ** 1.5 * math.atan(0.023101 * r) - 4.686035)


def wbgt_estimate(temp_c, rh, solar_wm2=0.0, wind_ms=1.0):
    """Outdoor WBGT = 0.7 Tnwb + 0.2 Tg + 0.1 Ta, with Tnwb and Tg estimated.

    Natural wet-bulb sits a little above the psychrometric wet-bulb, more in sun;
    the black-globe temperature climbs with solar load and falls with wind.
    Good enough for screening. A real site would feed a WBGT meter in through
    the ingest API instead.
    """
    s = min(1.0, max(0.0, solar_wm2) / 900.0)
    cool = 1.0 + 0.3 * max(0.0, wind_ms - 1.0)
    tnwb = wet_bulb_c(temp_c, rh) + 0.5 + 1.0 * s
    tg = temp_c + 1.0 + 12.0 * s / cool
    return round(0.7 * tnwb + 0.2 * tg + 0.1 * temp_c, 1)


def category(wbgt):
    for level, (hi, label, color) in enumerate(CATEGORIES):
        if wbgt < hi:
            return {"level": level, "label": label, "color": color}
    return {"level": 4, "label": "Extreme", "color": CATEGORIES[-1][2]}


def allocation(wbgt, workload, acclimatized=True):
    """Return (work_min, rest_min) per hour. work 0 = stop work."""
    table = TLV if acclimatized else ACTION_LIMIT
    limits = table[workload]
    # Well under the continuous-work limit: normal hour with a hydration break.
    if limits[0] is not None and wbgt < limits[0] - 1.0:
        return EASY
    for (work, rest), limit in zip(WORK_REST, limits):
        if limit is not None and wbgt <= limit:
            return work, rest
    return 0, 60


def policy(wbgt):
    return {
        key: {wl: dict(zip(("work", "rest"), allocation(wbgt, wl, acc))) for wl in WORKLOADS}
        for key, acc in (("acclimatized", True), ("unacclimatized", False))
    }


def midday_break(now=None):
    """UAE MoHRE midday break: no outdoor work in direct sun 12:30-15:00, 15 Jun - 15 Sep."""
    now = now or datetime.now(TZ)
    y = now.year
    in_season = date(y, 6, 15) <= now.date() <= date(y, 9, 15)
    mins = now.hour * 60 + now.minute
    active = in_season and 12 * 60 + 30 <= mins < 15 * 60
    return {"active": active, "in_season": in_season,
            "window": "12:30–15:00", "season": "15 Jun – 15 Sep"}


def workload_from_activity(act):
    """Map the wearable's activity index to a metabolic workload class.

    Anything below 'light' still counts as light: a worker standing in the sun
    is still exposed.
    """
    if act < 0.08:
        return "light"
    if act < 0.2:
        return "moderate"
    return "heavy"
