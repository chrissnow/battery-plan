"""Weather-dependent load: learn how house load rises in cold (and optionally hot) weather.

Model, per hour of the day h (local):

    load = a_h + b * max(0, Tb - T) + c * max(0, T - Tc)

- a_h: a baseline for each hour, so each home's own timings (setback, boost,
  off-peak hot water) are kept without assuming any;
- b, Tb: heating sensitivity (kW per degC) below a heating balance point;
- c, Tc: optional cooling sensitivity above a cooling balance point;
- T: outdoor temperature smoothed over the previous hours (buildings respond
  slowly), from an outdoor sensor or the weather entity's history.

b and c are shared across the day and fitted over the last few weeks (hour
fixed effects, balance points by grid search), so it stays stable on small or
noisy data. With dedicated heating sensors each hour's term is scaled by w_h,
that hour's share of the sensors' recent energy (mean 1 over the day; see
``hour_weights``): a heat pump that only runs overnight puts its extra cold-weather
load overnight, not spread over hours it is off. The fit is only used when it is clearly supported by the data;
otherwise the plan falls back to the plain load schedule.

Prediction: for each future half hour the plan's 7-day load schedule is moved
by weather(T_forecast) - (typical weather load at that hour over the same 7
days). The "weather load" shown to the user is weather(T_forecast).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
import math
from typing import Any

SMOOTH_HOURS = 6.0  # exponential smoothing of outdoor temperature
HEAT_POINTS = [x / 2 for x in range(20, 37)]  # 10.0 .. 18.0 degC
COOL_POINTS = [x / 2 for x in range(36, 53)]  # 18.0 .. 26.0 degC
MIN_DAYS = 7
MIN_ACTIVE_HOURS = 48  # hours with the temperature past the balance point
MIN_GAIN = 0.03  # the weather terms must explain >= 3% of the within-hour variance
MAX_SENS = 3.0  # kW per degC: anything bigger is a data problem
WEIGHT_FLAT = 0.1  # share of a flat profile mixed into the hour weights (no hour is ever zero)


@dataclass
class Row:
    """One hour of history."""

    t: datetime  # hour start, UTC
    hod: int  # local hour of day
    day: str  # local date
    temp: float  # smoothed outdoor temperature
    raw_temp: float
    load: float  # kW, mean over the hour


@dataclass
class WeatherModel:
    status: str = "off"  # active | not_enough_data | off
    reason: str = ""
    heat_kw_per_c: float | None = None
    heat_below_c: float | None = None
    cool_kw_per_c: float | None = None
    cool_above_c: float | None = None
    gain: float | None = None  # share of within-hour variance explained by the weather terms
    days: int = 0
    hours: int = 0
    daily: list[dict[str, Any]] = field(default_factory=list)  # for the scatter chart
    line_base_kwh: float | None = None  # daily line: base + line_hours * weather_kw(T)
    line_hours: int = 24
    typical_kw: list[float] = field(default_factory=lambda: [0.0] * 24)  # weather load by hour, recent days
    weights: list[float] | None = None  # per local hour (mean 1); None = the same in every hour

    @property
    def active(self) -> bool:
        return self.status == "active"

    def _w(self, hod: int | None) -> float:
        return self.weights[hod] if self.weights and hod is not None else 1.0

    def heat_kw(self, temp: float, hod: int | None = None) -> float:
        if not self.active or self.heat_kw_per_c is None or self.heat_below_c is None:
            return 0.0
        return self.heat_kw_per_c * max(0.0, self.heat_below_c - temp) * self._w(hod)

    def cool_kw(self, temp: float, hod: int | None = None) -> float:
        if not self.active or self.cool_kw_per_c is None or self.cool_above_c is None:
            return 0.0
        return self.cool_kw_per_c * max(0.0, temp - self.cool_above_c) * self._w(hod)

    def as_dict(self) -> dict[str, Any]:
        r = lambda v, n=3: None if v is None else round(v, n)  # noqa: E731
        return {
            "status": self.status,
            "reason": self.reason,
            "heating_kw_per_c": r(self.heat_kw_per_c),
            "heating_below_c": self.heat_below_c,
            "cooling_kw_per_c": r(self.cool_kw_per_c),
            "cooling_above_c": self.cool_above_c,
            "fit_gain": r(self.gain),
            "days": self.days,
            "hours": self.hours,
            "line_base_kwh": r(self.line_base_kwh, 2),
            "line_hours": self.line_hours,
            "hour_weights": [round(w, 2) for w in self.weights] if self.weights else None,
            "daily": self.daily,
        }


def smooth(temps: Iterable[tuple[datetime, float | None]], start: float | None = None) -> list[tuple[datetime, float | None]]:
    """Exponentially smoothed hourly temperatures (gaps of more than 3 h restart it)."""
    out: list[tuple[datetime, float | None]] = []
    s, last = start, None
    k = 1 - math.exp(-1 / SMOOTH_HOURS)
    for t, v in temps:
        if v is None:
            out.append((t, s))
            continue
        if s is None or (last is not None and (t - last) > timedelta(hours=3)):
            s = v
        else:
            s = s + k * (v - s)
        last = t
        out.append((t, s))
    return out


def hour_weights(profile: list[float | None]) -> list[float] | None:
    """Per local hour weights (mean 1) from a half-hourly (48) or hourly (24) heating profile.

    Each hour's share of the profile's energy, mixed with WEIGHT_FLAT of a flat
    profile. None when the profile is missing or holds no energy.
    """
    if len(profile) not in (24, 48) or any(v is None for v in profile):
        return None
    per = 2 if len(profile) == 48 else 1
    hourly = [sum(max(0.0, profile[h * per + k]) for k in range(per)) / per for h in range(24)]
    mean = sum(hourly) / 24
    if mean < 0.01:
        return None
    return [(1 - WEIGHT_FLAT) * v / mean + WEIGHT_FLAT for v in hourly]


def _fit_fixed(
    rows: list[Row], tb: float | None, tc: float | None, weights: list[float] | None = None
) -> tuple[float, float, float] | None:
    """Least squares for b (and c) with per-hour fixed effects. Returns (b, c, sse)."""
    groups: dict[int, list[Row]] = {}
    for r in rows:
        groups.setdefault(r.hod, []).append(r)
    sxx = sxy = syy = scc = scy = sxc = 0.0
    for hod, g in groups.items():
        n = len(g)
        w = weights[hod] if weights else 1.0
        x = [w * max(0.0, tb - r.temp) if tb is not None else 0.0 for r in g]
        c = [w * max(0.0, r.temp - tc) if tc is not None else 0.0 for r in g]
        y = [r.load for r in g]
        mx, mc, my = sum(x) / n, sum(c) / n, sum(y) / n
        for xi, ci, yi in zip(x, c, y):
            dx, dc, dy = xi - mx, ci - mc, yi - my
            sxx += dx * dx
            scc += dc * dc
            sxc += dx * dc
            sxy += dx * dy
            scy += dc * dy
            syy += dy * dy
    if tc is None:
        if sxx < 1e-6:
            return None
        b = sxy / sxx
        return b, 0.0, syy - b * sxy
    if tb is None:
        if scc < 1e-6:
            return None
        c = scy / scc
        return 0.0, c, syy - c * scy
    det = sxx * scc - sxc * sxc
    if abs(det) < 1e-9:
        return None
    b = (sxy * scc - scy * sxc) / det
    c = (scy * sxx - sxy * sxc) / det
    return b, c, syy - b * sxy - c * scy


def _within_ss(rows: list[Row]) -> float:
    groups: dict[int, list[float]] = {}
    for r in rows:
        groups.setdefault(r.hod, []).append(r.load)
    tot = 0.0
    for ys in groups.values():
        m = sum(ys) / len(ys)
        tot += sum((v - m) ** 2 for v in ys)
    return tot


def fit(
    rows: list[Row], cooling: bool, recent_days: int, now: datetime, weights: list[float] | None = None
) -> WeatherModel:
    """Fit the model; ``rows`` should already exclude dispatches, sessions etc.

    ``weights`` (24, mean 1) scale each hour's weather terms (see ``hour_weights``).
    """
    m = WeatherModel(status="not_enough_data", weights=weights)
    rows = [r for r in rows if r.temp is not None and r.load is not None]
    days = sorted({r.day for r in rows})
    m.days, m.hours = len(days), len(rows)
    if len(days) < MIN_DAYS:
        m.reason = f"needs {MIN_DAYS - len(days)} more days of history"
        return m
    sst = _within_ss(rows)
    if sst <= 0:
        m.reason = "load doesn't vary"
        return m

    best: tuple[float, float | None, float | None, float, float] | None = None  # sse, tb, tc, b, c
    for tb in HEAT_POINTS:
        if sum(1 for r in rows if r.temp < tb) < MIN_ACTIVE_HOURS:
            continue
        res = _fit_fixed(rows, tb, None, weights)
        if res and res[0] > 0 and (best is None or res[2] < best[0]):
            best = (res[2], tb, None, res[0], 0.0)
    if cooling:
        for tc in COOL_POINTS:
            if sum(1 for r in rows if r.temp > tc) < MIN_ACTIVE_HOURS:
                continue
            tb = best[1] if best else None
            res = _fit_fixed(rows, tb, tc, weights)
            if res and res[1] > 0 and (tb is None or res[0] > 0) and (best is None or res[2] < best[0]):
                best = (res[2], tb, tc, res[0], res[1])
    if best is None:
        m.reason = "not enough cold or hot weather in the history yet"
        return m
    sse, tb, tc, b, c = best
    gain = 1 - sse / sst
    m.gain = gain
    if gain < MIN_GAIN:
        m.reason = f"weather explains too little of the load ({gain:.0%})"
        return m
    if b > MAX_SENS or c > MAX_SENS:
        m.reason = "sensitivity implausibly large: check the sensors"
        return m

    m.status, m.reason = "active", ""
    m.heat_kw_per_c, m.heat_below_c = (b, tb) if tb is not None and b > 0 else (None, None)
    m.cool_kw_per_c, m.cool_above_c = (c, tc) if tc is not None and c > 0 else (None, None)

    # typical weather load by hour over the recent days (the load schedule's window)
    since = now - timedelta(days=recent_days)
    acc: list[list[float]] = [[] for _ in range(24)]
    for r in rows:
        if r.t >= since:
            acc[r.hod].append(m.heat_kw(r.temp, r.hod) + m.cool_kw(r.temp, r.hod))
    m.typical_kw = [sum(v) / len(v) if v else 0.0 for v in acc]

    # daily points for the scatter chart, and the line through them
    per_day: dict[str, list[Row]] = {}
    for r in rows:
        per_day.setdefault(r.day, []).append(r)
    pts = []
    full = sorted(len(v) for v in per_day.values())[len(per_day) // 2]  # typical hours per day
    for d, rs in sorted(per_day.items()):
        if len(rs) < 0.8 * full:
            continue
        avg_t = sum(r.raw_temp for r in rs) / len(rs)
        kwh = sum(r.load for r in rs) * full / len(rs)  # kWh over the hours this model sees
        pts.append({"d": d, "t": round(avg_t, 1), "kwh": round(kwh, 1)})
    m.daily = pts[-28:]
    m.line_hours = full
    if pts:
        def line_w(t: float) -> float:
            return full * ((b * max(0.0, tb - t) if tb is not None else 0.0) + (c * max(0.0, t - tc) if tc is not None else 0.0))
        m.line_base_kwh = sum(p["kwh"] - line_w(p["t"]) for p in pts) / len(pts)
    return m


def interp(series: list[tuple[datetime, float]], t: datetime) -> float | None:
    """Linear interpolation in a sorted (time, value) series; None outside it."""
    if not series or t < series[0][0] or t > series[-1][0]:
        return None
    lo, hi = 0, len(series) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if series[mid][0] <= t:
            lo = mid
        else:
            hi = mid
    (t0, v0), (t1, v1) = series[lo], series[hi]
    if t1 == t0:
        return v0
    return v0 + (v1 - v0) * (t - t0).total_seconds() / (t1 - t0).total_seconds()


# ---------- heat pumps: learn heat, forecast electricity through the COP ----------
#
# A heat pump's electricity is the heat the house needs divided by its COP, and the COP falls as
# it gets colder: the weather curve raises the supply temperature and the coil runs colder. The
# heat loss is the part that is linear in the outdoor temperature, so with a supply temperature
# sensor the model above is fitted on heat (electricity x COP) and the forecast is divided by the
# COP forecast for each hour. The COP is the data-sheet form: a share (``ratio``, ~0.45-0.5) of
# the ideal (Carnot) COP between the condensing (supply + 2 K) and evaporating temperatures; the coil
# is taken as COIL_GAP below the outdoor temperature (a fixed gap: the learned ratio absorbs the rest).
# Several units on one circuit are one system: their electricity (and heat) are summed.

COND_OFFSET = 2.0  # K: condensing temperature above the supply water
COIL_GAP = 4.0  # K: evaporating (coil) temperature below the outdoor temperature
RUN_KW = 0.3  # an hour counts as running at this mean electrical input or more
MIN_RUN_HOURS = 24


def carnot_cop(ratio: float, supply: float, evap: float) -> float:
    """``ratio`` x the ideal heating COP, condensing at ``supply`` + COND_OFFSET, evaporating at ``evap``."""
    tc = supply + COND_OFFSET + 273.15
    return ratio * tc / max(5.0, tc - (evap + 273.15))


@dataclass
class HeatPumpRow:
    """One hour of heat pump history."""

    t: datetime  # hour start, UTC
    hod: int  # local hour of day
    temp: float  # outdoor temperature (raw)
    elec: float  # kW in, all units
    supply: float | None = None  # supply temperature (setpoint or flow), degC
    heat: float | None = None  # kW out, all units, where the user's own sensors give it


@dataclass
class CopModel:
    """Supply temperature by weather curve and COP ratio, learned from history."""

    ratio: float
    ratio_learned: bool
    a: float  # supply = a + b * outdoor + offset[hour], within [lo, hi]
    b: float
    offset: list[float]
    lo: float
    hi: float
    hours: int  # running hours learned from
    typical_cop: list[float] = field(default_factory=lambda: [0.0] * 24)  # recent days, by hour
    typical_heat: list[float] = field(default_factory=lambda: [0.0] * 24)  # kW heat, recent days, by hour

    def supply(self, temp: float, hod: int) -> float:
        return min(self.hi, max(self.lo, self.a + self.b * temp + self.offset[hod]))

    def cop(self, temp: float, hod: int) -> float:
        return carnot_cop(self.ratio, self.supply(temp, hod), temp - COIL_GAP)

    def heat(self, row: HeatPumpRow) -> float:
        """The hour's heat output: the user's own figure, else electricity x COP at what it ran at."""
        if row.heat is not None:
            return row.heat
        if row.supply is not None:
            return row.elec * carnot_cop(self.ratio, row.supply, row.temp - COIL_GAP)
        return row.elec * self.cop(row.temp, row.hod)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cop_ratio": round(self.ratio, 3),
            "cop_ratio_learned": self.ratio_learned,
            "supply_at_0c": round(self.a, 1),
            "supply_per_c": round(self.b, 2),
            "supply_range": [round(self.lo, 1), round(self.hi, 1)],
            "running_hours": self.hours,
            "cop_at": {str(t): round(self.cop(t, 12), 2) for t in (-5, 0, 5, 10)},
        }


def fit_cop(rows: list[HeatPumpRow], ratio: float) -> CopModel | None:
    """Learn the weather curve (supply against outdoor, plus an offset per hour of day) and, where
    the user's sensors give heat, the COP ratio their figures imply."""
    run = [r for r in rows if r.elec >= RUN_KW and r.supply is not None]
    if len(run) < MIN_RUN_HOURS:
        return None
    n = len(run)
    mx = sum(r.temp for r in run) / n
    my = sum(r.supply for r in run) / n
    sxx = sum((r.temp - mx) ** 2 for r in run)
    b = sum((r.temp - mx) * (r.supply - my) for r in run) / sxx if sxx > 1e-6 else 0.0
    b = min(0.0, b)  # supply never rises with the outdoor temperature
    a = my - b * mx
    res: list[list[float]] = [[] for _ in range(24)]
    for r in run:
        res[r.hod].append(r.supply - a - b * r.temp)
    # an hour's own offset, shrunk towards none when it has few running hours
    offset = [sum(v) / (len(v) + 3) if v else 0.0 for v in res]
    m = CopModel(ratio=ratio, ratio_learned=False, a=a, b=b, offset=offset,
                 lo=min(r.supply for r in run), hi=max(r.supply for r in run), hours=n)
    known = [r for r in run if r.heat is not None]
    if len(known) >= MIN_RUN_HOURS:
        ideal = sum(r.elec * carnot_cop(1.0, r.supply, r.temp - COIL_GAP) for r in known)
        if ideal > 0:
            m.ratio = min(0.8, max(0.2, sum(r.heat for r in known) / ideal))
            m.ratio_learned = True
    return m


def set_typical(cop: CopModel, rows: list[HeatPumpRow], since: datetime) -> None:
    """Typical heat and COP by hour over the recent days (the load profile's days)."""
    heat: list[list[float]] = [[] for _ in range(24)]
    cops: list[list[float]] = [[] for _ in range(24)]
    for r in rows:
        if r.t >= since:
            heat[r.hod].append(cop.heat(r))
            cops[r.hod].append(cop.cop(r.temp, r.hod))
    cop.typical_heat = [sum(v) / len(v) if v else 0.0 for v in heat]
    cop.typical_cop = [sum(v) / len(v) if v else cop.cop(5.0, h) for h, v in enumerate(cops)]


def slot_adjustments(
    model: WeatherModel,
    forecast: list[tuple[datetime, float]],
    last_smoothed: float | None,
    slots: list[datetime],
    tz: tzinfo,
    cop: CopModel | None = None,
) -> dict[int, tuple[float, float, float, float]]:
    """Per half-hour slot start (epoch): (load adjustment kW, heating kW, cooling kW, forecast temp).

    ``forecast`` is hourly (UTC time, degC); it's smoothed on from the last
    observed smoothed temperature so it matches what the model learned from.
    With a heat pump model (``model`` fitted on heat): the slot's heat (typical
    heat at that hour moved by the weather) over its forecast COP, less the
    typical heat over the typical COP; heating/cooling kW are then electricity.
    """
    out: dict[int, tuple[float, float, float, float]] = {}
    if not model.active or not forecast:
        return out
    raw = sorted(forecast)
    sm = [(t, v) for t, v in smooth(raw, last_smoothed) if v is not None]
    for s in slots:
        mid = s + timedelta(minutes=15)
        ts = interp(sm, mid)
        tr = interp(raw, mid)
        if ts is None:
            continue
        hod = mid.astimezone(tz).hour
        h, c = model.heat_kw(ts, hod), model.cool_kw(ts, hod)
        temp = tr if tr is not None else ts
        if cop is not None:
            f = cop.cop(temp, hod)
            heat = max(0.0, cop.typical_heat[hod] + h + c - model.typical_kw[hod])
            typical = cop.typical_heat[hod] / cop.typical_cop[hod]
            out[int(s.timestamp())] = (heat / f - typical, h / f, c / f, temp)
            continue
        out[int(s.timestamp())] = (h + c - model.typical_kw[hod], h, c, temp)
    return out
