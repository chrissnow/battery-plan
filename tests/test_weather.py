"""Weather-dependent load: model fit, forecast adjustment, sensors."""
from datetime import datetime, timedelta, timezone
import math
import random

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_plan import weather_load as W
from custom_components.battery_plan.const import DOMAIN

from .test_setup import NOW, SETTINGS, flat

T0 = datetime(2026, 10, 10, tzinfo=timezone.utc)


def synth(days=21, b=0.1, tb=14.0, noise=0.05, seed=1):
    """Hourly temps and a load with a per-hour baseline plus b * max(0, tb - T_smoothed)."""
    rnd = random.Random(seed)
    temps = []
    for i in range(days * 24):
        t = T0 + timedelta(hours=i)
        trend = 12 - 8 * (i / (days * 24))  # autumn cooling
        temps.append((t, trend + 4 * math.sin((t.hour - 9) / 24 * 2 * math.pi)))
    sm = W.smooth(temps)
    rows = []
    for (t, raw), (_, s) in zip(temps, sm):
        base = 0.6 + (0.8 if 17 <= t.hour <= 21 else 0.0)
        load = base + b * max(0.0, tb - s) + rnd.gauss(0, noise)
        rows.append(W.Row(t, t.hour, t.date().isoformat(), s, raw, load))
    return rows, sm


def test_fit_recovers_heating() -> None:
    rows, _ = synth()
    m = W.fit(rows, cooling=False, recent_days=7, now=rows[-1].t + timedelta(hours=1))
    assert m.status == "active"
    assert m.heat_kw_per_c == pytest.approx(0.1, abs=0.015)
    assert m.heat_below_c == pytest.approx(14.0, abs=1.0)
    assert m.cool_kw_per_c is None
    assert len(m.daily) == 21 and m.line_base_kwh is not None


def test_fit_rejects_flat_or_short_data() -> None:
    rows, _ = synth(b=0.0)
    m = W.fit(rows, cooling=False, recent_days=7, now=rows[-1].t)
    assert m.status == "not_enough_data" and "too little" in m.reason or "cold" in m.reason
    short, _ = synth(days=4)
    m = W.fit(short, cooling=False, recent_days=7, now=short[-1].t)
    assert m.status == "not_enough_data" and "more days" in m.reason


def test_fit_cooling() -> None:
    rnd = random.Random(2)
    temps = [(T0 + timedelta(hours=i), 20 + 8 * math.sin(i / 24 * 2 * math.pi) + (i / 100)) for i in range(21 * 24)]
    rows = []
    for (t, raw), (_, s) in zip(temps, W.smooth(temps)):
        rows.append(W.Row(t, t.hour, t.date().isoformat(), s, raw, 0.5 + 0.2 * max(0.0, s - 23) + rnd.gauss(0, 0.03)))
    m = W.fit(rows, cooling=True, recent_days=7, now=rows[-1].t)
    assert m.status == "active" and m.cool_kw_per_c == pytest.approx(0.2, abs=0.04)
    assert m.cool_above_c == pytest.approx(23, abs=1.0)


def test_slot_adjustments_shift_by_forecast() -> None:
    rows, sm = synth()
    now = rows[-1].t + timedelta(hours=1)
    m = W.fit(rows, cooling=False, recent_days=7, now=now)
    cold = [(now + timedelta(hours=h), -2.0) for h in range(0, 30)]
    slots = [now + timedelta(minutes=30 * i) for i in range(48)]
    adj = W.slot_adjustments(m, cold, sm[-1][1], slots, timezone.utc)
    # by the end of the day the smoothed temperature is ~-2: heating = b * (Tb + 2), more than typical
    last = adj[int(slots[-1].timestamp())]
    assert last[1] == pytest.approx(m.heat_kw_per_c * (m.heat_below_c + 2), abs=0.1)
    assert last[0] > 0.3 and last[3] == pytest.approx(-2.0)
    # first slots: still near the observed temperature, small adjustment
    assert abs(adj[int(slots[0].timestamp())][0]) < last[0]


async def test_weather_sensors_and_plan(hass, recorder_mock, octopus_states, freezer) -> None:
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from homeassistant.core import SupportsResponse
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    freezer.move_to(NOW)
    rows, _ = synth(days=21)
    shift = NOW.replace(minute=0) - (rows[-1].t + timedelta(hours=1))  # end the history at now
    temp_rows, load_rows = [], []
    for r in rows:
        temp_rows.append({"start": r.t + shift, "mean": r.raw_temp, "min": r.raw_temp, "max": r.raw_temp})
        load_rows.append({"start": r.t + shift, "mean": r.load * 1000, "min": 0, "max": 0})
    for sid, unit, cls, data in (("sensor.outdoor", "°C", "temperature", temp_rows), ("sensor.heat_pump", "W", "power", load_rows)):
        hass.states.async_set(sid, "1", {"unit_of_measurement": unit, "state_class": "measurement", "device_class": cls})
        meta = {"source": "recorder", "statistic_id": sid, "name": None, "unit_of_measurement": unit, "has_mean": True,
                "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "unit_class": cls}
        async_import_statistics(hass, meta, data)
    await async_wait_recording_done(hass)

    hass.states.async_set("weather.home", "cloudy", {"temperature": 3, "temperature_unit": "°C"})

    async def forecasts(call):
        return {"weather.home": {"forecast": [
            {"datetime": (NOW + timedelta(hours=h)).isoformat(), "temperature": 0.0} for h in range(-1, 48)]}}

    hass.services.async_register("weather", "get_forecasts", forecasts, supports_response=SupportsResponse.ONLY)
    opts = flat(SETTINGS) | {"weather_entity": "weather.home", "outdoor_temp_entity": "sensor.outdoor",
                             "weather_load_entities": ["sensor.heat_pump"], "weather_cooling": False, "weather_days": 21}
    entry = MockConfigEntry(domain=DOMAIN, title="Battery Plan", data={"name": "Battery Plan"}, options=opts)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    wm = a["weather_model"]
    assert wm["status"] == "active", wm
    assert wm["heating_kw_per_c"] == pytest.approx(0.1, abs=0.02)
    assert wm["temperature_source"] == "sensor.outdoor"
    f = a["forecast"]
    assert all("tf" in x for x in f[:40]) and f[30]["wh"] > 0.5
    sens = hass.states.get("sensor.battery_plan_heating_sensitivity")
    assert float(sens.state) == pytest.approx(0.1, abs=0.02) and sens.attributes["status"] == "active"
    tm = hass.states.get("sensor.battery_plan_weather_load_tomorrow")
    assert float(tm.state) > 10  # ~b*(Tb-0)*24 h at 0 degC
    assert tm.attributes["slots"] and tm.attributes["slots"][0]["temp"] == 0.0
    assert a["days"][(NOW + timedelta(days=1)).date().isoformat()]["weather_kwh"] == pytest.approx(float(tm.state), abs=0.5)


async def test_no_weather_entity_no_sensors(hass, recorder_mock, octopus_states, freezer) -> None:
    from .test_setup import setup

    await setup(hass, freezer, flat(SETTINGS))
    assert hass.states.get("sensor.battery_plan_heating_sensitivity") is None
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert "weather_model" not in a and "tf" not in a["forecast"][0]
