"""Battery charge/export planner.

Pure Python (no Home Assistant imports) so it can be unit-tested on its own.

Model, per half-hour slot over the horizon:
- Inside the daily charge window the battery grid-charges towards target_soc,
  either at full power or spread so it finishes ``finish_early`` minutes before
  the window ends (like a DVCC charge-current spreader). The house runs from grid.
- Inside an Intelligent Octopus dispatch (outside the window) the battery
  grid-charges at full power, if ``dispatch_charges_battery``.
- Inside a free-electricity (power-up) session the battery charges at full power.
- Otherwise the house runs from the battery. Before each charge window the
  battery keeps enough for the load plus ``reserve_soc``; anything above that is
  exported at ``max_export_kw`` in the best-priced slots paying at least
  ``min_export_price``. Saving sessions export at ``saving_export_kw``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
import math

HALF = timedelta(minutes=30)

MODE_SELF = "self"
MODE_CHARGE = "charge"
MODE_HOLD = "hold"
MODE_IOG = "iog"
MODE_EXPORT = "export"
MODE_SAVING = "saving"
MODE_GRID = "grid"
MODES = [MODE_SELF, MODE_CHARGE, MODE_HOLD, MODE_IOG, MODE_EXPORT, MODE_SAVING, MODE_GRID]


@dataclass
class PlannerConfig:
    """Static settings for the planner."""

    capacity_kwh: float
    target_soc: float = 100.0
    reserve_soc: float = 20.0
    min_soc: float = 5.0
    charge_start: int = 23 * 3600 + 1800  # seconds after local midnight
    charge_duration: int = 6 * 3600  # seconds
    charge_mode: str = "spread"  # "spread" or "max"
    max_charge_kw: float = 9.5
    min_charge_kw: float = 0.5
    makeup_kw: float = 0.25
    finish_early_min: float = 15.0
    finish_early_full_min: float = 30.0  # used when target_soc >= 100
    long_balance_weekday: int | None = None  # weekday (Mon=0) the window starts on
    long_balance_min: float = 60.0
    max_export_kw: float = 5.0
    saving_export_kw: float | None = None
    min_export_price: float = 12.5  # p/kWh
    discharge_eff: float = 0.9
    charge_eff: float = 0.93
    dispatch_charges_battery: bool = True
    horizon_slots: int = 72


@dataclass
class Rate:
    """A tariff period. ``price`` is GBP/kWh inc VAT."""

    start: datetime
    end: datetime
    price: float


@dataclass
class PlannerInputs:
    """Live inputs for one planning run."""

    now: datetime
    tz: tzinfo
    soc: float
    load_kw: list[float]  # 24 values, by local hour of day
    export_rates: list[Rate] = field(default_factory=list)
    import_rates: list[Rate] = field(default_factory=list)
    dispatches: list[tuple[datetime, datetime]] = field(default_factory=list)
    saving_session: tuple[datetime, datetime] | None = None
    free_session: tuple[datetime, datetime] | None = None
    spread_kw: float | None = None  # charge rate already in force, if known


@dataclass
class _Slot:
    start: datetime
    end: datetime
    frm: datetime
    hours: float
    charge: bool
    sched: bool
    free: bool
    saving: bool
    iog_h: float
    finish_by: datetime | None
    load_kw: float
    export_p: float | None  # p/kWh
    import_gbp: float | None  # GBP/kWh


def _rate_at(rates: list[Rate], t: datetime) -> float | None:
    for r in rates:
        if r.start <= t < r.end:
            return r.price
    return None


def _overlaps(s: datetime, e: datetime, win: tuple[datetime, datetime] | None) -> bool:
    return win is not None and s < win[1] and e > win[0]


def _r(v: float, dp: int) -> float:
    q = 10**dp
    return math.floor(v * q + 0.5) / q


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def build_slots(cfg: PlannerConfig, inp: PlannerInputs) -> list[_Slot]:
    now = inp.now.astimezone(timezone.utc)
    epoch = now.timestamp()
    start0 = datetime.fromtimestamp(math.floor(epoch / 1800) * 1800, timezone.utc)
    free = inp.free_session if inp.free_session and inp.free_session[1] > now else None
    saving = inp.saving_session if inp.saving_session and inp.saving_session[1] > now else None
    disp = list(inp.dispatches) if cfg.dispatch_charges_battery else []
    slots: list[_Slot] = []
    for i in range(cfg.horizon_slots):
        s = start0 + i * HALF
        e = s + HALF
        frm = now if i == 0 else s
        local = s.astimezone(inp.tz)
        sod = local.hour * 3600 + local.minute * 60
        in_sched = ((sod - cfg.charge_start) % 86400) < cfg.charge_duration
        finish_by = None
        if in_sched:
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            bs = midnight + timedelta(seconds=cfg.charge_start)
            if bs > local:
                bs -= timedelta(days=1)
            if cfg.target_soc >= 100:
                early = (
                    cfg.long_balance_min
                    if cfg.long_balance_weekday is not None and bs.weekday() == cfg.long_balance_weekday
                    else cfg.finish_early_full_min
                )
            else:
                early = cfg.finish_early_min
            finish_by = bs.astimezone(timezone.utc) + timedelta(seconds=cfg.charge_duration) - timedelta(minutes=early)
        in_free = _overlaps(s, e, free)
        iog_h = 0.0
        if not in_sched:
            for ws, we in disp:
                ov = (min(e, we) - max(frm, ws)).total_seconds()
                if ov > 0:
                    iog_h += ov / 3600
        exp_gbp = _rate_at(inp.export_rates, s)
        slots.append(
            _Slot(
                start=s,
                end=e,
                frm=frm,
                hours=(e - frm).total_seconds() / 3600,
                charge=in_sched or in_free,
                sched=in_sched,
                free=in_free,
                saving=_overlaps(s, e, saving),
                iog_h=iog_h,
                finish_by=finish_by,
                load_kw=float(inp.load_kw[local.hour]),
                export_p=None if exp_gbp is None else exp_gbp * 100,
                import_gbp=_rate_at(inp.import_rates, s),
            )
        )
    return slots


def _simulate(cfg: PlannerConfig, inp: PlannerInputs, slots: list[_Slot], exp: list[float]) -> list[dict]:
    cap = cfg.capacity_kwh
    kwh = inp.soc / 100 * cap
    res_kwh = cfg.reserve_soc / 100 * cap
    min_kwh = min(cfg.min_soc, cfg.reserve_soc) / 100 * cap
    tgt = cfg.target_soc / 100 * cap
    eff = cfg.discharge_eff
    spread = inp.spread_kw
    rows = []
    for i, sl in enumerate(slots):
        exp_kw = imp_kw = 0.0
        if sl.charge:
            kw = cfg.max_charge_kw
            if sl.sched and not sl.free and cfg.charge_mode == "spread":
                soc_pct = kwh / cap * 100
                if soc_pct + 5 <= cfg.target_soc or spread is None:
                    hrs = (sl.finish_by - sl.frm).total_seconds() / 3600
                    need = (cfg.target_soc - soc_pct) / 100 * cap
                    spread = cfg.max_charge_kw if hrs <= 0.1 else need / hrs + cfg.makeup_kw
                kw = min(cfg.max_charge_kw, max(cfg.min_charge_kw, spread))
            batt = min(kw, (tgt - kwh) / sl.hours) if kwh < tgt else 0.0
            mode = MODE_CHARGE if batt > 0 else MODE_HOLD
            imp_kw = batt / cfg.charge_eff + sl.load_kw
        elif sl.iog_h > 0:
            avail = max(0.0, tgt - kwh)
            batt = min(cfg.max_charge_kw * sl.iog_h, avail) / sl.hours
            rest_h = sl.hours - sl.iog_h
            imp_kw = batt / cfg.charge_eff + sl.load_kw * sl.iog_h / sl.hours
            if rest_h > 0:
                batt -= sl.load_kw / eff * rest_h / sl.hours
            mode = MODE_IOG if kwh < tgt - 0.05 else MODE_HOLD
        else:
            load_b = sl.load_kw / eff
            want = exp[i]
            if want > 0:
                room = max(0.0, (kwh - res_kwh) / sl.hours)
                exp_b = min(want / eff, max(0.0, room - load_b))
                exp_kw = exp_b * eff
                batt = -(load_b + exp_b)
            else:
                batt = -load_b
            mode = (MODE_SAVING if sl.saving else MODE_EXPORT) if exp_kw > 0.05 else MODE_SELF
            if kwh + batt * sl.hours < min_kwh:
                batt = min(0.0, (min_kwh - kwh) / sl.hours)
                mode = MODE_GRID
                exp_kw = 0.0
                imp_kw = max(0.0, sl.load_kw + batt * eff)
        kwh += batt * sl.hours
        rows.append({"kwh": kwh, "batt": batt, "mode": mode, "exp_kw": exp_kw, "imp_kw": imp_kw})
    return rows


def plan(cfg: PlannerConfig, inp: PlannerInputs) -> dict:
    """Run the planner and return a JSON-serialisable result."""
    slots = build_slots(cfg, inp)
    cap = cfg.capacity_kwh
    eff = cfg.discharge_eff
    sav_kw = cfg.saving_export_kw if cfg.saving_export_kw is not None else cfg.max_export_kw
    n = len(slots)
    exp = [0.0] * n
    for i, sl in enumerate(slots):
        if sl.saving and not sl.charge:
            exp[i] = sav_kw

    reserve_pts = [i for i, sl in enumerate(slots) if sl.charge and (i == 0 or not slots[i - 1].charge)]
    seg = 0
    for r in reserve_pts:
        if r == 0:
            seg = 1
            continue
        rows = _simulate(cfg, inp, slots, exp)
        res_soc = cfg.reserve_soc
        if slots[r].free and inp.free_session:
            free_h = (inp.free_session[1] - inp.free_session[0]).total_seconds() / 3600
            res_soc = max(cfg.reserve_soc, 100 - free_h * cfg.max_charge_kw / cap * 100)
        surplus = rows[r - 1]["kwh"] - res_soc / 100 * cap
        cands = [
            j
            for j in range(seg, r)
            if not (slots[j].charge or slots[j].saving or slots[j].iog_h > 0)
            and slots[j].export_p is not None
            and slots[j].export_p >= cfg.min_export_price
        ]
        cands.sort(key=lambda j: -slots[j].export_p)  # stable, like JS sort
        for j in cands:
            if surplus <= 0.05:
                break
            kw = min(cfg.max_export_kw, surplus * eff / slots[j].hours)
            exp[j] = kw
            surplus -= kw * slots[j].hours / eff
        seg = r
        while seg < n and slots[seg].charge:
            seg += 1

    rows = _simulate(cfg, inp, slots, exp)

    forecast = []
    days: dict[str, dict[str, float]] = {}
    for sl, row in zip(slots, rows):
        ik = row["imp_kw"] * sl.hours
        ek = row["exp_kw"] * sl.hours
        forecast.append(
            {
                "t": _iso(sl.end),
                "soc": _r(row["kwh"] / cap * 100, 1),
                "kw": _r(row["batt"], 2),
                "exp": _r(row["exp_kw"], 2),
                "ik": _r(ik, 3),
                "ek": _r(ek, 3),
                "mode": row["mode"],
                "rate": None if sl.export_p is None else _r(sl.export_p, 2),
                "ip": None if sl.import_gbp is None else _r(sl.import_gbp * 100, 2),
            }
        )
        d = days.setdefault(
            sl.start.astimezone(inp.tz).date().isoformat(),
            {"import_kwh": 0.0, "import_cost": 0.0, "export_kwh": 0.0, "export_value": 0.0, "priced": True, "hours": 0.0},
        )
        d["hours"] += sl.hours
        d["import_kwh"] += ik
        d["export_kwh"] += ek
        if sl.import_gbp is None and ik > 0:
            d["priced"] = False
        d["import_cost"] += ik * (sl.import_gbp or 0)
        d["export_value"] += ek * ((sl.export_p or 0) / 100)
    for d in days.values():
        for k in ("import_kwh", "import_cost", "export_kwh", "export_value"):
            d[k] = _r(d[k], 2 if "kwh" in k else 3)
        d["hours"] = _r(d["hours"], 2)

    blocks: list[dict] = []
    for i, (sl, row) in enumerate(zip(slots, rows)):
        if row["mode"] == MODE_SELF:
            continue
        last = blocks[-1] if blocks else None
        if last and last["mode"] == row["mode"] and last["_i"] == i - 1:
            last["end"] = _iso(sl.end)
            last["_i"] = i
            last["kwh"] += abs(row["batt"]) * sl.hours
            last["end_soc"] = _r(row["kwh"] / cap * 100, 1)
        else:
            blocks.append(
                {
                    "mode": row["mode"],
                    "start": _iso(inp.now if i == 0 else sl.start),
                    "end": _iso(sl.end),
                    "_i": i,
                    "kwh": abs(row["batt"]) * sl.hours,
                    "end_soc": _r(row["kwh"] / cap * 100, 1),
                }
            )
    for b in blocks:
        del b["_i"]
        b["kwh"] = _r(b["kwh"], 1)

    first_res = next((r for r in reserve_pts if r > 0), None)
    soc_at_charge = forecast[first_res - 1]["soc"] if first_res is not None else forecast[-1]["soc"]
    nxt = blocks[0] if blocks else None
    all_rates = inp.export_rates
    return {
        "soc_now": inp.soc,
        "soc_at_next_charge": soc_at_charge,
        "min_soc_ahead": min(f["soc"] for f in forecast),
        "reserve_soc": cfg.reserve_soc,
        "capacity_kwh": _r(cap, 1),
        "charge_kw": _r(cfg.max_charge_kw, 2),
        "max_export_kw": cfg.max_export_kw,
        "min_export_rate": cfg.min_export_price,
        "export_kwh_planned": _r(sum(r["exp_kw"] * s.hours for s, r in zip(slots, rows)), 1),
        "next_action": nxt["mode"] if nxt else MODE_SELF,
        "next_action_start": nxt["start"] if nxt else None,
        "next_action_end": nxt["end"] if nxt else None,
        "iog_dispatches": [[_iso(a), _iso(b)] for a, b in inp.dispatches],
        "free_session": [_iso(x) for x in inp.free_session] if inp.free_session else None,
        "saving_session": [_iso(x) for x in inp.saving_session] if inp.saving_session else None,
        "rates_until": _iso(max(r.end for r in all_rates)) if all_rates else None,
        "days": days,
        "plan": blocks,
        "forecast": forecast,
        "generated_at": _iso(inp.now),
    }


def merge_windows(wins: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping/adjacent windows."""
    out: list[list[datetime]] = []
    for s, e in sorted(wins):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def hours_before(wins: list[tuple[datetime, datetime]], end: datetime, span: timedelta = timedelta(hours=24)) -> float:
    """Hours of (merged) windows inside [end - span, end]."""
    lo = end - span
    tot = 0.0
    for s, e in merge_windows(wins):
        a, b = max(s, lo), min(e, end)
        if b > a:
            tot += (b - a).total_seconds() / 3600
    return tot


def covers(wins: list[tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    """True if one merged window fully covers [start, end]."""
    return any(s <= start and e >= end for s, e in merge_windows(wins))


def day_key(t: datetime, tz: tzinfo) -> str:
    return t.astimezone(tz).date().isoformat()


__all__ = [
    "PlannerConfig",
    "PlannerInputs",
    "Rate",
    "plan",
    "merge_windows",
    "hours_before",
    "covers",
    "day_key",
    "MODES",
]
