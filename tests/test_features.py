"""Overrides, SOC-tracking export and EV inhibit (planner logic is in test_planner.py)."""
from datetime import datetime, timezone

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from .test_control import CUR, control_opts, run, setup, val


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


async def test_export_held_for_last_minutes(hass, octopus_states, freezer) -> None:
    """Per-half-hour tracking, but the export is held for the last 5 minutes (default) rather than
    tracked to the end, where a small SOC difference over a few minutes tapers or surges."""
    hass.states.async_set("sensor.batt_soc", "60", {"device_class": "battery", "unit_of_measurement": "%"})
    await setup(hass, freezer, control_opts(), at=datetime(2026, 11, 3, 22, 50, tzinfo=timezone.utc))
    sp = float(val(hass, "sensor.battery_plan_target_grid_setpoint"))
    assert sp < -100 and "by 23:00" in hass.states.get("sensor.battery_plan_control_status").attributes["reason"]
    # 22:56, SOC lower than tracking would expect: held, not tapered
    hass.states.async_set("sensor.batt_soc", "57", {"device_class": "battery", "unit_of_measurement": "%"})
    await run(hass, freezer, 6 * 60)
    st = hass.states.get("sensor.battery_plan_control_status").attributes
    assert "holding" in st["reason"], st["reason"]
    assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) == pytest.approx(sp, abs=1)
    # with 0 it tracks to the end again
    hass.config_entries.async_update_entry(hass.config_entries.async_entries("battery_plan")[0],
                                           options={**control_opts(), "export_hold_minutes": 0})
    await hass.async_block_till_done()
    await run(hass, freezer)
    assert "holding" not in hass.states.get("sensor.battery_plan_control_status").attributes["reason"]


async def test_current_deadband(hass, octopus_states, freezer) -> None:
    """Charge current changes smaller than the deadband (default 10 A) aren't written."""
    from custom_components.battery_plan.const import DEFAULTS

    assert DEFAULTS["current_deadband_a"] == 10
    _, calls = await setup(hass, freezer, control_opts(current_deadband_a=10),
                           at=datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc))
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_charge_current_control"}, blocking=True)
    await hass.async_block_till_done()
    cur = [c for c in calls if c.data["entity_id"] == CUR]
    assert cur, "spread current written on turning on"
    first = cur[-1].data["value"]
    hass.states.async_set(CUR, str(first), {"min": 0, "max": 344})
    n = len(cur)
    # nudge the SOC: the spread moves by a few amps, inside the deadband
    hass.states.async_set("sensor.batt_soc", "25", {"device_class": "battery", "unit_of_measurement": "%"})
    await run(hass, freezer)
    target = float(val(hass, "sensor.battery_plan_target_charge_current"))
    assert 0.5 < abs(target - first) < 10  # the target moved ...
    assert len([c for c in calls if c.data["entity_id"] == CUR]) == n  # ... but wasn't written


async def test_ev_inhibit_in_window(hass, octopus_states, freezer) -> None:
    """Exporting inside the charge window (buy-sell-buy): the EV is inhibited, unless an IOG dispatch
    is under way, so the car's own off-peak charging isn't blocked."""
    t = lambda h, m: datetime(2026, 11, 4, h, m, tzinfo=timezone.utc)
    await setup(hass, freezer, control_opts(), at=t(0, 10))
    await hass.services.async_call("select", "select_option", {"entity_id": "select.battery_plan_override", "option": "max_export"}, blocking=True)
    await hass.async_block_till_done()
    assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) < -200
    assert val(hass, "binary_sensor.battery_plan_ev_inhibit") == "on"
    # a dispatch from 00:00 to 01:00: the car is meant to charge, so it isn't inhibited
    hass.states.async_set("binary_sensor.dispatching", "on", {"planned_dispatches": [
        {"start": t(0, 0), "end": t(1, 0), "source": "SMART"}]})
    await hass.async_block_till_done()
    await run(hass, freezer)
    assert val(hass, "binary_sensor.battery_plan_ev_inhibit") == "off"


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
