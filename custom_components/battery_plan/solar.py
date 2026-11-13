"""Solar forecast maths for the planner: pure Python (no Home Assistant imports).

- ``slot_kw``: an energy-dashboard solar forecast (Wh per hour) as mean kW per half hour.
- ``calibrate``: from past days' forecast and actual kWh, the forecast's bias (median of
  actual / forecast) and a dull day (a low percentile of the ratio, after the bias), which the
  planner weighs against the expected day when it sets the overnight target.
- ``nowcast``: today's actual so far against the forecast so far, applied to the rest of today.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import math
from statistics import median

MIN_DAYS = 5  # days of history before the bias and the dull day are learned
MIN_DAY_KWH = 0.5  # days forecast below this tell nothing about the ratio
BIAS_RANGE = (0.5, 1.5)
DULL_RANGE = (0.2, 1.0)
DULL_PCT = 0.2  # the dull day: this percentile of actual / (forecast x bias)
DEFAULT_DULL = 0.7
NOWCAST_RANGE = (0.3, 1.5)


def _clamp(v: float, lo_hi: tuple[float, float]) -> float:
    return min(lo_hi[1], max(lo_hi[0], v))


def slot_kw(wh_hours: dict[datetime, float], scale: float = 1.0) -> dict[int, float]:
    """Hourly forecast (period start -> Wh) as mean kW per half-hour slot start (epoch).

    An hour's Wh is its mean kW (/1000); both of its half hours get that. A period starting
    off the hour (some services start at sunrise) goes to the hour it falls in."""
    out: dict[int, float] = {}
    for t, wh in wh_hours.items():
        if wh is None or not math.isfinite(wh):
            continue
        h = int(t.timestamp()) // 3600 * 3600
        kw = max(0.0, float(wh)) / 1000 * scale
        for s in (h, h + 1800):
            out[s] = out.get(s, 0.0) + kw
    return out


def daily_kwh(slots: dict[int, float], tz: tzinfo) -> dict[str, float]:
    """kWh per local day of a half-hourly kW forecast."""
    out: dict[str, float] = {}
    for s, kw in slots.items():
        d = datetime.fromtimestamp(s, timezone.utc).astimezone(tz).date().isoformat()
        out[d] = out.get(d, 0.0) + kw * 0.5
    return out


def kwh_between(slots: dict[int, float], start: datetime, end: datetime) -> float:
    """kWh of a half-hourly kW forecast between two times (slots counted pro rata)."""
    a, b = start.timestamp(), end.timestamp()
    tot = 0.0
    for s, kw in slots.items():
        ov = min(b, s + 1800) - max(a, s)
        if ov > 0:
            tot += kw * ov / 3600
    return tot


def calibrate(history: dict[str, tuple[float, float | None]]) -> tuple[float, float, int]:
    """(bias, dull, days used) from {local day: (forecast kWh, actual kWh)}.

    Bias: the median of actual / forecast. Dull: the ``DULL_PCT`` percentile of the ratio once
    the bias is taken out, i.e. how much less than the corrected forecast a bad day brings."""
    ratios = sorted(a / f for f, a in history.values() if a is not None and f >= MIN_DAY_KWH and a >= 0)
    if len(ratios) < MIN_DAYS:
        return 1.0, DEFAULT_DULL, len(ratios)
    bias = _clamp(median(ratios), BIAS_RANGE)
    k = DULL_PCT * (len(ratios) - 1)
    lo, hi = math.floor(k), math.ceil(k)
    pct = ratios[lo] + (ratios[hi] - ratios[lo]) * (k - lo)
    return bias, _clamp(pct / bias, DULL_RANGE), len(ratios)


def nowcast(fc_so_far: float, actual_so_far: float, fc_today: float) -> float:
    """Factor for the rest of today: today's actual / forecast so far, weighted by how much of
    the day's forecast has passed (early in the day a few cloudy minutes mean little)."""
    if fc_so_far < MIN_DAY_KWH * 0.5 or fc_today <= 0:
        return 1.0
    ratio = _clamp(actual_so_far / fc_so_far, NOWCAST_RANGE)
    w = min(1.0, fc_so_far / fc_today)
    return 1.0 + w * (ratio - 1.0)


def apply(slots: dict[int, float], factor: float, today_factor: float, now: datetime, tz: tzinfo) -> dict[int, float]:
    """The forecast with the bias (all days) and the nowcast (the rest of today) applied."""
    end_today = (now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
    e = end_today.timestamp()
    return {s: kw * factor * (today_factor if s < e else 1.0) for s, kw in slots.items()}
