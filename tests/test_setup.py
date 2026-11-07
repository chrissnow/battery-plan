"""Config flow, setup and sensor tests."""
from datetime import datetime, timedelta, timezone

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.battery_plan.const import DOMAIN

NOW = datetime(2026, 11, 3, 22, 30, tzinfo=timezone.utc)

SETTINGS = {
    "battery": {"soc_entity": "sensor.batt_soc", "capacity_kwh": 61.7, "target_soc": 100, "reserve_soc": 20, "min_soc": 5},
    "tariff": {"import_rates": ["event.imp_current", "event.imp_next"], "export_rates": ["event.exp_current", "event.exp_next"],
               "standing_charge_entity": "sensor.standing"},
    "charging": {"charge_start": "23:30:00", "charge_hours": 6, "charge_mode": "spread", "max_charge_kw": 17.6,
                 "min_charge_kw": 0.51, "makeup_kw": 0.26, "finish_early_min": 15, "finish_early_full_min": 30,
                 "long_balance_day": "sat", "long_balance_min": 60, "charge_eff": 0.93},
    "export": {"max_export_kw": 11, "saving_export_kw": 12, "min_export_price": 12.5, "discharge_eff": 0.9},
    "load": {"load_days": 7, "load_kw": 1.5},
    "octopus": {"dispatching_entity": "binary_sensor.dispatching", "dispatch_charges_battery": True,
                "target_time_entity": "select.target_time"},
    "actuals": {"grid_power_entity": "sensor.grid_power", "grid_power_invert": False},
}


def flat(settings):
    return {k: v for sec in settings.values() for k, v in sec.items()}


async def test_config_flow(hass: HomeAssistant, recorder_mock, octopus_states) -> None:
    res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert res["type"] is FlowResultType.FORM and res["step_id"] == "user"
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"name": "Battery Plan"})
    assert res["step_id"] == "settings"
    bad = {**SETTINGS, "battery": {**SETTINGS["battery"], "reserve_soc": 100}}
    res2 = await hass.config_entries.flow.async_configure(res["flow_id"], bad)
    assert res2["errors"] == {"base": "reserve_above_target"}
    res = await hass.config_entries.flow.async_configure(res["flow_id"], SETTINGS)
    assert res["type"] is FlowResultType.CREATE_ENTRY
    assert res["options"]["capacity_kwh"] == 61.7
    assert res["options"]["import_rates"] == ["event.imp_current", "event.imp_next"]
    await hass.async_block_till_done()


async def setup(hass, freezer, options):
    freezer.move_to(NOW)
    entry = MockConfigEntry(domain=DOMAIN, title="Battery Plan", data={"name": "Battery Plan"}, options=options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_plan_sensors(hass: HomeAssistant, recorder_mock, octopus_states, freezer: FrozenDateTimeFactory) -> None:
    await setup(hass, freezer, flat(SETTINGS))
    st = hass.states.get("sensor.battery_plan_soc_at_next_charge")
    assert st is not None, [s.entity_id for s in hass.states.async_all() if "battery_plan" in s.entity_id]
    a = st.attributes
    assert float(st.state) == pytest.approx(20, abs=1)
    modes = [b["mode"] for b in a["plan"]]
    assert modes[:4] == ["export", "charge", "hold", "iog"], modes
    assert a["plan"][3]["start"].startswith("2026-11-04T07:30")
    assert len(a["forecast"]) == 72
    assert a["load_source"] == "flat"
    day = a["days"]["2026-11-04"]
    assert day["import_kwh"] > 40 and day["import_cost"] > 2 and day["export_kwh"] > 5
    assert hass.states.get("sensor.battery_plan_next_action").state == "export"
    assert float(hass.states.get("sensor.battery_plan_dispatch_hours_before_ready").state) == 0.5
    assert hass.states.get("binary_sensor.battery_plan_final_slot_scheduled").state == "on"
    tmrw = hass.states.get("sensor.battery_plan_net_tomorrow_plan")
    assert tmrw.attributes["unit_of_measurement"] == hass.config.currency
    assert float(tmrw.state) == pytest.approx(day["export_value"] - day["import_cost"] - 0.507455, abs=0.01)


async def test_replans_on_dispatch_change(hass, recorder_mock, octopus_states, freezer) -> None:
    await setup(hass, freezer, flat(SETTINGS))
    hass.states.async_set("binary_sensor.dispatching", "off", {"planned_dispatches": []})
    freezer.tick(timedelta(seconds=15))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert "iog" not in [b["mode"] for b in a["plan"]]
    assert hass.states.get("binary_sensor.battery_plan_final_slot_scheduled").state == "off"


async def test_actual_costs(hass, recorder_mock, octopus_states, freezer) -> None:
    await setup(hass, freezer, flat(SETTINGS))
    # 2 kW import for 30 min at 34.264p, then 10 kW export for 30 min at 20p (23:00-23:30 export rate 0.20)
    freezer.tick(timedelta(minutes=30))
    hass.states.async_set("sensor.grid_power", "-10000", {"device_class": "power", "unit_of_measurement": "W"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    imp = hass.states.get("sensor.battery_plan_import_cost_today")
    exp = hass.states.get("sensor.battery_plan_export_earnings_today")
    assert float(imp.state) == pytest.approx(1.0 * 0.34264, abs=0.005)
    assert float(exp.state) == pytest.approx(5.0 * 0.20, abs=0.005)
    net = hass.states.get("sensor.battery_plan_net_today")
    assert float(net.state) == pytest.approx(1.0 - 0.34264 - 0.507455, abs=0.01)
    # 23:30 -> 00:10, still exporting 10 kW: today becomes yesterday at midnight
    freezer.tick(timedelta(minutes=40))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    y = hass.states.get("sensor.battery_plan_net_yesterday")
    assert y.attributes["date"] == "2026-11-03"
    assert float(y.attributes["export_kwh"]) == pytest.approx(10.0, abs=0.01)  # 23:00-00:00
    assert float(y.state) == pytest.approx(5 * 0.20 + 5 * 0.20 - 0.34264 - 0.507455, abs=0.01)
    assert float(hass.states.get("sensor.battery_plan_export_earnings_today").state) == pytest.approx(10 * 10 / 60 * 0.17, abs=0.01)


async def test_minimal_setup(hass, recorder_mock, octopus_states, freezer) -> None:
    """No optional entities: no IOG or actual-cost entities."""
    opts = flat(SETTINGS)
    for k in ("export_rates", "standing_charge_entity", "dispatching_entity", "target_time_entity", "grid_power_entity"):
        opts.pop(k)
    await setup(hass, freezer, opts)
    assert hass.states.get("sensor.battery_plan_soc_at_next_charge") is not None
    assert hass.states.get("sensor.battery_plan_net_today") is None
    assert hass.states.get("binary_sensor.battery_plan_final_slot_scheduled") is None
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert a["export_kwh_planned"] == 0


async def test_soc_unavailable(hass, recorder_mock, octopus_states, freezer) -> None:
    hass.states.async_set("sensor.batt_soc", "unavailable")
    freezer.move_to(NOW)
    entry = MockConfigEntry(domain=DOMAIN, title="Battery Plan", data={"name": "Battery Plan"}, options=flat(SETTINGS))
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is config_entries.ConfigEntryState.SETUP_RETRY


async def test_options_flow(hass, recorder_mock, octopus_states, freezer) -> None:
    entry = await setup(hass, freezer, flat(SETTINGS))
    res = await hass.config_entries.options.async_init(entry.entry_id)
    assert res["step_id"] == "init"
    new = {**SETTINGS, "export": {**SETTINGS["export"], "min_export_price": 40}}
    res = await hass.config_entries.options.async_configure(res["flow_id"], new)
    assert res["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert a["min_export_rate"] == 40 and a["export_kwh_planned"] == 0


async def test_load_profile_from_statistics(hass, recorder_mock, octopus_states, freezer) -> None:
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    freezer.move_to(NOW)
    rows = []
    start = NOW.replace(minute=0) - timedelta(days=3)
    for i in range(72):
        t = start + timedelta(hours=i)
        rows.append({"start": t, "mean": 400.0 + 100 * t.hour, "min": 0, "max": 0})
    ev = [{"start": r["start"], "mean": 7000.0 if r["start"].hour == 3 else 0.0, "min": 0, "max": 0} for r in rows]
    for eid, data in (("sensor.load_l1", rows), ("sensor.load_l2", rows), ("sensor.ev", ev)):
        hass.states.async_set(eid, "500", {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"})
        meta = {"source": "recorder", "statistic_id": eid, "name": None, "unit_of_measurement": "W",
                "has_mean": True, "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "unit_class": "power"}
        async_import_statistics(hass, meta, data)
    await async_wait_recording_done(hass)
    opts = flat(SETTINGS) | {"load_entities": ["sensor.load_l1", "sensor.load_l2"], "load_exclude_entities": ["sensor.ev"]}
    await setup(hass, freezer, opts)
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    assert a["load_source"] == "statistics_hourly"  # no 5-minute data in the test recorder
    prof = a["load_profile_kw"]
    assert len(prof) == 48
    # two phases, each (400 + 100*h) W, per half hour
    assert prof[0] == prof[1] == pytest.approx(0.8, abs=0.01)
    assert prof[36] == pytest.approx(2 * 2.2, abs=0.01)
    # 03:00 hour: 2 x 0.7 kW minus 7 kW EV -> clamped to 0
    assert prof[6] == 0


def _initial_form_data(fields: list[dict]) -> dict:
    """Mirror of the frontend's computeInitialHaFormData for the parts we use."""
    out = {}
    for f in fields:
        sv = (f.get("description") or {}).get("suggested_value")
        if sv is not None:
            out[f["name"]] = sv
        elif "default" in f:
            out[f["name"]] = f["default"]
        elif f.get("type") == "expandable":
            inner = _initial_form_data(f["schema"])
            if f.get("required") or inner:
                out[f["name"]] = inner
        elif f.get("required") and "selector" in f and "boolean" in f["selector"]:
            out[f["name"]] = False
    return out


def _missing_required(fields: list[dict], data: dict) -> list[str]:
    miss = []
    for f in fields:
        if f.get("type") == "expandable":
            miss += _missing_required(f["schema"], data.get(f["name"]) or {})
        elif f.get("required") and data.get(f["name"]) in (None, ""):
            miss.append(f["name"])
    return miss


async def test_new_user_form_needs_only_battery_and_tariff(hass: HomeAssistant, recorder_mock, octopus_states) -> None:
    """Untouched collapsed sections must arrive filled with their defaults (issue: 'Not all required fields')."""
    import voluptuous_serialize
    from homeassistant.helpers import config_validation as cv

    res = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    res = await hass.config_entries.flow.async_configure(res["flow_id"], {"name": "Battery Plan"})
    fields = voluptuous_serialize.convert(res["data_schema"], custom_serializer=cv.custom_serializer)
    data = _initial_form_data(fields)
    # only the fields a new user must pick are empty
    assert sorted(_missing_required(fields, data)) == ["capacity_kwh", "import_rates", "soc_entity"]
    data["battery"].update(soc_entity="sensor.batt_soc", capacity_kwh=61.7)
    data["tariff"]["import_rates"] = ["event.imp_current", "event.imp_next"]
    assert data["control"]["ev_allow_invert"] is False and data["export"]["export_tracking"] is True
    res = await hass.config_entries.flow.async_configure(res["flow_id"], data)
    assert res["type"] == "create_entry", res.get("errors")
    assert res["options"]["export_tracking"] is True and res["options"]["charge_start"] == "23:30:00"


async def test_card_resource_registered_and_updated(hass: HomeAssistant, recorder_mock, octopus_states, freezer) -> None:
    from homeassistant.components.lovelace.const import LOVELACE_DATA
    from homeassistant.setup import async_setup_component

    from custom_components.battery_plan.const import CARD_URL, VERSION

    assert await async_setup_component(hass, "lovelace", {})
    res = hass.data[LOVELACE_DATA].resources
    await res.async_load()
    res.loaded = True
    await res.async_create_item({"res_type": "module", "url": f"{CARD_URL}?v=0.0.1"})
    await res.async_create_item({"res_type": "module", "url": f"{CARD_URL}?v=0.0.2"})
    await res.async_create_item({"res_type": "module", "url": "/local/other.js"})
    await setup(hass, freezer, flat(SETTINGS))
    urls = sorted(r["url"] for r in res.async_items())
    assert urls == sorted(["/local/other.js", f"{CARD_URL}?v={VERSION}"])


async def test_load_profile_skips_dispatches_and_sessions(hass, recorder_mock, octopus_states, freezer) -> None:
    """IOG dispatches and Octoplus sessions don't count as normal house load."""
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    start = NOW.replace(minute=0) - timedelta(days=3)
    disp = start + timedelta(days=1, hours=-start.hour + 13)  # 13:00-14:00 two days ago: dispatch
    free = disp + timedelta(days=1, hours=2)  # 15:00-16:00 yesterday: free session
    win = start + timedelta(days=1, hours=-start.hour + 1)  # 01:00 inside the charge window
    # recorded history for the dispatch sensor and the free-session calendar
    for t, eid, st in ((start, "binary_sensor.dispatching", "off"), (start, "calendar.free", "off"),
                       (win, "binary_sensor.dispatching", "on"), (win + timedelta(hours=1), "binary_sensor.dispatching", "off"),
                       (disp, "binary_sensor.dispatching", "on"), (disp + timedelta(hours=1), "binary_sensor.dispatching", "off"),
                       (free, "calendar.free", "on"), (free + timedelta(hours=1), "calendar.free", "off")):
        freezer.move_to(t)
        hass.states.async_set(eid, st, {"planned_dispatches": []} if eid.startswith("binary") else {})
        await async_wait_recording_done(hass)
    rows = []
    for i in range(72):
        t = start + timedelta(hours=i)
        busy = t in (disp, free, win)
        rows.append({"start": t, "mean": 9000.0 if busy else 500.0, "min": 0, "max": 0})
    hass.states.async_set("sensor.load", "500", {"device_class": "power", "unit_of_measurement": "W", "state_class": "measurement"})
    meta = {"source": "recorder", "statistic_id": "sensor.load", "name": None, "unit_of_measurement": "W",
            "has_mean": True, "mean_type": StatisticMeanType.ARITHMETIC, "has_sum": False, "unit_class": "power"}
    async_import_statistics(hass, meta, rows)
    await async_wait_recording_done(hass)
    opts = flat(SETTINGS) | {"load_entities": ["sensor.load"], "free_calendar": "calendar.free"}
    await setup(hass, freezer, opts)
    a = hass.states.get("sensor.battery_plan_soc_at_next_charge").attributes
    prof = a["load_profile_kw"]
    assert prof[26] == prof[27] == pytest.approx(0.5)  # dispatch hour left out
    assert prof[30] == prof[31] == pytest.approx(0.5)  # free session left out
    assert prof[2] == pytest.approx((9 + 0.5 + 0.5) / 3, abs=0.01)  # inside the charge window: kept
    assert a["load_masked_slots"] >= 4


async def test_buckets_bridge_daily_masks(hass) -> None:
    from custom_components.battery_plan.coordinator import BatteryPlanCoordinator as C

    await hass.config.async_set_time_zone("Europe/London")

    d0 = datetime(2026, 11, 1, tzinfo=timezone.utc)  # GMT: local == UTC
    rows, masked = [], set()
    for day in range(3):
        for slot in range(48):
            t = d0 + timedelta(days=day, minutes=30 * slot)
            kw = 2.0 if slot < 24 else 1.0
            if slot == 15:  # 07:30 dispatch every day
                kw, _ = 9.0, masked.add(int(t.timestamp()))
            rows.append({"start": t, "mean": kw})
    out = C._buckets(rows, hourly=False, masked=masked)
    assert out[15] == pytest.approx(2.0)  # bridged from 07:00 and 08:00, not 9 kW
    assert out[0] == 2.0 and out[30] == 1.0


async def test_costs_from_energy_dashboard(hass, recorder_mock, octopus_states, freezer) -> None:
    """With priced grid meters in the Energy dashboard, actual costs come from its statistics."""
    from homeassistant.components.energy.data import async_get_manager
    from homeassistant.components.recorder.models import StatisticMeanType
    from homeassistant.components.recorder.statistics import async_import_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

    freezer.move_to(NOW)
    mgr = await async_get_manager(hass)
    await mgr.async_update({"energy_sources": [{
        "type": "grid",
        "flow_from": [{"stat_energy_from": "sensor.meter_import", "stat_cost": "sensor.meter_import_cost",
                       "entity_energy_price": None, "number_energy_price": None}],
        "flow_to": [{"stat_energy_to": "sensor.meter_export", "stat_compensation": "sensor.meter_export_comp",
                     "entity_energy_price": None, "number_energy_price": None}],
        "cost_adjustment_day": 0,
    }]})
    # hourly sums: yesterday 00:00 -> now; 1 kWh import at 0.10 and 0.5 kWh export at 0.20 per hour
    start = NOW.replace(hour=0, minute=0) - timedelta(days=1)
    hours = int((NOW.replace(minute=0) - start).total_seconds() // 3600)
    for sid, unit, per_h in (("sensor.meter_import", "kWh", 1.0), ("sensor.meter_import_cost", "GBP", 0.10),
                             ("sensor.meter_export", "kWh", 0.5), ("sensor.meter_export_comp", "GBP", 0.10)):
        rows = [{"start": start + timedelta(hours=i), "state": per_h * (i + 1), "sum": per_h * (i + 1)} for i in range(hours)]
        meta = {"source": "recorder", "statistic_id": sid, "name": None, "unit_of_measurement": unit, "has_mean": False,
                "mean_type": StatisticMeanType.NONE, "has_sum": True, "unit_class": "energy" if unit == "kWh" else None}
        hass.states.async_set(sid, "0", {"unit_of_measurement": unit, "state_class": "total_increasing"})
        async_import_statistics(hass, meta, rows)
    await async_wait_recording_done(hass)
    opts = flat(SETTINGS)
    opts.pop("grid_power_entity")  # not needed with the Energy dashboard
    await setup(hass, freezer, opts)
    await hass.async_block_till_done()
    imp = hass.states.get("sensor.battery_plan_import_cost_today")
    assert imp.attributes["source"] == "energy_dashboard"
    # today: 00:00 -> 22:00 complete hours = 22 h
    assert float(imp.state) == pytest.approx(22 * 0.10, abs=0.11)
    assert imp.attributes["import_kwh"] == pytest.approx(22, abs=1.01)
    y = hass.states.get("sensor.battery_plan_net_yesterday")
    assert y.attributes["date"] == "2026-11-02"
    assert y.attributes["import_kwh"] == pytest.approx(24, abs=0.01)
    assert float(y.state) == pytest.approx(24 * 0.10 - 24 * 0.10 - 0.507455, abs=0.01)
