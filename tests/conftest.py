"""Fixtures for Battery Plan tests."""
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    yield


def rates(start: datetime, values: list[float]) -> list[dict]:
    return [{"start": start + timedelta(minutes=30 * i), "end": start + timedelta(minutes=30 * (i + 1)),
             "value_inc_vat": v, "is_capped": False} for i, v in enumerate(values)]


def iog_rates(day: datetime) -> list[float]:
    out = []
    for i in range(48):
        h = i / 2
        out.append(0.066 if h < 5.5 or h >= 23.5 or h == 7.5 else 0.34264)
    return out


@pytest.fixture
async def octopus_states(hass):
    """Victron SOC + Octopus Energy integration entities, for 2026-11-03/04 (UTC == local in Nov)."""
    await hass.config.async_set_time_zone("Europe/London")
    d0 = datetime(2026, 11, 3, tzinfo=timezone.utc)
    d1 = d0 + timedelta(days=1)
    exp_today = [0.18] * 32 + [0.30] * 6 + [0.20] * 10
    exp_tmrw = [0.17] * 32 + [0.30] * 6 + [0.19] * 8
    hass.states.async_set("sensor.batt_soc", "24.1", {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set("event.imp_current", "x", {"rates": rates(d0, iog_rates(d0))})
    hass.states.async_set("event.imp_next", "x", {"rates": rates(d1, iog_rates(d1))})
    hass.states.async_set("event.exp_current", "x", {"rates": rates(d0, exp_today)})
    hass.states.async_set("event.exp_next", "x", {"rates": rates(d1, exp_tmrw)})
    hass.states.async_set("sensor.standing", "0.507455", {"unit_of_measurement": "GBP"})
    hass.states.async_set("binary_sensor.dispatching", "off", {"planned_dispatches": [
        {"start": datetime(2026, 11, 4, 7, 30, tzinfo=timezone.utc), "end": datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc), "source": "SMART"}]})
    hass.states.async_set("select.target_time", "08:00", {"raw_value": "08:00:00"})
    hass.states.async_set("sensor.grid_power", "2000", {"device_class": "power", "unit_of_measurement": "W"})
