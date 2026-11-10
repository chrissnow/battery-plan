"""Control (dry run + switch) and battery action tests."""
from datetime import datetime, timedelta, timezone
import math

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
    assert val(hass, "switch.battery_plan_grid_setpoint_control") == "off"
    assert val(hass, "sensor.battery_plan_control_status") == "disabled"
    sp = float(val(hass, "sensor.battery_plan_target_grid_setpoint"))
    assert sp < -100  # exporting now (22:30, SOC 24% above 20% floor)
    assert float(val(hass, "sensor.battery_plan_target_charge_current")) == pytest.approx(17.6 * 1000 / 51.2, abs=0.2)
    await run(hass, freezer)
    assert calls == []


async def test_enable_writes_with_deadband_and_revert(hass, octopus_states, freezer) -> None:
    _, calls = await setup(hass, freezer, control_opts())
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_grid_setpoint_control"}, blocking=True)
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
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.battery_plan_grid_setpoint_control"}, blocking=True)
    await hass.async_block_till_done()
    assert calls[-1].data == {"entity_id": SP, "value": -50.0}
    assert all(c.data["entity_id"] != CUR for c in calls)  # setpoint switch never touches the current
    assert val(hass, "sensor.battery_plan_control_status") == "disabled"


async def test_switches_are_independent(hass, octopus_states, freezer) -> None:
    """Charge current control alone writes only the current; turning it off restores max."""
    hass.states.async_set("sensor.batt_soc", "40", {"device_class": "battery", "unit_of_measurement": "%"})
    _, calls = await setup(hass, freezer, control_opts(), at=datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc))
    cur_sw = "switch.battery_plan_charge_current_control"
    await hass.services.async_call("switch", "turn_on", {"entity_id": cur_sw}, blocking=True)
    await hass.async_block_till_done()
    assert {c.data["entity_id"] for c in calls} == {CUR}
    assert calls[-1].data["value"] == float(val(hass, "sensor.battery_plan_target_charge_current")) < 300
    a = hass.states.get(cur_sw).attributes
    assert a["charge_current_control"] is True and a["setpoint_control"] is False
    await hass.services.async_call("switch", "turn_off", {"entity_id": cur_sw}, blocking=True)
    await hass.async_block_till_done()
    assert calls[-1].data == {"entity_id": CUR, "value": pytest.approx(343.8, abs=0.5)}
    assert all(c.data["entity_id"] != SP for c in calls)


async def test_fallback_when_soc_unavailable(hass, octopus_states, freezer) -> None:
    _, calls = await setup(hass, freezer, control_opts())
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_grid_setpoint_control"}, blocking=True)
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


async def test_full_current_in_window_with_buy_sell_buy(hass, octopus_states, freezer) -> None:
    """With buy-sell-buy on, the window charges at full current rather than spread (40%: spread would be ~152 A)."""
    hass.states.async_set("sensor.batt_soc", "40", {"device_class": "battery", "unit_of_measurement": "%"})
    await setup(hass, freezer, control_opts(night_cycle=True), at=datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc))
    assert float(val(hass, "sensor.battery_plan_target_charge_current")) == pytest.approx(17.6 * 1000 / 51.2, abs=0.1)


async def test_rejected_dispatch_not_charged(hass, octopus_states, freezer) -> None:
    """A dispatch the plan rejects for an export: the charge schedule stays parked, the EV is inhibited."""
    hass.states.async_set("sensor.batt_soc", "95", {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set(SCHED, "0", {"min": 0, "max": 100})
    # 07:30-08:00 dispatch (fixture) with a 60p export then
    from .conftest import rates
    d0 = datetime(2026, 11, 4, tzinfo=timezone.utc)
    hass.states.async_set("event.exp_current", "x", {"rates": rates(d0, [0.18] * 15 + [0.60] + [0.18] * 16 + [0.30] * 6 + [0.20] * 10)})
    await setup(hass, freezer, control_opts(reject_dispatch=True, charge_schedule_soc_entity=SCHED,
                                            ev_allow_entity="input_boolean.zappi_allow"),
                at=datetime(2026, 11, 4, 7, 35, tzinfo=timezone.utc))
    st = hass.states.get("sensor.battery_plan_control_status").attributes
    assert st["mode"] == "export", st
    assert float(val(hass, "sensor.battery_plan_target_charge_soc")) == 0
    assert val(hass, "binary_sensor.battery_plan_ev_inhibit") == "on"


async def test_heartbeat(hass, octopus_states, freezer) -> None:
    hass.config.components.add("mqtt")
    pub = async_mock_service(hass, "mqtt", "publish")
    await setup(hass, freezer, control_opts(heartbeat_topic="BatteryPlan/heartbeat"))
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_grid_setpoint_control"}, blocking=True)
    await hass.async_block_till_done()
    assert pub and pub[-1].data == {"topic": "BatteryPlan/heartbeat", "payload": "on"}
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.battery_plan_grid_setpoint_control"}, blocking=True)
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



async def test_spread_during_iog_dispatch(hass, octopus_states, freezer) -> None:
    """07:30-08:00 dispatch at 95%: fill it, finishing 15 min early (07:45) rather than at full power."""
    hass.states.async_set("sensor.batt_soc", "95", {"device_class": "battery", "unit_of_measurement": "%"})
    await setup(hass, freezer, control_opts(), at=datetime(2026, 11, 4, 7, 30, tzinfo=timezone.utc))
    kw = 0.05 * 61.7 / 0.25 + 0.26
    assert float(val(hass, "sensor.battery_plan_target_charge_current")) == pytest.approx(kw * 1000 / 51.2, abs=0.5)
    a = hass.states.get("sensor.battery_plan_control_status").attributes
    assert a["top_up_finish_by"].startswith("2026-11-04T07:45")
    # after the dispatch: full current again
    await run(hass, freezer, 31 * 60)
    assert float(val(hass, "sensor.battery_plan_target_charge_current")) == pytest.approx(343.8, abs=0.5)


async def test_confirmed_half_hour_counts_whole(hass, octopus_states, freezer) -> None:
    """A dispatch planned to stop at 07:40, but the 07:30 half hour is confirmed (started_dispatches):
    billed off-peak to 08:00, so the top-up runs to 07:45 (08:00 less 15 min) and the plan charges in it."""
    t = lambda h, m: datetime(2026, 11, 4, h, m, tzinfo=timezone.utc)
    hass.states.async_set("binary_sensor.dispatching", "on", {
        "planned_dispatches": [{"start": t(7, 30), "end": t(7, 40), "source": "SMART"}],
        "started_dispatches": [{"start": t(7, 30), "end": t(8, 0)}]})
    hass.states.async_set("sensor.batt_soc", "90", {"device_class": "battery", "unit_of_measurement": "%"})
    await setup(hass, freezer, control_opts(), at=t(7, 35))
    a = hass.states.get("sensor.battery_plan_control_status").attributes
    assert a["top_up_finish_by"].startswith("2026-11-04T07:45")
    assert a["mode"] == "iog"
    plan = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert ["2026-11-04T07:30:00.000Z", "2026-11-04T08:00:00.000Z"] in plan["iog_dispatches"]


async def test_charge_window_from_inverter_schedule(hass, octopus_states, freezer) -> None:
    """Window and target come from the inverter's schedule entities; capacity from the BMS."""
    hass.states.async_set("time.ess_start", "00:00:00")
    hass.states.async_set("number.ess_duration", "240", {"unit_of_measurement": "min"})
    hass.states.async_set("number.ess_soc", "90", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.batt_soc", "40", {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set("sensor.consumed_ah", "-690", {"unit_of_measurement": "Ah"})  # 690 / 0.6 = 1150 Ah
    opts = control_opts(schedule_start_entity="time.ess_start", schedule_duration_entity="number.ess_duration",
                        schedule_soc_entity="number.ess_soc", consumed_ah_entity="sensor.consumed_ah")
    await setup(hass, freezer, opts, at=datetime(2026, 11, 4, 1, 0, tzinfo=timezone.utc))
    # window 00:00-04:00, target 90% (<100: finish 15 min early -> 03:45), 2.75 h left, 1150 Ah
    amps = 1150 * 0.5 / 2.75 + 0.26 * 1000 / 51.2
    assert float(val(hass, "sensor.battery_plan_target_charge_current")) == pytest.approx(amps, abs=0.5)
    a = hass.states.get("sensor.battery_plan_control_status").attributes
    assert a["bms_capacity_ah"] == 1150
    assert a["charge_window"]["source"] == {"start": "time.ess_start", "duration": "number.ess_duration", "target": "number.ess_soc"}
    # the planner uses them too
    plan = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert plan["charge_window"]["duration"] == 4 * 3600 and plan["charge_window"]["target"] == 90
    # schedule entity unavailable -> settings
    hass.states.async_set("time.ess_start", "unavailable")
    await run(hass, freezer)
    assert hass.states.get("sensor.battery_plan_control_status").attributes["charge_window"]["source"]["start"] == "settings"


SCHED = "number.ess_schedule_5_soc"


async def test_charge_schedule_soc(hass, octopus_states, freezer) -> None:
    """Charge schedule target: parked outside charges, raised for dispatches, and for the window only when asked."""
    hass.states.async_set(SCHED, "0", {"min": 0, "max": 100})
    tgt = "sensor.battery_plan_target_charge_soc"
    _, calls = await setup(hass, freezer, control_opts(charge_schedule_soc_entity=SCHED))
    assert float(val(hass, tgt)) == 0  # 22:30, exporting: parked
    await run(hass, freezer, 30 * 60)  # 23:00: still parked
    assert float(val(hass, tgt)) == 0
    await run(hass, freezer, 45 * 60)  # 23:45, in the window: left to the inverter's own schedule
    assert float(val(hass, tgt)) == 0
    await run(hass, freezer, 8 * 3600)  # 07:45, in the 07:30-08:00 dispatch: charge as the schedule would
    assert float(val(hass, tgt)) == 100
    assert calls == []  # charge control off: nothing written
    # charge control on: the target is written; off again outside a charge: parked once
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.battery_plan_charge_current_control"}, blocking=True)
    await hass.async_block_till_done()
    assert any(c.data["entity_id"] == SCHED and c.data["value"] == 100 for c in calls)
    await run(hass, freezer, 30 * 60)  # 08:15: dispatch over
    assert float(val(hass, tgt)) == 0
    assert calls[-1].data["entity_id"] in (SCHED, CUR)
    assert any(c.data["entity_id"] == SCHED and c.data["value"] == 0 for c in calls)


async def test_charge_schedule_soc_overnight(hass, octopus_states, freezer) -> None:
    """With "charge overnight with the charge schedule" the window raises it too; control stopping there keeps it raised."""
    hass.states.async_set(SCHED, "0", {"min": 0, "max": 100})
    tgt = "sensor.battery_plan_target_charge_soc"
    opts = control_opts(charge_schedule_soc_entity=SCHED, charge_schedule_window=True)
    _, calls = await setup(hass, freezer, opts, at=datetime(2026, 11, 3, 23, 45, tzinfo=timezone.utc))
    assert float(val(hass, tgt)) == 100
    for on in ("turn_on", "turn_off"):
        await hass.services.async_call("switch", on, {"entity_id": "switch.battery_plan_charge_current_control"}, blocking=True)
        await hass.async_block_till_done()
    sched = [c.data["value"] for c in calls if c.data["entity_id"] == SCHED]
    assert sched and sched[-1] == 100  # no backstop schedule: keep the overnight charge going


async def _free_session_setup(hass, freezer, soc, at):
    """Octoplus free electricity 13:00-16:00 on 4 Nov, from a mocked calendar."""
    from homeassistant.core import SupportsResponse

    start, end = datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc), datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)
    hass.states.async_set("calendar.free", "off")
    async_mock_service(hass, "calendar", "get_events", supports_response=SupportsResponse.ONLY,
                       response={"calendar.free": {"events": [{"start": start.isoformat(), "end": end.isoformat(), "summary": "Free"}]}})
    hass.states.async_set("sensor.batt_soc", str(soc), {"device_class": "battery", "unit_of_measurement": "%"})
    hass.states.async_set(SCHED, "0", {"min": 0, "max": 100})
    await setup(hass, freezer, control_opts(free_calendar="calendar.free", charge_schedule_soc_entity=SCHED), at=at)


async def test_free_session_ahead(hass, octopus_states, freezer) -> None:
    """Before a free session: export to make room (the refill is free), charge schedule still parked."""
    # 95%: the capped 16 kWh (4.5 kWh of it house load over 3 h) still needs room making
    await _free_session_setup(hass, freezer, 95, datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc))
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert a["free_session"][0].startswith("2026-11-04T13:00")
    before = [f for f in a["forecast"] if f["t"] <= "2026-11-04T13:00:00.000Z"]
    assert any(f["mode"] == "export" for f in before)  # 17p export, refilled for free: make room
    session = [f for f in a["forecast"] if "2026-11-04T13:00:00.000Z" < f["t"] <= "2026-11-04T16:00:00.000Z"]
    assert session[0]["mode"] == "charge" and session[0]["ip"] == 0
    assert a["free_sessions"][0]["free_kwh"] == pytest.approx(16, abs=0.05)  # capped (default 16 kWh)
    assert float(val(hass, "sensor.battery_plan_target_charge_soc")) == 0  # not yet


async def test_free_session_charging(hass, octopus_states, freezer) -> None:
    """In a free session: charge at the planned rate up to the plan's SOC where the 16 kWh of free
    import runs out (house load included), charging setpoint."""
    await _free_session_setup(hass, freezer, 40, datetime(2026, 11, 4, 13, 30, tzinfo=timezone.utc))
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    run = [f for f in a["forecast"] if "2026-11-04T13:30" < f["t"] <= "2026-11-04T16:00:00.000Z"]
    assert [f["mode"] for f in run][:3] == ["charge", "charge", "self"]  # 13:30-14:30, then the cap
    peak = math.ceil(max(f["soc"] for f in run[:2]))
    assert 50 < peak < 100
    assert float(val(hass, "sensor.battery_plan_target_charge_soc")) == peak
    st = hass.states.get("sensor.battery_plan_control_status").attributes
    assert st["top_up_finish_by"] is None  # not spread over the session
    assert float(val(hass, "sensor.battery_plan_target_charge_current")) == pytest.approx(17.6 * 1000 / 51.2, abs=0.1)
    assert float(val(hass, "sensor.battery_plan_target_grid_setpoint")) == 0, (st["reason"], st["mode"])

