"""Compare the Python planner with the original Node-RED planner.js."""
import json, subprocess, sys, pathlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE.parents[1] / "custom_components" / "battery_plan"))
import planner as P  # noqa: E402

TODAY = [0.1815,0.1949,0.1991,0.1937,0.1892,0.1877,0.1911,0.1837,0.187,0.1829,0.2047,0.192,0.2099,0.2209,0.2313,0.2364,0.2281,0.2286,0.191,0.1969,0.2004,0.2003,0.1878,0.1872,0.1864,0.1842,0.1798,0.1798,0.1864,0.1863,0.2192,0.2311,0.2904,0.3049,0.2996,0.2972,0.2998,0.2952,0.2291,0.218,0.1936,0.1978,0.1987,0.1953,0.1888,0.1775,0.1698,0.1726]
TMRW = [0.1678,0.1723,0.1745,0.172,0.1731,0.1706,0.1715,0.1694,0.1739,0.1692,0.1714,0.1737,0.2005,0.2099,0.2089,0.2099,0.2099,0.2099,0.1864,0.1845,0.1826,0.1786,0.1754,0.174,0.1773,0.1797,0.1845,0.1883,0.1986,0.1977,0.2133,0.225,0.295,0.3021,0.296,0.2949,0.2996,0.2953,0.2216,0.2138,0.1901,0.1944,0.1953,0.1859,0.1826,0.1733]
TZ = ZoneInfo("Europe/London")
V = 51.2

def scenario(**kw):
    sc = dict(now="2026-11-03T22:30:00Z", soc=24.1, reserve=20, min_soc=5, target=100, max_export_kw=11, saving_kw=12,
              min_price=12.5, start_secs=84600, dur_secs=21600, max_a=344, rates_start="2026-11-03T00:00:00Z",
              export_gbp=TODAY + TMRW, load_w=[1500] * 24, dispatches=[{"start": "2026-11-04T07:30:00+00:00", "end": "2026-11-04T08:00:00+00:00"}])
    sc.update(kw)
    return sc

SCENARIOS = {
    "tonight": scenario(),
    "no_dispatch_profile": scenario(dispatches=[], load_w=[600]*6 + [1200]*12 + [2500]*4 + [900]*2),
    "morning_full": scenario(now="2026-11-04T08:10:00Z", soc=96),
    "saving_session": scenario(now="2026-11-04T08:00:00Z", soc=96, saving=[63000, 66600]),
    "mid_charge": scenario(now="2026-11-04T02:10:00Z", soc=55),
    "target_90": scenario(target=90, now="2026-11-03T21:00:00Z", soc=60),
    "saturday_long": scenario(now="2026-11-07T22:00:00Z", soc=30, rates_start="2026-11-07T00:00:00Z"),
}

def run_py(sc):
    now = datetime.fromisoformat(sc["now"].replace("Z", "+00:00"))
    rs = datetime.fromisoformat(sc["rates_start"].replace("Z", "+00:00"))
    rates = [P.Rate(rs + i * P.HALF, rs + (i + 1) * P.HALF, v) for i, v in enumerate(sc["export_gbp"])]
    saving = None
    if sc.get("saving"):
        mid = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        s, e = (mid + timedelta(seconds=x) for x in sc["saving"])
        if e <= now:
            s, e = s + timedelta(days=1), e + timedelta(days=1)
        saving = (s, e)
    cfg = P.PlannerConfig(capacity_kwh=1206 * V / 1000, target_soc=sc["target"], reserve_soc=sc["reserve"], min_soc=sc["min_soc"],
                          charge_start=sc["start_secs"], charge_duration=sc["dur_secs"], charge_mode="spread",
                          max_charge_kw=sc["max_a"] * V / 1000, min_charge_kw=10 * V / 1000, makeup_kw=5 * V / 1000,
                          finish_early_min=15, finish_early_full_min=30, long_balance_weekday=5, long_balance_min=60,
                          max_export_kw=sc["max_export_kw"], saving_export_kw=sc["saving_kw"], min_export_price=sc["min_price"],
                          discharge_eff=0.9, charge_eff=0.93)
    disp = [(datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"])) for d in sc["dispatches"]]
    inp = P.PlannerInputs(now=now, tz=TZ, soc=sc["soc"], load_kw=[w / 1000 for w in sc["load_w"]], export_rates=rates,
                          dispatches=disp, saving_session=saving)
    return P.plan(cfg, inp)

def run_js(sc, name):
    f = HERE / f".{name}.json"
    f.write_text(json.dumps(sc))
    out = subprocess.run(["node", str(HERE / "run_js.js"), str(f)], capture_output=True, text=True, check=True,
                         env={"TZ": "Europe/London", "PATH": "/usr/bin:/usr/local/bin"})
    f.unlink()
    return json.loads(out.stdout)

def compare(name):
    sc = SCENARIOS[name]
    py, js = run_py(sc), run_js(sc, name)
    errs = []
    assert len(py["forecast"]) == len(js["forecast"])
    for a, b in zip(py["forecast"], js["forecast"]):
        if a["t"] != b["t"] or a["mode"] != b["mode"]:
            errs.append(f'{a["t"]} mode py={a["mode"]} js={b["mode"]}')
        for k in ("soc", "kw", "exp", "ik", "ek", "rate"):
            if (a[k] is None) != (b[k] is None) or (a[k] is not None and abs(a[k] - b[k]) > 0.02 + 1e-9):
                errs.append(f'{a["t"]} {k} py={a[k]} js={b[k]}')
    for k in ("soc_at_next_charge", "min_soc_ahead", "export_kwh_planned", "next_action"):
        if py[k] != js[k]:
            errs.append(f"{k} py={py[k]} js={js[k]}")
    pb = [(b["mode"], b["start"], b["end"], b["end_soc"]) for b in py["plan"]]
    jb = [(b["mode"], b["start"], b["end"], b["end_soc"]) for b in js["plan"]]
    if pb != jb:
        errs.append(f"plan py={pb}\n      js={jb}")
    return errs, py

def test_parity():
    bad = {}
    for name in SCENARIOS:
        errs, _ = compare(name)
        if errs:
            bad[name] = errs[:8]
    assert not bad, json.dumps(bad, indent=1)

if __name__ == "__main__":
    for name in SCENARIOS:
        errs, py = compare(name)
        print(f"{name:22s} {'OK' if not errs else 'DIFF'}  next={py['next_action']} soc@charge={py['soc_at_next_charge']} export={py['export_kwh_planned']}kWh")
        for e in errs[:8]:
            print("   ", e)
