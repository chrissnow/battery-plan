"""Overrides, SOC-tracking export and EV inhibit (planner logic is in test_planner.py)."""
import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from .test_control import control_opts, run, setup, val


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
