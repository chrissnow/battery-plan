"""Planner tests: pure Python, no Home Assistant needed.

Without the Home Assistant test packages: pytest --noconftest tests/test_planner.py
"""
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import pytest

# load planner.py on its own: importing the package would import Home Assistant
_spec = importlib.util.spec_from_file_location(
    "battery_plan_planner", Path(__file__).parents[1] / "custom_components" / "battery_plan" / "planner.py"
)
P = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = P
_spec.loader.exec_module(P)

TZ = ZoneInfo("Europe/London")
D0 = datetime(2026, 11, 4, tzinfo=timezone.utc)
IOG = [0.066 if (i < 11 or i >= 47) else 0.3426 for i in range(48)]  # 6.6p 23:30-05:30, 34.26p otherwise
BASE = dict(capacity_kwh=60, reserve_soc=20, min_soc=5, max_export_kw=10, charge_mode="max", max_charge_kw=10)


def _rates(vals):
    return [P.Rate(D0 + i * P.HALF, D0 + (i + 1) * P.HALF, v) for i, v in enumerate(vals)]


def _plan(exp, soc=50, load=2.0, now=None, imp=None, **kw):
    """Plan from 12:00 on 4 Nov (unless ``now``) with IOG import and ``exp`` (GBP/kWh per half hour from 00:00)."""
    cfg = {**BASE, **kw}
    sessions = {k: cfg.pop(k) for k in ("dispatches", "free_session", "saving_session", "ready") if k in cfg}
    inp = P.PlannerInputs(now=now or D0 + timedelta(hours=12), tz=TZ, soc=soc, load_kw=[load] * 24,
                          export_rates=_rates(exp), import_rates=_rates(imp or IOG * 3), **sessions)
    return P.plan(P.PlannerConfig(**cfg), inp)


def _arb_plan(soc=50, exp_peak=0.38, exp_off=0.20, **kw):
    """Export peak 16:00-19:00."""
    return _plan([exp_peak if 32 <= i < 38 else exp_off for i in range(48)] * 2, soc=soc, **kw)


def soc_at(res, hhmm, day="2026-11-04"):
    return next(f["soc"] for f in res["forecast"] if f["t"] == f"{day}T{hhmm}:00.000Z")


def _today(res, key="export_kwh"):
    return res["days"]["2026-11-04"][key]


def _grid_before_window(res):
    return [f for f in res["forecast"] if f["mode"] == "grid" and f["t"] <= "2026-11-04T23:30"]


def _net(res, until="2026-11-05T00:00"):
    return sum(f["ek"] * f["rate"] / 100 - f["ik"] * f["ip"] / 100 for f in res["forecast"] if f["t"] <= until)


def test_day_floor():
    evening = [0.30 if 32 <= i < 38 else 0.10 for i in range(48)]
    kw = dict(soc=60, load=0.2, now=D0 + timedelta(hours=8))
    flat = _plan(evening * 2, **kw)
    assert min(f["soc"] for f in flat["forecast"] if f["mode"] == "export") >= 19.9
    day = _plan(evening * 2, day_reserve_soc=25, **kw)
    assert min(f["soc"] for f in day["forecast"] if f["mode"] == "export") >= 24.9  # daytime exports stop at 25%
    assert soc_at(day, "19:00") == pytest.approx(25, abs=0.5)
    assert day["forecast"][0]["fl"] == 25 and day["reserve_soc"] == 25 and day["night_reserve_soc"] == 20
    # after 22:25 the night floor applies: a late slot can take it down to 20%
    late = list(evening)
    late[45] = 0.30
    res = _plan(late * 2, day_reserve_soc=25, **kw)
    assert soc_at(res, "23:00") == pytest.approx(20, abs=0.5)
    assert next(f for f in res["forecast"] if f["t"] == "2026-11-04T23:00:00.000Z")["mode"] == "export"


def test_free_session_empties_first():
    # 10p export vs a 6.6p overnight refill (7.9p after losses): not worth 5p ...
    flat = [0.10] * 96
    kw = dict(soc=90, load=1.0, now=D0 + timedelta(hours=8))
    assert _plan(flat, **kw)["export_kwh_planned"] == 0
    # ... but a free-electricity session refills it for nothing: empty the battery ahead of it
    free = (D0 + timedelta(hours=13), D0 + timedelta(hours=16))
    res = _plan(flat, free_session=free, **kw)
    assert res["export_kwh_planned"] > 10
    assert any(f["mode"] == "export" for f in res["forecast"] if f["t"] <= "2026-11-04T13:00:00.000Z")
    # in the session it charges at full power, and that energy costs nothing
    session = [f for f in res["forecast"] if "2026-11-04T13:00:00.000Z" < f["t"] <= "2026-11-04T16:00:00.000Z"]
    assert session[0]["mode"] == "charge" and session[0]["kw"] == pytest.approx(10, abs=0.01)
    assert soc_at(res, "16:00") > soc_at(res, "13:00") + 30
    assert all(f["ip"] == 0 for f in session) and sum(f["ik"] for f in session) > 15
    day = res["days"]["2026-11-04"]
    assert day["import_cost"] == pytest.approx(
        sum(f["ik"] * f["ip"] / 100 for f in res["forecast"] if f["t"] <= "2026-11-05T00:00:00.000Z"), abs=0.01)


def test_export_margin():
    # 50% = 30 kWh; 11.5 h of 2 kW load reaches the window below the 20% floor: no surplus.
    # The energy the house would use below the floor is exported at 38p and replaced in the
    # 6.6p window instead, but not so much that the house buys at peak
    arb = _arb_plan()
    assert 1 < _today(arb) < 5
    assert not _grid_before_window(arb)
    assert arb["export_margin"] == 5
    # a margin no export can clear: nothing
    none = _arb_plan(export_margin=40)
    assert none["export_kwh_planned"] == 0
    # allowed to buy at peak (38p export vs 34.26p import): afterwards, then in advance
    peak = _arb_plan(export_margin=2, export_peak_import=True)
    assert _today(peak) > _today(arb) + 3
    assert any(f["mode"] == "hold" for f in peak["forecast"])  # 3.7p clears 2p: bought in advance too
    net = lambda r: sum(d["export_value"] - d["import_cost"] for d in r["days"].values())
    assert net(peak) > net(arb) > net(none)
    # buying at peak needs the export to beat it by the margin: 38 - 34.26 < 5
    assert _today(_arb_plan(export_peak_import=True)) == pytest.approx(_today(arb), abs=0.3)


def test_export_margin_surplus():
    # a full battery has surplus; keeping it saves 6.6p / (0.93 x 0.9) = 7.9p per kWh exported
    assert _today(_arb_plan(soc=100, exp_peak=0.10, exp_off=0.10)) == 0  # 10p: 2.1p gain
    full = _arb_plan(soc=100, exp_peak=0.14, exp_off=0.10)  # 14p: 6.1p gain
    assert _today(full) > 10
    assert not _grid_before_window(full)


def test_buy_in_advance():
    bought = lambda r: [f for f in r["forecast"] if f["mode"] in ("hold", "charge") and "2026-11-04T12:30" <= f["t"] <= "2026-11-04T23:30"]
    # 50p peak vs 34.26p import: buying afterwards isn't enough to export at full power, so
    # energy is bought in advance too, within 3 h (default) of the 16:00 export
    after = _arb_plan(exp_peak=0.50)
    both = _arb_plan(exp_peak=0.50, export_peak_import=True)
    assert not bought(after)
    assert bought(both)
    assert all("2026-11-04T13:30" <= f["t"] <= "2026-11-04T16:00:00.000Z" for f in bought(both))
    assert all(f["kw"] >= 0 and f["ik"] >= f["ld"] * 0.5 - 1e-6 for f in bought(both))  # house on grid
    assert _today(both) > _today(after) + 8
    assert _net(both) > _net(after) + 1
    # with no buying in advance (0 h) the house still buys afterwards
    assert _grid_before_window(_arb_plan(exp_peak=0.50, export_peak_import=True, buy_ahead_h=0))
    # 38p doesn't pay for the energy the export slot's own load needs above the floor
    assert not bought(_arb_plan(export_peak_import=True))


def test_buy_ahead_window():
    bought = lambda r: [f for f in r["forecast"] if f["mode"] in ("hold", "charge") and "2026-11-04T12:30" <= f["t"] <= "2026-11-04T23:30"]
    # nothing is bought more than buy_ahead_h before the export (16:00)
    for h in (1, 2, 3):
        r = _arb_plan(exp_peak=0.60, export_peak_import=True, buy_ahead_h=h)
        assert bought(r), h
        first = min(f["t"] for f in bought(r))  # slot end
        assert first >= f"2026-11-04T{16 - h:02d}:30", (h, first)
    # with only half an hour, what holding can't free is grid-charged right before the export
    short = _arb_plan(exp_peak=0.60, export_peak_import=True, buy_ahead_h=0.5)
    chg = [f for f in bought(short) if f["mode"] == "charge"]
    assert chg and all(f["t"] == "2026-11-04T16:00:00.000Z" for f in chg)
    assert chg[0]["kw"] > 0 and chg[0]["ik"] > chg[0]["ld"] * 0.5
    # 0 h: no buying in advance at all
    assert not bought(_arb_plan(exp_peak=0.60, export_peak_import=True, buy_ahead_h=0))


def test_export_before_dispatch():
    """The battery makes room for a daytime dispatch that refills it at the off-peak rate."""
    disp = [(D0 + timedelta(hours=7, minutes=30), D0 + timedelta(hours=8))]
    imp = [0.066 if (i < 11 or i >= 47 or i == 15) else 0.3426 for i in range(48)] * 2  # dispatch priced off-peak
    exp = [0.25 if 11 <= i < 15 else 0.10 for i in range(96)]  # 25p only before the dispatch
    kw = dict(soc=100, load=1.0, now=D0 + timedelta(hours=5, minutes=30), imp=imp, dispatches=disp, ready=D0 + timedelta(hours=8))
    before = lambda r: sum(f["ek"] for f in r["forecast"] if f["t"] <= "2026-11-04T07:30:00.000Z")
    assert before(_plan(exp, export_margin=40, **kw)) == 0
    # 25p export vs 6.6p refill (7.9p after losses): exports ahead of the dispatch, which refills it
    res = _plan(exp, **kw)
    assert before(res) > 3
    assert soc_at(res, "08:00") > soc_at(res, "07:30") + 8  # the dispatch refills 5 kWh (10 kW x 0.5 h)
    assert res["export_kwh_planned"] == pytest.approx(before(res), abs=0.1)  # 10p elsewhere doesn't pay
    # when it only needs part of that time, it exports at the last moment: the half hour before
    # the dispatch (a later replan would then see a cancelled dispatch before anything went out)
    busy = _plan(exp, **{**kw, "load": 3.0})
    pre = [f for f in busy["forecast"] if f["t"] <= "2026-11-04T07:30:00.000Z"]
    assert [f["t"][11:16] for f in pre if f["exp"] > 0] == ["07:30"]


NIGHT = dict(export_margin=5, charge_mode="spread", max_charge_kw=17, load=1.0)


def _window(res):
    return [f for f in res["forecast"] if "2026-11-04T23:30:00.000Z" < f["t"] <= "2026-11-05T05:30:00.000Z"]


def test_night_cycle():
    """Buy, sell and buy back: export inside the charge window, but always refill it by the finish-early time."""
    kw = dict(NIGHT, soc=30, now=D0 + timedelta(hours=20))
    exp = [0.18] * 144  # Agile Outgoing ~18p overnight vs 6.6p import
    plain = _plan(exp, **kw)
    assert not any(f["exp"] > 0 for f in _window(plain))
    cyc = _plan(exp, night_cycle=True, **kw)
    sold = sum(f["ek"] for f in _window(cyc))
    assert sold > 3  # 18p vs 6.6 / (0.93 x 0.9) = 7.9p: 10p over a 5p margin
    assert min(f["soc"] for f in _window(cyc) if f["exp"] > 0) >= 19.5  # stops at the night floor
    # bought back: full by the finish-early time (05:00), and nothing sold after it
    assert soc_at(cyc, "05:00", "2026-11-05") == pytest.approx(100, abs=0.5)
    assert soc_at(plain, "05:00", "2026-11-05") == pytest.approx(100, abs=0.5)
    assert not any(f["exp"] > 0 for f in _window(cyc) if f["t"] > "2026-11-05T05:00:00.000Z")
    assert _net(cyc, "2026-11-05T05:30") > _net(plain, "2026-11-05T05:30") + sold * 0.05


def test_night_cycle_inside_window():
    """Replanning during the night (every few minutes) keeps the window's own exports."""
    cyc = _plan([0.18] * 144, night_cycle=True, soc=45, now=D0 + timedelta(hours=24, minutes=40), **NIGHT)
    assert sum(f["ek"] for f in _window(cyc)) > 1
    assert soc_at(cyc, "05:00", "2026-11-05") == pytest.approx(100, abs=0.5)


def test_offpeak_window():
    w = lambda rates, disp=(): P.offpeak_window(rates, list(disp), TZ)
    h = lambda hh, mm=0: D0 + timedelta(hours=hh, minutes=mm)
    # today's rates only (before tomorrow's are published): 00:00-05:30 and 23:30-24:00 wrap round
    assert w(_rates(IOG)) == (23 * 3600 + 1800, 6 * 3600)
    assert w(_rates(IOG * 2)) == (23 * 3600 + 1800, 6 * 3600)
    # dispatches are priced off-peak too: a daytime one is too short, one right after the window
    # doesn't stretch it, and one inside it doesn't split it
    late = list(IOG)
    late[11] = late[15] = 0.066  # 05:30-06:00 and 07:30-08:00
    assert w(_rates(late), [(h(5, 30), h(6)), (h(7, 30), h(8))]) == (23 * 3600 + 1800, 6 * 3600)
    assert w(_rates(late)) == (23 * 3600 + 1800, 6 * 3600 + 1800)  # unless it isn't known as a dispatch
    # tomorrow's rates disagree (no 05:30 dispatch then): only half hours cheap every day count
    assert w(_rates(late + IOG)) == (23 * 3600 + 1800, 6 * 3600)
    # Agile-like prices and a flat rate: no window, the configured one applies
    assert w(_rates([0.20 + 0.01 * ((i * 7) % 5) for i in range(48)])) is None
    assert w(_rates([0.25] * 48)) is None
    assert w([]) is None


def test_only_the_final_dispatch_is_firm():
    """Exports count only on the dispatch slot before the ready time: others may move earlier or vanish."""
    imp = [0.066 if (i < 11 or i >= 47 or i in (12, 15)) else 0.3426 for i in range(48)] * 2  # dispatches off-peak
    exp = [0.25 if 11 <= i < 15 else 0.10 for i in range(96)]
    final = (D0 + timedelta(hours=7, minutes=30), D0 + timedelta(hours=8))
    extra = (D0 + timedelta(hours=6), D0 + timedelta(hours=6, minutes=30))
    kw = dict(soc=100, load=3.0, now=D0 + timedelta(hours=5, minutes=30), imp=imp)
    before = lambda r: sum(f["ek"] for f in r["forecast"] if f["t"] <= "2026-11-04T07:30:00.000Z")
    only_final = _plan(exp, dispatches=[final], ready=final[1], **kw)
    both = _plan(exp, dispatches=[extra, final], ready=final[1], **kw)
    assert before(only_final) > 1
    # the 06:00 dispatch isn't counted on: no more export than the final slot alone refills
    assert before(both) == pytest.approx(before(only_final), abs=0.3)
    # but the plan still charges in it
    assert next(f for f in both["forecast"] if f["t"] == "2026-11-04T06:30:00.000Z")["mode"] in ("iog", "hold")
    # without a ready time no dispatch is firm: nothing exported ahead of them
    assert before(_plan(exp, dispatches=[extra, final], **kw)) == 0

