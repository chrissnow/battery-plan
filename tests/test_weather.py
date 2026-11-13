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


def test_hour_weights() -> None:
    w = W.hour_weights([0.0] * 12 + [2.0] * 12)
    assert sum(w) / 24 == pytest.approx(1.0)
    assert w[0] == pytest.approx(W.WEIGHT_FLAT) and w[23] == pytest.approx(2 - W.WEIGHT_FLAT)
    assert W.hour_weights([1.0] * 48) == pytest.approx([1.0] * 24)  # half-hourly, flat
    assert W.hour_weights([0.0] * 48) is None and W.hour_weights([None] * 24) is None and W.hour_weights([1.0] * 5) is None


def night_synth(days=21, b=0.15, tb=12.0, noise=0.05, seed=3):
    """A heat pump that only runs 20:00-09:00 (0.8 kW plus b per degree below tb), off by day."""
    rnd = random.Random(seed)
    rows, _ = synth(days=days, b=0.0, noise=0.0)
    out = []
    for r in rows:
        on = r.hod >= 20 or r.hod < 9
        load = 0.8 + b * max(0.0, tb - r.temp) + rnd.gauss(0, noise) if on else 0.0
        out.append(W.Row(r.t, r.hod, r.day, r.temp, r.raw_temp, max(0.0, load)))
    return out


def test_weights_put_heating_where_the_heat_pump_runs() -> None:
    rows = night_synth()
    now = rows[-1].t + timedelta(hours=1)
    flat_m = W.fit(rows, cooling=False, recent_days=7, now=now)
    recent = [r for r in rows if r.t >= now - timedelta(days=7)]
    prof = [sum(r.load for r in recent if r.hod == h) / max(1, sum(1 for r in recent if r.hod == h)) for h in range(24)]
    m = W.fit(rows, cooling=False, recent_days=7, now=now, weights=W.hour_weights(prof))
    assert m.status == "active" and m.gain > flat_m.gain + 0.1
    # at 2 degC: the night hours carry the heating, the day hours (heat pump off) almost none
    assert m.heat_kw(2.0, 2) == pytest.approx(0.15 * 10, rel=0.2)
    assert m.heat_kw(2.0, 14) < 0.15 * m.heat_kw(2.0, 2)
    assert flat_m.heat_kw(2.0, 14) == flat_m.heat_kw(2.0, 2)  # the flat model spreads it evenly
    assert m.as_dict()["hour_weights"][14] == pytest.approx(W.WEIGHT_FLAT, abs=0.02)
    # a cold forecast moves the night slots, not the day ones
    cold = [(now + timedelta(hours=h), -2.0) for h in range(0, 50)]
    slots = [now + timedelta(minutes=30 * i) for i in range(96)]
    adj = W.slot_adjustments(m, cold, None, slots, timezone.utc)
    night = [v[0] for k, v in adj.items() if datetime.fromtimestamp(k, timezone.utc).hour in (2, 3)]
    day = [v[0] for k, v in adj.items() if datetime.fromtimestamp(k, timezone.utc).hour in (13, 14)]
    assert min(night) > 0.5 and max(day) < 0.15


async def test_weather_sensors_and_plan(hass, recorder_mock, octopus_states, freezer) -> None:
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from homeassistant.core import SupportsResponse
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    freezer.move_to(NOW)
    rows, _ = synth(days=21)
    shift = NOW.replace(minute=0) - (rows[-1].t + timedelta(hours=1))  # end the history at now
    temp_rows, load_rows, house_rows = [], [], []
    for r in rows:
        temp_rows.append({"start": r.t + shift, "mean": r.raw_temp, "min": r.raw_temp, "max": r.raw_temp})
        load_rows.append({"start": r.t + shift, "mean": r.load * 1000, "min": 0, "max": 0})
        house_rows.append({"start": r.t + shift, "mean": (r.load + 0.5) * 1000, "min": 0, "max": 0})  # heat pump + 0.5 kW
    for sid, unit, cls, data in (("sensor.outdoor", "°C", "temperature", temp_rows), ("sensor.heat_pump", "W", "power", load_rows),
                                 ("sensor.house", "W", "power", house_rows)):
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
    opts = flat(SETTINGS) | {"weather_entity": "weather.home", "outdoor_temp_entity": "sensor.outdoor", "load_entities": ["sensor.house"],
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
    tomorrow = a["days"][(NOW + timedelta(days=1)).date().isoformat()]
    assert tomorrow["weather_kwh"] == pytest.approx(float(tm.state), abs=0.5)
    # dedicated heating sensor, part of the house load: each slot splits into heating and the rest
    assert len(wm["hour_weights"]) == 24
    assert all("ht" in x and x["ld"] - x["ht"] == pytest.approx(0.5, abs=0.02) for x in f[:40])
    assert tomorrow["heating_kwh"] > tomorrow["weather_kwh"]


async def test_no_weather_entity_no_sensors(hass, recorder_mock, octopus_states, freezer) -> None:
    from .test_setup import setup

    await setup(hass, freezer, flat(SETTINGS))
    assert hass.states.get("sensor.battery_plan_heating_sensitivity") is None
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert "weather_model" not in a and "tf" not in a["forecast"][0]
