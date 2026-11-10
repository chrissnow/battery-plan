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
    sessions = {k: cfg.pop(k) for k in ("dispatches", "free_session", "free_sessions", "free_used_kwh", "power_up_sessions", "saving_session", "ready") if k in cfg}
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
    res = _plan(flat, free_session=free, free_cap_kwh=0, **kw)  # no cap
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


def _session(res, a="13:00", b="16:00"):
    return [f for f in res["forecast"] if f"2026-11-04T{a}:00.000Z" < f["t"] <= f"2026-11-04T{b}:00.000Z"]


def test_free_session_cap_at_peak():
    """At peak a session's free import stops at the cap (16 kWh, house load included)."""
    free = (D0 + timedelta(hours=13), D0 + timedelta(hours=16))
    res = _plan([0.10] * 96, soc=20, load=1.0, now=D0 + timedelta(hours=8), free_session=free)
    s = _session(res)
    assert sum(f["ik"] for f in s if f["ip"] == 0) == pytest.approx(16, abs=0.05)
    assert s[0]["mode"] == "charge" and s[0]["kw"] == pytest.approx(10, abs=0.01)
    # 16 kWh = 1.5 h of 10 kW + 1 kW house, then the battery runs the house again
    assert [f["mode"] for f in s] == ["charge", "charge", "charge", "self", "self", "self"]
    assert all(f["ik"] == 0 for f in s[3:])
    assert soc_at(res, "16:00") < soc_at(res, "14:30")
    sess = res["free_sessions"][0]
    assert sess["until"].startswith("2026-11-04T14:30") and sess["free_kwh"] == pytest.approx(16, abs=0.05)
    # import already used in a session in progress counts against it
    mid = _plan([0.10] * 96, soc=20, load=1.0, now=D0 + timedelta(hours=13, minutes=30), free_session=free, free_used_kwh=10)
    # 13:30-14:00 at 10 kW takes 5.9 kWh; the 0.1 kWh left is less than the house uses, so it stops
    assert [f["mode"] for f in _session(mid, "13:30")][:2] == ["charge", "self"]
    assert mid["free_sessions"][0]["free_kwh"] == pytest.approx(5.88, abs=0.01)
    assert mid["free_sessions"][0]["used_kwh"] == 10
    # past the cap, import is paid at the rate it's billed at
    over = _plan([0.10] * 96, soc=20, load=1.0, now=D0 + timedelta(hours=8), free_session=free, free_cap_kwh=4)
    s = _session(over)
    assert s[0]["mode"] == "charge" and s[0]["ik"] == pytest.approx(4, abs=0.01) and s[0]["ip"] == 0
    assert s[1]["mode"] == "self" and s[1]["ik"] == 0


def test_free_session_cap_off_peak():
    """Off-peak (an IOG dispatch for the car, priced off-peak) it carries on past the cap."""
    free = (D0 + timedelta(hours=13), D0 + timedelta(hours=16))
    imp = [0.066 if (i < 11 or i >= 47 or 26 <= i < 32) else 0.3426 for i in range(48)] * 3
    disp = [(D0 + timedelta(hours=13), D0 + timedelta(hours=16))]
    res = _plan([0.10] * 96, soc=10, load=1.0, now=D0 + timedelta(hours=8), imp=imp, dispatches=disp,
                free_session=free, free_cap_kwh=4)
    s = _session(res)
    assert all(f["mode"] == "charge" for f in s)
    assert s[0]["ip"] < 6.5 and s[1]["ip"] == pytest.approx(6.6, abs=0.01)  # paid off-peak past the cap
    assert res["free_sessions"][0]["until"].startswith("2026-11-04T16:00")
    day = res["days"]["2026-11-04"]
    assert day["import_cost"] == pytest.approx(
        sum(f["ik"] * f["ip"] / 100 for f in res["forecast"] if f["t"] <= "2026-11-05T00:00:00.000Z"), abs=0.01)


def test_free_sessions_each_have_a_cap():
    """Two sessions in a day (12:00-13:00, 14:00-15:00): 16 kWh each."""
    wins = [(D0 + timedelta(hours=12), D0 + timedelta(hours=13)), (D0 + timedelta(hours=14), D0 + timedelta(hours=15))]
    res = _plan([0.10] * 96, soc=10, load=1.0, now=D0 + timedelta(hours=8), free_sessions=wins, free_cap_kwh=7)
    assert [s["free_kwh"] for s in res["free_sessions"]] == pytest.approx([7, 7], abs=0.05)
    assert res["free_session"][0].startswith("2026-11-04T12:00")
    # back to back (12:00-13:00, 13:00-14:00): still one cap per event (hour), nothing lost at 13:00
    b2b = [(D0 + timedelta(hours=12), D0 + timedelta(hours=13)), (D0 + timedelta(hours=13), D0 + timedelta(hours=14))]
    res = _plan([0.10] * 96, soc=10, load=1.0, now=D0 + timedelta(hours=8), free_sessions=b2b, free_cap_kwh=7)
    assert [s["free_kwh"] for s in res["free_sessions"]] == pytest.approx([7, 7], abs=0.05)
    assert all(f["mode"] == "charge" and f["ip"] == 0 for f in _session(res, "12:00", "14:00"))
    # in the second hour, the import used so far counts from 13:00 (the coordinator measures it per event)
    mid = _plan([0.10] * 96, soc=10, load=1.0, now=D0 + timedelta(hours=13, minutes=10), free_sessions=b2b,
                free_cap_kwh=7, free_used_kwh=2)
    assert mid["free_sessions"][0]["free_kwh"] == pytest.approx(5, abs=0.05)


def test_power_up_has_no_cap():
    """Power Ups are free with no cap; free electricity sessions keep theirs."""
    pu = [(D0 + timedelta(hours=13), D0 + timedelta(hours=16))]
    res = _plan([0.10] * 96, soc=10, load=1.0, now=D0 + timedelta(hours=8), power_up_sessions=pu)
    s = _session(res)
    assert all(f["mode"] == "charge" and f["ip"] == 0 for f in s)
    assert sum(f["ik"] for f in s) > 16 + 5
    assert res["free_sessions"][0]["kind"] == "power_up" and res["free_sessions"][0]["cap_kwh"] is None
    # with a capped free session earlier the same day, each keeps its own rule
    free = [(D0 + timedelta(hours=10), D0 + timedelta(hours=11))]
    both = _plan([0.10] * 96, soc=10, load=1.0, now=D0 + timedelta(hours=8), free_sessions=free, power_up_sessions=pu,
                 free_cap_kwh=4)
    assert [(x["kind"], x["cap_kwh"]) for x in both["free_sessions"]] == [("free", 4), ("power_up", None)]
    assert both["free_sessions"][0]["free_kwh"] == pytest.approx(4, abs=0.05)
    assert both["free_sessions"][1]["free_kwh"] > 16
    # the same hour in both calendars (as the Octopus integration lists them): it's the capped one
    dup = _plan([0.10] * 96, soc=10, load=1.0, now=D0 + timedelta(hours=8), free_sessions=pu, power_up_sessions=pu)
    assert [(x["kind"], x["cap_kwh"]) for x in dup["free_sessions"]] == [("free", 16)]
    assert dup["free_sessions"][0]["free_kwh"] == pytest.approx(16, abs=0.05)


def test_inverter_limit_includes_house_load():
    """The inverter's AC output feeds the house first: export is planned at no more than its
    maximum less the house load (12.5 kW Quattro, ~2 kW evening load: ~10.5 kW, not 11)."""
    free = _arb_plan(soc=100, exp_peak=0.40)
    capped = _arb_plan(soc=100, exp_peak=0.40, inverter_kw=10)  # max export 10 kW, load 2 kW
    peak = lambda r: [f for f in r["forecast"] if "2026-11-04T16:30" <= f["t"] <= "2026-11-04T19:00:00.000Z"]
    assert max(f["exp"] for f in peak(free)) == pytest.approx(10, abs=0.01)
    assert max(f["exp"] for f in peak(capped)) == pytest.approx(8, abs=0.01)
    assert all(f["exp"] + f["ld"] <= 10 + 0.01 for f in capped["forecast"])


def test_unpriced_import_not_made_up():
    """Past the published import rates nothing is invented: that import is reported as unpriced."""
    res = _plan([0.10] * 96, imp=IOG, now=D0 + timedelta(hours=20))  # import rates for 4 Nov only
    tmrw = res["days"]["2026-11-05"]
    assert tmrw["priced"] is False and tmrw["unpriced_kwh"] == pytest.approx(tmrw["import_kwh"], abs=0.05)
    assert tmrw["import_cost"] == 0
    assert "unpriced_kwh" not in res["days"]["2026-11-04"]


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

