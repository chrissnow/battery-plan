"""Battery charge/export planner.

Pure Python (no Home Assistant imports) so it can be unit-tested on its own.

Model, per half-hour slot over the horizon:
- Inside the daily charge window the battery grid-charges towards target_soc,
  either at full power or spread so it finishes ``finish_early`` minutes before
  the window ends (like a DVCC charge-current spreader). The house runs from grid.
- Inside an Intelligent Octopus dispatch (outside the window) the battery
  grid-charges at full power, if ``dispatch_charges_battery``.
- Inside a free-electricity (power-up) session the battery charges at full power, until
  the session's import reaches ``free_cap_kwh`` (house load counts too). Past the cap it
  carries on only where import is off-peak anyway (e.g. an IOG dispatch for the car);
  at peak the session ends there and the import over the cap is paid at the rate.
- Otherwise the house runs from the battery, down to ``min_soc``, then from grid.
  Saving sessions export at ``saving_export_kw``.
- Exports are chosen by one rule: export a kWh when it earns at least ``export_margin``
  p/kWh more than keeping it would save, as the simulated plan shows (a smaller recharge
  in the window, house load not bought later, nothing ahead of a free-electricity
  session). Exports stop at the export floor of the time. See ``_ExportSearch``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    reserve_soc: float = 20.0  # export floor (at night / at the charge window)
    day_reserve_soc: float | None = None  # optional higher floor during the day
    day_reserve_start: int = 5 * 3600 + 25 * 60  # seconds after local midnight
    day_reserve_end: int = 22 * 3600 + 25 * 60
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
    inverter_kw: float | None = None  # max inverter AC output (house load + export), if limited
    export_margin: float = 5.0  # p/kWh an export must earn over keeping the energy
    export_peak_import: bool = False  # exports may leave the house buying at peak before the window
    buy_ahead_h: float = 3.0  # with export_peak_import: hold / grid-charge at most this long before an export
    night_cycle: bool = False  # may export inside the charge window and buy it back there
    reject_dispatch: bool = False  # may export in an IOG dispatch (EV inhibited) instead of topping up
    discharge_eff: float = 0.9
    charge_eff: float = 0.93
    dispatch_charges_battery: bool = True
    free_cap_kwh: float = 16.0  # free import per free-electricity session (0 = no cap)
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
    load_kw: list[float]  # 24 (per hour) or 48 (per half hour) values, local time
    export_rates: list[Rate] = field(default_factory=list)
    import_rates: list[Rate] = field(default_factory=list)
    dispatches: list[tuple[datetime, datetime]] = field(default_factory=list)
    saving_session: tuple[datetime, datetime] | None = None
    free_sessions: list[tuple[datetime, datetime]] = field(default_factory=list)
    free_used_kwh: float = 0.0  # grid import so far in the (capped) free session in progress
    power_up_sessions: list[tuple[datetime, datetime]] = field(default_factory=list)  # uncapped
    spread_kw: float | None = None  # charge rate already in force, if known
    # car ready time (Intelligent Octopus target): the dispatch slot just before it is the one
    # that doesn't move, so it's the only dispatch exports may count on to refill the battery
    ready: datetime | None = None
    # weather-dependent load, per half-hour slot start (epoch):
    # (load adjustment kW, heating kW, cooling kW, forecast temperature)
    weather: dict[int, tuple[float, float, float, float]] = field(default_factory=dict)


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
    floor: float  # export floor SOC % for this slot
    export_p: float | None  # p/kWh
    import_gbp: float | None  # GBP/kWh
    heat_kw: float = 0.0
    cool_kw: float = 0.0
    temp: float | None = None
    next_charge: int = 0  # index of the first charge slot at or after this one (len(slots) if none)
    buyback: int | None = None  # in a charge window: the slot ending at its finish-early time
    free_id: int | None = None  # which free session (they each have their own cap)
    cheap: bool = False  # import at the lowest rate seen (off-peak, or a dispatch priced so)
    car: bool = False  # an IOG dispatch (the car charging) overlaps this half hour


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


def floor_at(cfg: PlannerConfig, t: datetime, tz: tzinfo) -> float:
    """Export floor (SOC %) at time ``t``: the day floor inside its window, else the night floor."""
    if cfg.day_reserve_soc is None:
        return cfg.reserve_soc
    local = t.astimezone(tz)
    sod = local.hour * 3600 + local.minute * 60 + local.second
    a, b = cfg.day_reserve_start, cfg.day_reserve_end
    inside = a <= sod < b if a <= b else (sod >= a or sod < b)
    return cfg.day_reserve_soc if inside else cfg.reserve_soc


def charge_finish_by(cfg: PlannerConfig, t: datetime, tz: tzinfo) -> datetime | None:
    """If ``t`` is inside the daily charge window, when spread charging should finish (UTC)."""
    local = t.astimezone(tz)
    sod = local.hour * 3600 + local.minute * 60 + local.second
    if ((sod - cfg.charge_start) % 86400) >= cfg.charge_duration:
        return None
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
    return bs.astimezone(timezone.utc) + timedelta(seconds=cfg.charge_duration) - timedelta(minutes=early)


def offpeak_window(
    rates: list[Rate], dispatches: list[tuple[datetime, datetime]], tz: tzinfo, min_hours: float = 2.0
) -> tuple[int, int] | None:
    """The daily off-peak window read from import rates: (start, duration), seconds after local midnight.

    A half hour of the day is off-peak if it is at the lowest rate seen (within 0.1p) on every
    day the rates cover. Half hours that overlap a planned dispatch (which Intelligent Octopus
    also prices off-peak) don't count either way; inside the window they're filled in, so a
    dispatch at 01:00 doesn't split it, while one just after it doesn't stretch it. The window
    is the longest run of off-peak half hours, wrapping past midnight, of at least ``min_hours``.
    None if there isn't one (e.g. Agile, or a flat rate): the configured window applies.
    """
    if not rates:
        return None
    low = min(r.price for r in rates)
    cheap: list[bool | None] = [None] * 48
    for r in rates:
        t = r.start
        while t < r.end:
            e = min(r.end, t + HALF)
            if not any(ds < e and de > t for ds, de in dispatches):
                local = t.astimezone(tz)
                k = local.hour * 2 + (local.minute >= 30)
                c = r.price <= low + 0.001
                cheap[k] = c if cheap[k] is None else (cheap[k] and c)
            t = e
    if all(c is not False for c in cheap) or not any(cheap):
        return None  # flat rate, or nothing cheaper
    # fill unknown half hours with an off-peak one on both sides
    for k in range(48):
        if cheap[k] is None:
            before = next(cheap[(k - n) % 48] for n in range(1, 48) if cheap[(k - n) % 48] is not None)
            after = next(cheap[(k + n) % 48] for n in range(1, 48) if cheap[(k + n) % 48] is not None)
            if before and after:
                cheap[k] = True
    first = next(k for k in range(48) if cheap[k] is not True)
    best_start, best_len, start, length = 0, 0, 0, 0
    for n in range(1, 49):
        k = (first + n) % 48
        if cheap[k] is True:
            if length == 0:
                start = k
            length += 1
            if length > best_len:
                best_start, best_len = start, length
        else:
            length = 0
    if best_len * 0.5 < min_hours:
        return None
    return best_start * 1800, best_len * 1800


def spread_charge_kw(
    cfg: PlannerConfig, soc: float, now: datetime, finish_by: datetime, previous: float | None
) -> float:
    """Charge power for spread mode: energy still needed / time left, plus make-up, clamped.

    Not recalculated once within 5% of the target (keeps ``previous``), like a DVCC spreader.
    """
    spread = previous
    if soc + 5 <= cfg.target_soc or spread is None:
        hrs = (finish_by - now).total_seconds() / 3600
        need = (cfg.target_soc - soc) / 100 * cfg.capacity_kwh
        spread = cfg.max_charge_kw if hrs <= 0.1 else need / hrs + cfg.makeup_kw
    return min(cfg.max_charge_kw, max(cfg.min_charge_kw, spread))


def free_sessions(cfg: PlannerConfig, inp: PlannerInputs) -> list[tuple[datetime, datetime, float]]:
    """Free sessions not yet over, earliest first: (start, end, free import cap kWh).

    Free-electricity sessions are capped at ``free_cap_kwh`` each; Power Ups aren't."""
    now = inp.now.astimezone(timezone.utc)
    cap = cfg.free_cap_kwh if cfg.free_cap_kwh > 0 else math.inf
    wins = list(inp.free_sessions)
    # the Octopus integration lists the capped weekend hours in its Power Up calendar too:
    # one with the same times as a free session is that session, so it keeps the cap
    same = {(s, e) for s, e in wins}
    out = [(s, e, cap) for s, e in wins] + [(s, e, math.inf) for s, e in inp.power_up_sessions if (s, e) not in same]
    return sorted(w for w in out if w[1] > now)


def build_slots(cfg: PlannerConfig, inp: PlannerInputs) -> list[_Slot]:
    now = inp.now.astimezone(timezone.utc)
    epoch = now.timestamp()
    start0 = datetime.fromtimestamp(math.floor(epoch / 1800) * 1800, timezone.utc)
    frees = free_sessions(cfg, inp)
    low = min((r.price for r in inp.import_rates), default=None)
    saving = inp.saving_session if inp.saving_session and inp.saving_session[1] > now else None
    disp = list(inp.dispatches) if cfg.dispatch_charges_battery else []
    slots: list[_Slot] = []
    for i in range(cfg.horizon_slots):
        s = start0 + i * HALF
        e = s + HALF
        frm = now if i == 0 else s
        local = s.astimezone(inp.tz)
        finish_by = charge_finish_by(cfg, s, inp.tz)
        in_sched = finish_by is not None
        # overlapping sessions (a Power Up during a free session): the one with the larger cap
        hits = [k for k, (ws, we, _) in enumerate(frees) if _overlaps(s, e, (ws, we))]
        free_id = max(hits, key=lambda k: frees[k][2]) if hits else None
        in_free = free_id is not None
        imp_gbp = _rate_at(inp.import_rates, s)
        iog_h = 0.0
        if not in_sched:
            for ws, we in disp:
                ov = (min(e, we) - max(frm, ws)).total_seconds()
                if ov > 0:
                    iog_h += ov / 3600
        car = any(_overlaps(s, e, w) for w in inp.dispatches)
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
                load_kw=max(0.0, float(inp.load_kw[local.hour * 2 + (local.minute >= 30)] if len(inp.load_kw) == 48 else inp.load_kw[local.hour])
                            + (inp.weather.get(int(s.timestamp()), (0.0,))[0])),
                heat_kw=inp.weather.get(int(s.timestamp()), (0.0, 0.0))[1],
                cool_kw=inp.weather.get(int(s.timestamp()), (0.0, 0.0, 0.0))[2],
                temp=inp.weather[int(s.timestamp())][3] if int(s.timestamp()) in inp.weather else None,
                floor=floor_at(cfg, frm, inp.tz),
                export_p=None if exp_gbp is None else exp_gbp * 100,
                import_gbp=imp_gbp,
                free_id=free_id,
                car=car,
                cheap=iog_h > 0 or (imp_gbp is not None and low is not None and imp_gbp <= low + 0.001),
            )
        )
    nxt = len(slots)
    for i in range(len(slots) - 1, -1, -1):
        if slots[i].charge:
            nxt = i
        slots[i].next_charge = nxt
    i = 0
    while i < len(slots):  # each charge window: energy sold in it is bought back by its finish-early time
        if not (slots[i].sched and not slots[i].free):
            i += 1
            continue
        j = i
        while j + 1 < len(slots) and slots[j + 1].sched and not slots[j + 1].free:
            j += 1
        ends = [k for k in range(i, j + 1) if slots[k].end <= slots[k].finish_by]
        for k in range(i, j + 1):
            slots[k].buyback = ends[-1] if ends else j
        i = j + 1
    return slots


def _simulate(
    cfg: PlannerConfig,
    inp: PlannerInputs,
    slots: list[_Slot],
    exp: list[float],
    buy: list[float | None] | None = None,
) -> list[dict]:
    cap = cfg.capacity_kwh
    kwh = inp.soc / 100 * cap
    min_kwh = min(cfg.min_soc, cfg.reserve_soc) / 100 * cap
    tgt = cfg.target_soc / 100 * cap
    eff = cfg.discharge_eff
    spread = inp.spread_kw
    # free import left per session; the one in progress has used some already
    now = inp.now.astimezone(timezone.utc)
    frees = free_sessions(cfg, inp)
    left: dict[int, float] = {}
    for k, (fs, _, fcap) in enumerate(frees):
        left[k] = max(0.0, fcap - (inp.free_used_kwh if fs <= now else 0.0))
    # buy-sell-buy: each window's last half hour that sells (by its buy-back slot); up to it the
    # window charges at full power, as a spread charge leaves no time to buy back much
    last_sell: dict[int, int] = {}
    if cfg.night_cycle:
        for i, sl in enumerate(slots):
            if sl.sched and exp[i] > 0 and sl.buyback is not None:
                last_sell[sl.buyback] = i
    rows = []
    for i, sl in enumerate(slots):
        exp_kw = imp_kw = free_kw = 0.0
        # a free session charges while it has free import left, or past that where import is off-peak
        # (less than the house load's worth left at peak: the battery runs the house instead)
        free_now = sl.free_id is not None and (sl.cheap or left[sl.free_id] > max(0.05, sl.load_kw * sl.hours))
        # a window half hour sells (instead of charging) only if the battery is above the floor by
        # more than the house load, so an export it can't make doesn't stop the charge
        sells = (exp[i] > 0 and sl.sched and not free_now
                 and (kwh - sl.floor / 100 * cap) / sl.hours > sl.load_kw / eff + 0.05)
        if (sl.sched or free_now) and not sells:
            kw = cfg.max_charge_kw
            full = sl.buyback in last_sell and i <= last_sell[sl.buyback]
            if sl.sched and not free_now and cfg.charge_mode == "spread" and not full:
                kw = spread = spread_charge_kw(cfg, kwh / cap * 100, sl.frm, sl.finish_by, spread)
            batt = min(kw, (tgt - kwh) / sl.hours) if kwh < tgt else 0.0
            if free_now and not sl.cheap and not sl.sched:
                # at peak: charge only with what's left of the free import after the house load
                batt = min(batt, max(0.0, (left[sl.free_id] / sl.hours - sl.load_kw) * cfg.charge_eff))
            mode = MODE_CHARGE if batt > 0 else MODE_HOLD
            imp_kw = batt / cfg.charge_eff + sl.load_kw
            if free_now:
                free_kw = min(imp_kw, left[sl.free_id] / sl.hours)
                left[sl.free_id] -= free_kw * sl.hours
        elif sl.iog_h > 0 and not exp[i] > 0:  # (an export there rejects the dispatch)
            avail = max(0.0, tgt - kwh)
            batt = min(cfg.max_charge_kw * sl.iog_h, avail) / sl.hours
            rest_h = sl.hours - sl.iog_h
            imp_kw = batt / cfg.charge_eff + sl.load_kw * sl.iog_h / sl.hours
            if rest_h > 0:
                batt -= sl.load_kw / eff * rest_h / sl.hours
            mode = MODE_IOG if kwh < tgt - 0.05 else MODE_HOLD
        elif buy and buy[i] is not None:
            # bought in advance for an export: the house runs from grid and the battery rests
            # (0) or grid-charges at up to buy[i] kW
            batt = min(buy[i], (tgt - kwh) / sl.hours) if kwh < tgt else 0.0
            mode = MODE_CHARGE if batt > 0.05 else MODE_HOLD
            imp_kw = sl.load_kw + batt / cfg.charge_eff
        else:
            load_b = sl.load_kw / eff
            want = exp[i]
            if want > 0 and cfg.inverter_kw:
                # the inverter's AC output feeds the house first: export gets what's left
                want = min(want, max(0.0, cfg.inverter_kw - sl.load_kw))
            if want > 0:
                room = max(0.0, (kwh - sl.floor / 100 * cap) / sl.hours)
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
        rows.append({"kwh": kwh, "batt": batt, "mode": mode, "exp_kw": exp_kw, "imp_kw": imp_kw,
                     "free_kw": free_kw, "free": free_now})
    return rows


def _import_price(inp: PlannerInputs, sl: _Slot) -> float | None:
    """Import price (GBP/kWh) for a slot; past the published rates, the same time on an earlier day."""
    if sl.import_gbp is not None:
        return sl.import_gbp
    for days in (1, 2):
        p = _rate_at(inp.import_rates, sl.start - timedelta(days=days))
        if p is not None:
            return p
    return None


def _export_candidates(cfg: PlannerConfig, slots: list[_Slot]) -> list[int]:
    """Slots an export may go in: ordinary slots before a charge window (so the energy is
    replaced inside the horizon), and with ``night_cycle`` the window's own slots up to its
    finish-early time."""
    out = []
    for i, sl in enumerate(slots):
        if sl.export_p is None:
            continue
        if not (sl.charge or sl.saving or (sl.iog_h > 0 and not cfg.reject_dispatch)):
            if sl.next_charge < len(slots):
                out.append(i)
        elif cfg.night_cycle and sl.sched and not sl.free and not sl.car and sl.end <= sl.finish_by:
            # (not while the car charges in a dispatch: it would take the export)
            out.append(i)
    return out


class _ExportSearch:
    """Chooses exports that earn at least ``export_margin`` p/kWh more than keeping the energy.

    Keeping it is worth whatever replaces it later in the simulated plan: a smaller recharge
    in the window, house load not bought at peak, or nothing if a free-electricity session
    refills the battery anyway. Every candidate is judged on the whole plan's value.

    1. ``raise_exports``: best price first, each slot raised in 0.5 kW steps while the last
       step still clears the margin. Unless ``export_peak_import``, a step may not cause
       import outside the charge window, dispatches and free sessions.
    2. ``buy_ahead`` (with ``export_peak_import``): where an export still stops at the floor,
       buy for it in advance, no more than ``buy_ahead_h`` before its run of well-paid half
       hours, nearest first: hold (house on grid, battery resting), then grid-charge in 2 kW
       steps, as much as gives the most value over the margin.

    Energy sold inside a charge window must be bought back by its finish-early time, as what's
    left after the horizon isn't valued and the battery should sit full then (balancing).
    """

    STEP_KW = 0.5
    BUY_STEP_KW = 2.0
    SHORT_KWH = 0.3  # tolerance on buying back what's sold in a window

    def __init__(self, cfg: PlannerConfig, inp: PlannerInputs, slots: list[_Slot], exp: list[float]) -> None:
        self.cfg, self.inp, self.slots, self.exp = cfg, inp, slots, exp
        self.buy: list[float | None] = [None] * len(slots)
        self.margin = cfg.export_margin / 100
        self.tgt_kwh = cfg.target_soc / 100 * cfg.capacity_kwh
        known = [p for p in (_import_price(inp, sl) for sl in slots) if p is not None]
        worst = max(known, default=0.0)
        # an unpriced slot is assumed to cost the most seen (free-session import is netted off per row)
        self.imp_p = [_import_price(inp, sl) or worst for sl in slots]
        # each window's first slot by its buy-back slot, and how short of the target it already
        # ends without exports in it (the spread charge can land a little under)
        self.windows: dict[int, int] = {}
        for i, sl in enumerate(slots):
            if sl.buyback is not None:
                self.windows.setdefault(sl.buyback, i)
        rows0 = _simulate(cfg, inp, slots, [0.0 if sl.sched else x for sl, x in zip(slots, exp)], self.buy)
        self.short0 = {b: max(0.0, self.tgt_kwh - rows0[b]["kwh"]) for b in self.windows}

    def simulate(self) -> list[dict]:
        return _simulate(self.cfg, self.inp, self.slots, self.exp, self.buy)

    def value(self) -> tuple[float, float, float, float]:
        """Value (GBP), export kWh, import kWh outside cheap periods, and the shortfall (kWh)
        below the target at the buy-back slot of a window the plan exports in."""
        rows = self.simulate()
        v = ek_tot = peak = 0.0
        for sl, r, ip in zip(self.slots, rows, self.imp_p):
            ek, ik, paid = r["exp_kw"] * sl.hours, r["imp_kw"] * sl.hours, (r["imp_kw"] - r["free_kw"]) * sl.hours
            ek_tot += ek
            v += ek * ((sl.export_p or 0) / 100) - paid * ip
            if not (sl.sched or sl.iog_h > 0 or r["free"]):
                peak += ik
        short = 0.0
        for b, a in self.windows.items():
            if any(self.exp[i] > 0 for i in range(a, b + 1)):
                short = max(short, self.tgt_kwh - rows[b]["kwh"] - self.short0[b])
        return v, ek_tot, peak, short

    def beats_peak(self, j: int) -> bool:
        """With buy at peak on: an export may leave the house buying at peak (afterwards or in
        advance) only if it pays at least the peak import price plus the margin. Judged on the whole
        plan, a step whose energy mostly comes back cheaply overnight could otherwise buy a little
        at peak for an export worth less than the import."""
        if not self.cfg.export_peak_import:
            return False
        peak = max((p for sl, p in zip(self.slots, self.imp_p) if not (sl.cheap or sl.sched)), default=0.0)
        return (self.slots[j].export_p or 0) / 100 >= peak + self.margin

    def order(self, cands: list[int]) -> list[int]:
        """Best price first; at equal prices the latest half hour (a later replan may yet cancel
        it), but inside a charge window the earliest, to leave time to buy the energy back."""
        sl = self.slots
        return sorted(set(cands), key=lambda j: (-sl[j].export_p, j if sl[j].sched else -j))

    def raise_exports(self, order: list[int]) -> None:
        cfg, exp = self.cfg, self.exp
        for j in order:
            if exp[j] >= cfg.max_export_kw - 1e-6:
                continue
            levels = [exp[j]]
            while levels[-1] < cfg.max_export_kw - 1e-6:
                levels.append(min(cfg.max_export_kw, levels[-1] + self.STEP_KW))
            cache: dict[int, tuple[float, float, float, float]] = {}

            def at(i: int) -> tuple[float, float, float, float]:
                if i not in cache:
                    exp[j] = levels[i]
                    cache[i] = self.value()
                return cache[i]

            def pays(i: int) -> bool:
                (v0, e0, p0, _), (v1, e1, p1, short) = at(i - 1), at(i)
                if p1 - p0 > 0.01 and not self.beats_peak(j):
                    return False  # it would leave the house buying at peak for less than that costs
                if short > self.SHORT_KWH:
                    return False
                return e1 - e0 > 0.01 and v1 - v0 >= self.margin * (e1 - e0)

            # each extra step is worth less than the last (the floor, dearer replacement), so bisect
            lo, hi = 0, len(levels) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if pays(mid):
                    lo = mid
                else:
                    hi = mid - 1
            exp[j] = levels[lo]

    def buy_ahead(self, order: list[int], cands: list[int]) -> None:
        cfg, slots, exp, buy = self.cfg, self.slots, self.exp, self.buy
        ok = set(cands)
        ahead = timedelta(hours=cfg.buy_ahead_h)
        for j in order:
            if slots[j].sched:
                continue  # in a window, buying is just the charge
            if not self.beats_peak(j):
                continue  # buying at peak for an export that pays less than that costs
            while self.simulate()[j]["exp_kw"] < cfg.max_export_kw - 0.05:  # held back by the floor
                steps = self._buy_steps(j, ok, ahead)
                if not steps:
                    break
                base = self.value()
                saved, old = list(buy), exp[j]
                exp[j] = cfg.max_export_kw
                # a half hour bought may only top up another export or this slot's own load
                # (which must sit above the floor too), so try ever more and keep the best amount
                best, best_n = 0.0, 0
                for n, (k, kw) in enumerate(steps, 1):
                    buy[k] = kw
                    v, e, _, short = self.value()
                    if e - base[1] > 0.01 and short <= self.SHORT_KWH:
                        score = v - base[0] - self.margin * (e - base[1])
                        if score > best + 1e-4:
                            best, best_n = score, n
                buy[:] = saved
                for k, kw in steps[:best_n]:
                    buy[k] = kw
                if best_n == 0:
                    exp[j] = old
                    break

    def _buy_steps(self, j: int, ok: set[int], ahead: timedelta) -> list[tuple[int, float]]:
        """Ways to buy in advance for the export at ``j``, nearest first: holds, then charge steps.
        Never in a half hour paying about as much to export, and only within ``ahead`` of the
        start of that run of well-paid half hours."""
        cfg, slots, exp, buy = self.cfg, self.slots, self.exp, self.buy
        dear = lambda k: (slots[k].export_p or 0) + cfg.export_margin >= slots[j].export_p
        run = j
        while run > 0 and not slots[run - 1].charge and dear(run - 1):
            run -= 1
        near = [
            k
            for k in range(run - 1, -1, -1)
            if k in ok
            and slots[k].next_charge == slots[j].next_charge
            and exp[k] <= 0
            and not dear(k)
            and slots[k].start >= slots[run].start - ahead
        ]
        steps = [(k, 0.0) for k in near if buy[k] is None]
        for k in near:
            kw = max(buy[k] or 0.0, 0.0)
            while kw < cfg.max_charge_kw - 1e-6:
                kw = min(cfg.max_charge_kw, kw + self.BUY_STEP_KW)
                steps.append((k, kw))
        return steps


def _firm_dispatches(inp: PlannerInputs) -> list[tuple[datetime, datetime]]:
    """Dispatches exports may count on: only the half hour before the ready time.

    Other dispatch times move (the hours before ready stay about the same, but may come
    before an export) or vanish, so the export search plans as if they weren't there."""
    if inp.ready is None:
        return []
    a, b = inp.ready - HALF, inp.ready
    return [(max(s, a), min(e, b)) for s, e in inp.dispatches if s < b and e > a]


def _choose_exports(
    cfg: PlannerConfig, inp: PlannerInputs, slots: list[_Slot], search_slots: list[_Slot]
) -> tuple[list[float], list[float | None]]:
    """Export kW wanted per slot, and what's bought in advance (None, 0 = hold, kW = grid charge).

    ``search_slots`` are the slots as the search sees them: only firm dispatches."""
    sav_kw = cfg.saving_export_kw if cfg.saving_export_kw is not None else cfg.max_export_kw
    exp = [sav_kw if sl.saving and not sl.charge else 0.0 for sl in slots]
    search = _ExportSearch(cfg, inp, search_slots, exp)
    cands = _export_candidates(cfg, slots)
    if cands:
        order = search.order(cands)
        search.raise_exports(order)
        if cfg.export_peak_import:
            search.buy_ahead(order, cands)
    return exp, search.buy


def plan(cfg: PlannerConfig, inp: PlannerInputs) -> dict:
    """Run the planner and return a JSON-serialisable result."""
    slots = build_slots(cfg, inp)
    firm = _firm_dispatches(inp) if cfg.dispatch_charges_battery else []
    search_slots = slots if firm == list(inp.dispatches) else build_slots(cfg, replace(inp, dispatches=firm))
    exp, buy = _choose_exports(cfg, inp, slots, search_slots)
    rows = _simulate(cfg, inp, slots, exp, buy)
    return _report(cfg, inp, slots, rows)


def _report(cfg: PlannerConfig, inp: PlannerInputs, slots: list[_Slot], rows: list[dict]) -> dict:
    """The plan as published: per-slot forecast, per-day costs, blocks and headline figures."""
    cap = cfg.capacity_kwh
    forecast = []
    days: dict[str, dict[str, float]] = {}
    for sl, row in zip(slots, rows):
        ik = row["imp_kw"] * sl.hours
        ek = row["exp_kw"] * sl.hours
        paid = ik - row["free_kw"] * sl.hours  # free-session import is credited afterwards
        # import price paid on average in the slot: 0 while a free session covers it all
        if sl.import_gbp is None:
            ip = 0.0 if row["free"] and paid < 1e-6 else None
        elif ik > 1e-9:
            ip = _r(sl.import_gbp * 100 * paid / ik, 2)
        else:
            ip = 0.0 if row["free"] else _r(sl.import_gbp * 100, 2)
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
                "ip": ip,
                "ld": _r(sl.load_kw, 2),
                "fl": sl.floor,
            }
        )
        if inp.weather:
            if sl.heat_kw:
                forecast[-1]["wh"] = _r(sl.heat_kw, 2)
            if sl.cool_kw:
                forecast[-1]["wc"] = _r(sl.cool_kw, 2)
            if sl.temp is not None:
                forecast[-1]["tf"] = _r(sl.temp, 1)
        d = days.setdefault(
            sl.start.astimezone(inp.tz).date().isoformat(),
            {"import_kwh": 0.0, "import_cost": 0.0, "export_kwh": 0.0, "export_value": 0.0, "priced": True, "hours": 0.0},
        )
        d["hours"] += sl.hours
        if inp.weather:
            d["weather_kwh"] = d.get("weather_kwh", 0.0) + (sl.heat_kw + sl.cool_kw) * sl.hours
        d["import_kwh"] += ik
        d["export_kwh"] += ek
        if sl.import_gbp is None and paid > 1e-6:
            d["priced"] = False  # rates not published yet: this import isn't in import_cost
            d["unpriced_kwh"] = d.get("unpriced_kwh", 0.0) + paid
        d["import_cost"] += paid * (sl.import_gbp or 0)
        d["export_value"] += ek * ((sl.export_p or 0) / 100)
    for d in days.values():
        for k in ("import_kwh", "import_cost", "export_kwh", "export_value", "weather_kwh", "unpriced_kwh"):
            if k in d:
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

    sessions = []
    for k, (fs, fe, fcap) in enumerate(free_sessions(cfg, inp)):
        own = [(sl, r) for sl, r in zip(slots, rows) if sl.free_id == k]
        on = [sl for sl, r in own if r["free"]]
        sessions.append({
            "start": _iso(fs),
            "end": _iso(fe),
            "kind": "power_up" if math.isinf(fcap) and (fs, fe) in inp.power_up_sessions else "free",
            "cap_kwh": None if math.isinf(fcap) else fcap,
            # when the plan stops treating it as free (the cap runs out at peak), else its end
            "until": _iso(min(fe, on[-1].end)) if on else _iso(fs),
            "free_kwh": _r(sum(r["free_kw"] * sl.hours for sl, r in own), 2),
            "used_kwh": _r(inp.free_used_kwh, 2) if fs <= inp.now else 0.0,
        })

    first_res = next((i for i in range(1, len(slots)) if slots[i].charge and not slots[i - 1].charge), None)
    soc_at_charge = forecast[first_res - 1]["soc"] if first_res is not None else forecast[-1]["soc"]
    nxt = blocks[0] if blocks else None
    all_rates = inp.export_rates
    return {
        "soc_now": inp.soc,
        "soc_at_next_charge": soc_at_charge,
        "min_soc_ahead": min(f["soc"] for f in forecast),
        "reserve_soc": floor_at(cfg, inp.now, inp.tz),
        "night_reserve_soc": cfg.reserve_soc,
        "min_soc": min(cfg.min_soc, cfg.reserve_soc),  # the house may use the battery down to this
        "day_reserve_soc": cfg.day_reserve_soc,
        "capacity_kwh": _r(cap, 1),
        "charge_kw": _r(cfg.max_charge_kw, 2),
        "max_export_kw": cfg.max_export_kw,
        "export_margin": cfg.export_margin,
        "export_kwh_planned": _r(sum(r["exp_kw"] * s.hours for s, r in zip(slots, rows)), 1),
        "next_action": nxt["mode"] if nxt else MODE_SELF,
        "next_action_start": nxt["start"] if nxt else None,
        "next_action_end": nxt["end"] if nxt else None,
        "iog_dispatches": [[_iso(a), _iso(b)] for a, b in inp.dispatches],
        "free_session": [sessions[0]["start"], sessions[0]["end"]] if sessions else None,
        "free_sessions": sessions,
        "free_cap_kwh": cfg.free_cap_kwh,
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
    "charge_finish_by",
    "floor_at",
    "offpeak_window",
    "spread_charge_kw",
    "Rate",
    "plan",
    "merge_windows",
    "hours_before",
    "covers",
    "day_key",
    "MODES",
]
