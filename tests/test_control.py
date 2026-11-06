"""Control (dry run + switch) and battery action tests."""
from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed, async_mock_service

from homeassistant.core import HomeAssistant

from custom_components.battery_plan.const import DOMAIN

from .test_setup import NOW, SETTINGS, flat

SP = "number.ess_setpoint"
CUR = "number.dvcc_current"


def control_opts(**extra):
    o = flat(SETTINGS) | {
        "grid_setpoint_entity": SP, "charge_current_entity": CUR, "idle_setpoint_w": -50, "charge_setpoint_w": 0,
        "setpoint_deadband_w": 100, "battery_voltage": 51.2, "stale_minutes": 15,
        "battery_power_entity": "sensor.batt_power", "battery_power_invert": False,
    }
    o.update(extra)
    return o


async def setup(hass, freezer, opts, at=NOW):
    freezer.move_to(at)
    hass.states.async_set(SP, "-50", {"min": -15000, "max": 15000})
    hass.states.async_set(CUR, "344", {"min": 0, "max": 344})
    hass.states.async_set("sensor.batt_power", "-1500", {"device_class": "power", "unit_of_measurement": "W"})
    entry = MockConfigEntry(domain=DOMAIN, title="Battery Plan", data={"name": "Battery Plan"}, options=opts)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # after setup: our number platform registers the real number.set_value
    calls = async_mock_service(hass, "number", "set_value")
    return entry, calls


def val(hass, e):
    return hass.states.get(e).state


async def run(hass, freezer, secs=31):
    freezer.tick(timedelta(seconds=secs))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_dry_run_writes_nothing(hass: HomeAssistant, octopus_states, freezer) -> None:
    _, calls = await setup(hass, freezer, control_opts())
    assert val(hass, "switch.battery_plan_control") == "off"
    assert val(hass, "sensor.battery_plan_control_status") == "disabled"
    sp = float(val(hass, "sensor.battery_plan_target_grid_setpoint"))
    assert sp < -100  # exporting now (22:30, SOC 24% above 20% floor)
    assert float(val(hass, "sensor.battery_plan_target_charge_current")) == pytest.approx(17.6 * 1000 / 51.2, abs=0.2)
    await run(hass, freezer)
    assert calls == []


async def test_enable_writes_with_deadband_and_revert(hass, octopus_states, freezer) -> None:
    _, calls = await setup(hass, freezer, control_opts())
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_control"}, blocking=True)
    await hass.async_block_till_done()
    assert val(hass, "sensor.battery_plan_control_status") == "active"
    written = {c.data["entity_id"]: c.data["value"] for c in calls}
    assert written[SP] == float(val(hass, "sensor.battery_plan_target_grid_setpoint")) < -100
    assert CUR not in written  # already 344 A
    n = len(calls)
    hass.states.async_set(SP, str(written[SP]), {"min": -15000, "max": 15000})
    await run(hass, freezer)
    assert len(calls) == n  # unchanged -> no rewrite
    # live floor: SOC drops to the export floor -> idle setpoint
    hass.states.async_set("sensor.batt_soc", "19.5", {"device_class": "battery", "unit_of_measurement": "%"})
    await run(hass, freezer)
    assert calls[-1].data == {"entity_id": SP, "value": -50.0}
    assert "floor" in hass.states.get("sensor.battery_plan_control_status").attributes["reason"]
    # turning off reverts
    hass.states.async_set(SP, "-50", {"min": -15000, "max": 15000})
    hass.states.async_set("sensor.batt_soc", "24.1", {"device_class": "battery", "unit_of_measurement": "%"})
    await run(hass, freezer)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.battery_plan_control"}, blocking=True)
    await hass.async_block_till_done()
    assert calls[-1].data["entity_id"] in (SP, CUR)
    last = {c.data["entity_id"]: c.data["value"] for c in calls[-2:]}
    assert last.get(SP) == -50 and last.get(CUR) == pytest.approx(343.8, abs=0.5)
    assert val(hass, "sensor.battery_plan_control_status") == "disabled"


async def test_fallback_when_soc_unavailable(hass, octopus_states, freezer) -> None:
    _, calls = await setup(hass, freezer, control_opts())
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_control"}, blocking=True)
    hass.states.async_set("sensor.batt_soc", "unavailable")
    await run(hass, freezer)
    assert val(hass, "sensor.battery_plan_control_status") == "fallback"
    assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) == -50
    assert calls[-1].data == {"entity_id": SP, "value": -50.0}


async def test_spread_current_in_charge_window(hass, octopus_states, freezer) -> None:
    hass.states.async_set("sensor.batt_soc", "40", {"device_class": "battery", "unit_of_measurement": "%"})
    await setup(hass, freezer, control_opts(), at=datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc))
    # (100-40)% of 61.7 kWh over 5 h (finish 05:00) + 0.26 kW make-up
    kw = 0.6 * 61.7 / 5 + 0.26
    assert float(val(hass, "sensor.battery_plan_target_charge_current")) == pytest.approx(kw * 1000 / 51.2, abs=0.5)
    assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) == 0


async def test_heartbeat(hass, octopus_states, freezer) -> None:
    hass.config.components.add("mqtt")
    pub = async_mock_service(hass, "mqtt", "publish")
    await setup(hass, freezer, control_opts(heartbeat_topic="BatteryPlan/heartbeat"))
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_control"}, blocking=True)
    await hass.async_block_till_done()
    assert pub and pub[-1].data == {"topic": "BatteryPlan/heartbeat", "payload": "on"}
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.battery_plan_control"}, blocking=True)
    await hass.async_block_till_done()
    assert pub[-1].data["payload"] == "off"


async def test_battery_action(hass, octopus_states, freezer) -> None:
    await setup(hass, freezer, control_opts())
    a = "sensor.battery_plan_battery_action"
    assert val(hass, a) == "self"  # grid 2 kW import, battery -1.5 kW, plan says export
    assert hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes["action_entity"] == a
    hass.states.async_set("sensor.grid_power", "-9000", {"device_class": "power", "unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert val(hass, a) == "export"
    hass.states.async_set("sensor.grid_power", "-300", {"device_class": "power", "unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert val(hass, a) == "export"  # hysteresis: still above 0.2 kW
    hass.states.async_set("sensor.grid_power", "-100", {"device_class": "power", "unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert val(hass, a) == "self"
    hass.states.async_set("sensor.batt_power", "9000", {"device_class": "power", "unit_of_measurement": "W"})
    await hass.async_block_till_done()
    assert val(hass, a) == "charge"

