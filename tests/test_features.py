"""Day floor, pre-free price, overrides, SOC-tracking export and EV inhibit."""
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.battery_plan import planner as P

from .test_control import CUR, SP, control_opts, run, setup, val

TZ = ZoneInfo("Europe/London")


def _plan(expensive, load=0.2, soc=60, **cfg_kw):
    now = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
    d0 = datetime(2026, 11, 4, tzinfo=timezone.utc)
    vals = [0.30 if i in expensive else 0.10 for i in range(48)] + [0.10] * 48
    rates = [P.Rate(d0 + i * P.HALF, d0 + (i + 1) * P.HALF, v) for i, v in enumerate(vals)]
    cfg = P.PlannerConfig(capacity_kwh=60, reserve_soc=20, max_export_kw=10, min_export_price=12.5, charge_mode="max",
                          max_charge_kw=10, **cfg_kw)
    inp = P.PlannerInputs(now=now, tz=TZ, soc=soc, load_kw=[load] * 24, export_rates=rates)
    return P.plan(cfg, inp)


def soc_at(res, hhmm):
    t = f"2026-11-04T{hhmm}:00.000Z"
    return next(f["soc"] for f in res["forecast"] if f["t"] == t)


def test_day_floor():
    evening = set(range(32, 38))  # 16:00-19:00
    assert soc_at(_plan(evening), "19:00") < 23  # flat floor: exports to ~20% + load
    day = _plan(evening, day_reserve_soc=25)
    assert soc_at(day, "18:00") == pytest.approx(25, abs=0.2)  # daytime exports stop at 25%
    assert min(f["soc"] for f in day["forecast"] if f["mode"] == "export") >= 24.9
    assert day["forecast"][0]["fl"] == 25 and day["reserve_soc"] == 25 and day["night_reserve_soc"] == 20
    # after 22:25 the night floor applies: a late slot can take it down to 20%
    late = _plan(evening | {45}, day_reserve_soc=25)
    assert soc_at(late, "18:00") == pytest.approx(25, abs=0.2)
    assert soc_at(late, "23:00") == pytest.approx(20, abs=0.5)
    assert next(f for f in late["forecast"] if f["t"].endswith("23:00:00.000Z"))["mode"] == "export"


def test_pre_free_price():
    now = datetime(2026, 11, 4, 8, 0, tzinfo=timezone.utc)
    d0 = datetime(2026, 11, 4, tzinfo=timezone.utc)
    rates = [P.Rate(d0 + i * P.HALF, d0 + (i + 1) * P.HALF, 0.10) for i in range(96)]
    free = (datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc), datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc))  # 3 h x 10 kW: room down to 50%
    base = dict(capacity_kwh=60, reserve_soc=20, max_export_kw=10, min_export_price=12.5, charge_mode="max", max_charge_kw=10)
    inp = P.PlannerInputs(now=now, tz=TZ, soc=90, load_kw=[1.0] * 24, export_rates=rates, free_session=free)
    assert P.plan(P.PlannerConfig(**base), inp)["export_kwh_planned"] == 0  # 10p < 12.5p
    res = P.plan(P.PlannerConfig(**base, min_export_price_pre_free=9), inp)
    assert res["export_kwh_planned"] > 10  # empties ahead of the free session at 10p
    pre = [f for f in res["forecast"] if f["t"] <= "2026-11-04T13:00"]
    assert any(f["mode"] == "export" for f in pre)


async def test_overrides(hass, octopus_states, freezer) -> None:
    _, calls = await setup(hass, freezer, control_opts(limited_export_kw=4))
    sel = "select.battery_plan_override"
    assert val(hass, sel) == "auto"
    for opt, expect in (("hold", -50), ("zero", 0), ("limited_export", -4000), ("max_export", -11000), ("saving", -12000)):
        await hass.services.async_call("select", "select_option", {"entity_id": sel, "option": opt}, blocking=True)
        await hass.async_block_till_done()
        assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) == expect, opt
    ctl = hass.config_entries.async_entries("battery_plan")[0].runtime_data.controller
    await ctl.async_set_custom(-2500)
    await hass.services.async_call("select", "select_option", {"entity_id": sel, "option": "custom"}, blocking=True)
    await hass.async_block_till_done()
    assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) == -2500
    # export floor still applies to overrides
    hass.states.async_set("sensor.batt_soc", "19", {"device_class": "battery", "unit_of_measurement": "%"})
    await run(hass, freezer)
    assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) == -50
    assert calls == []  # control still off


async def test_tracking_export(hass, octopus_states, freezer) -> None:
    # 22:30 export slot: SOC 24.1 -> plan's SOC at 23:00, live load 1.5 kW (flat load, no load sensors)
    await setup(hass, freezer, control_opts())
    plan = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes["forecast"][0]
    assert plan["mode"] == "export"
    kw = (24.1 - max(plan["soc"], 20)) / 100 * 61.7 / 0.5 * 0.9 - 1.5
    assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) == pytest.approx(-kw * 1000, abs=60)
    assert "by" in hass.states.get("sensor.battery_plan_control_status").attributes["reason"]


async def test_ev_inhibit(hass, octopus_states, freezer) -> None:
    hass.states.async_set("input_boolean.zappi_allow", "on")
    on = async_mock_service(hass, "input_boolean", "turn_on")
    off = async_mock_service(hass, "input_boolean", "turn_off")
    await setup(hass, freezer, control_opts(ev_allow_entity="input_boolean.zappi_allow"))
    assert val(hass, "binary_sensor.battery_plan_ev_inhibit") == "on"  # exporting at 22:30
    assert not off  # control off: nothing switched
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_grid_setpoint_control"}, blocking=True)
    await hass.async_block_till_done()
    assert off and off[-1].data["entity_id"] == "input_boolean.zappi_allow"
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.battery_plan_grid_setpoint_control"}, blocking=True)
    await hass.async_block_till_done()
    assert on and on[-1].data["entity_id"] == "input_boolean.zappi_allow"  # allowed again on stop
