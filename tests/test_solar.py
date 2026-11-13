"""Solar: forecast maths (pure) and the energy-platform forecast through the coordinator."""
from datetime import datetime, timedelta, timezone
import math
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from custom_components.battery_plan import solar as S

TZ = ZoneInfo("Europe/London")
D = datetime(2026, 6, 4, tzinfo=timezone.utc)


def test_slot_kw_splits_hours() -> None:
    out = S.slot_kw({D + timedelta(hours=10): 3000.0, D + timedelta(hours=11, minutes=20): 1000.0})
    h10, h11 = int((D + timedelta(hours=10)).timestamp()), int((D + timedelta(hours=11)).timestamp())
    assert out[h10] == out[h10 + 1800] == pytest.approx(3.0)
    assert out[h11] == out[h11 + 1800] == pytest.approx(1.0)  # off-hour start goes to its hour
    assert S.daily_kwh(out, TZ) == {"2026-06-04": pytest.approx(4.0)}
    assert S.kwh_between(out, D + timedelta(hours=10, minutes=15), D + timedelta(hours=11)) == pytest.approx(2.25)


def test_calibrate_bias_and_dull() -> None:
    assert S.calibrate({"2026-06-01": (10.0, 8.0)}) == (1.0, S.DEFAULT_DULL, 1)  # too few days
    ratios = [0.4, 0.7, 0.8, 0.8, 0.9, 0.9, 1.0, 1.0, 1.1, 1.2]
    hist = {f"2026-05-{i + 1:02d}": (20.0, 20.0 * r) for i, r in enumerate(ratios)}
    hist["2026-05-20"] = (0.2, 5.0)  # tiny forecast: ignored
    hist["2026-05-21"] = (20.0, None)  # no actual: ignored
    bias, dull, n = S.calibrate(hist)
    assert n == 10 and bias == pytest.approx(0.9)
    # 20th percentile of the ratios (0.7 + 0.8 x 0.1 = 0.78) over the bias
    assert dull == pytest.approx(0.78 / 0.9, abs=1e-6)
    b2, d2, _ = S.calibrate({f"d{i}": (10.0, 30.0) for i in range(6)})
    assert b2 == S.BIAS_RANGE[1] and d2 == S.DULL_RANGE[1]


def test_nowcast_weighted_by_day_passed() -> None:
    assert S.nowcast(0.1, 0.0, 20.0) == 1.0  # too early to tell
    assert S.nowcast(10.0, 5.0, 20.0) == pytest.approx(0.75)  # half the day at half: 1 + 0.5 x -0.5
    assert S.nowcast(20.0, 0.0, 20.0) == pytest.approx(S.NOWCAST_RANGE[0])
    assert S.nowcast(5.0, 50.0, 10.0) == pytest.approx(1.25)  # ratio capped at 1.5, half weight


def test_apply_scales_rest_of_today_only() -> None:
    now = D + timedelta(hours=12)
    slots = {int((D + timedelta(hours=h)).timestamp()): 2.0 for h in (13, 37)}
    out = S.apply(slots, 0.9, 0.5, now, TZ)
    assert out[int((D + timedelta(hours=13)).timestamp())] == pytest.approx(0.9)
    assert out[int((D + timedelta(hours=37)).timestamp())] == pytest.approx(1.8)


# ---------- through Home Assistant ----------

def _wh_hours(start: datetime, days: int = 2, peak_w: float = 6000.0) -> dict[str, float]:
    """A clear-sky-ish curve, 08:00-16:00 UTC (November), as the energy platform returns it."""
    out = {}
    day0 = start.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(days):
        for h in range(8, 16):
            t = day0 + timedelta(days=d, hours=h)
            out[t.isoformat()] = peak_w * math.sin(math.pi * (h - 7.5) / 8) ** 2
    return out


async def _fake_solar(hass, forecast: dict[str, float]):
    from pytest_homeassistant_custom_component.common import MockConfigEntry, MockModule, mock_integration, mock_platform

    calls = []

    async def async_get_solar_forecast(hass, entry_id):
        calls.append(entry_id)
        return {"wh_hours": forecast}

    mock_integration(hass, MockModule("fakesolar"))
    mock_platform(hass, "fakesolar.energy", SimpleNamespace(async_get_solar_forecast=async_get_solar_forecast))
    e = MockConfigEntry(domain="fakesolar", title="Solar", entry_id="solar1")
    e.add_to_hass(hass)
    return calls


async def test_forecast_reaches_the_plan(hass, recorder_mock, octopus_states, freezer) -> None:
    from .test_setup import NOW, SETTINGS, flat, setup

    calls = await _fake_solar(hass, _wh_hours(NOW + timedelta(days=1)))
    await setup(hass, freezer, flat(SETTINGS) | {"pv_forecast_entry": "solar1", "pv_scale": 0.8})
    assert calls == ["solar1"]
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    sol = a["solar"]
    assert sol["status"] == "forecast" and sol["bias"] == pytest.approx(0.8)
    tomorrow = (NOW + timedelta(days=1)).date().isoformat()
    assert sol["forecast_kwh"][tomorrow] == pytest.approx(0.8 * sum(_wh_hours(NOW + timedelta(days=1), 1).values()) / 1000, abs=0.1)
    assert a["days"][tomorrow]["pv_kwh"] > 10
    assert any(f.get("pv", 0) > 3 for f in a["forecast"])
    assert a["plan_target_soc"] <= 100 and a["target_scores"]


async def test_history_calibrates(hass, recorder_mock, octopus_states, freezer, hass_storage) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.battery_plan.const import DOMAIN

    from .test_setup import NOW, SETTINGS, flat

    await _fake_solar(hass, _wh_hours(NOW + timedelta(days=1)))
    days = {(NOW - timedelta(days=i)).date().isoformat(): {"fc": 20.0, "actual": 20.0 * r}
            for i, r in enumerate([0.6, 0.7, 0.8, 0.8, 0.9, 1.0], start=1)}
    hass_storage[f"{DOMAIN}.bp1.pv"] = {"version": 1, "key": f"{DOMAIN}.bp1.pv", "data": {"days": days}}
    hass.states.async_set("sensor.pv_power", "0", {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"})
    freezer.move_to(NOW)
    entry = MockConfigEntry(domain=DOMAIN, title="Battery Plan", data={"name": "Battery Plan"}, entry_id="bp1",
                            options=flat(SETTINGS) | {"pv_forecast_entry": "solar1", "pv_entity": "sensor.pv_power"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sol = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes["solar"]
    assert sol["status"] == "calibrated" and sol["history_days"] == 6
    assert sol["bias"] == pytest.approx(0.8)
    assert sol["dull"] == pytest.approx(0.7 / 0.8, abs=0.01)  # 20th pct of 0.6..1.0 = 0.7
    # tomorrow's forecast was stored (before 06:00 that day) for the history
    stored = hass_storage[f"{DOMAIN}.bp1.pv"]["data"]["days"]
    assert stored[(NOW + timedelta(days=1)).date().isoformat()]["fc"] > 10


async def test_no_solar_entry_no_pv_keys(hass, recorder_mock, octopus_states, freezer) -> None:
    from .test_setup import SETTINGS, flat, setup

    await setup(hass, freezer, flat(SETTINGS))
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert "solar" not in a and "plan_target_soc" not in a and "pv" not in a["forecast"][0]
