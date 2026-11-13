"""Sensors for Battery Plan."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.const import EntityCategory
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import BatteryPlanConfigEntry
from .const import (
    CONF_WEATHER_ENTITY,
    CONF_CHARGE_SOC_ENTITY,
    ACTION_OFF_KW,
    ACTION_ON_KW,
    ACTION_SETTLE,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERT,
    CONF_DISPATCHING_ENTITY,
    CONF_GRID_POWER_ENTITY,
    CONF_PV_ENTITY,
    CONF_PV_FORECAST_ENTRY,
    CONF_GRID_POWER_INVERT,
    CONF_TARGET_TIME_ENTITY,
    DOMAIN,
    VERSION,
)
from .control import STATUSES
from .coordinator import BatteryPlanCoordinator
from .planner import MODES

# Big attributes stay out of the recorder database
BIG = frozenset({"forecast", "plan", "days", "load_profile_kw", "iog_dispatches", "weather_model", "charge_window",
                 "load_entities", "load_exclude", "solar", "target_scores"})


def device_info(entry: BatteryPlanConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Battery Plan",
        model="Planner",
        sw_version=VERSION,
    )


def _day(data: dict, offset: int) -> dict | None:
    key = (dt_util.now().date() + timedelta(days=offset)).isoformat()
    return (data.get("days") or {}).get(key)


def _remaining_today(data: dict) -> dict:
    """Planned import/export for the rest of today (forecast slots ending today)."""
    today = dt_util.now().date()
    out = {"import_cost": 0.0, "export_value": 0.0, "import_kwh": 0.0, "export_kwh": 0.0}
    for f in data.get("forecast") or []:
        end = dt_util.parse_datetime(f["t"])
        if end is None or dt_util.as_local(end - timedelta(minutes=30)).date() != today:
            continue
        out["import_kwh"] += f["ik"]
        out["export_kwh"] += f["ek"]
        out["import_cost"] += f["ik"] * (f.get("ip") or 0) / 100
        out["export_value"] += f["ek"] * (f.get("rate") or 0) / 100
    return out


@dataclass(frozen=True, kw_only=True)
class PlanSensorDescription(SensorEntityDescription):
    value_fn: Callable[[BatteryPlanCoordinator], Any]
    attrs_fn: Callable[[BatteryPlanCoordinator], dict[str, Any]] | None = None
    available_fn: Callable[[BatteryPlanCoordinator], bool] = lambda c: True


def _net_plan(c: BatteryPlanCoordinator, offset: int) -> float | None:
    d = _day(c.data, offset)
    if not d:
        return None
    sc = c.data.get("standing_charge") or 0.0
    return round(d["export_value"] - d["import_cost"] - sc, 2)


def _net_today_est(c: BatteryPlanCoordinator) -> float | None:
    rest = _remaining_today(c.data)
    sc = c.data.get("standing_charge") or 0.0
    if c.costs.enabled:
        t = c.costs.today
        return round(t["export_value"] + rest["export_value"] - t["import_cost"] - rest["import_cost"] - sc, 2)
    return _net_plan(c, 0)


def _day_attrs(c: BatteryPlanCoordinator, offset: int) -> dict[str, Any]:
    d = _day(c.data, offset) or {}
    return {k: d.get(k) for k in ("import_kwh", "import_cost", "export_kwh", "export_value", "priced")} | {
        "standing_charge": c.data.get("standing_charge")
    }


PLAN_SENSORS: tuple[PlanSensorDescription, ...] = (
    PlanSensorDescription(
        key="soc_at_next_charge",
        translation_key="soc_at_next_charge",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data["soc_at_next_charge"],
        attrs_fn=lambda c: {k: v for k, v in c.data.items() if k != "soc_at_next_charge"}
        | {"action_entity": c.action_entity_id},
    ),
    PlanSensorDescription(
        key="min_soc_ahead",
        translation_key="min_soc_ahead",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda c: c.data["min_soc_ahead"],
    ),
    PlanSensorDescription(
        key="next_action",
        translation_key="next_action",
        device_class=SensorDeviceClass.ENUM,
        options=MODES,
        value_fn=lambda c: c.data["next_action"],
        attrs_fn=lambda c: {"start": c.data["next_action_start"], "end": c.data["next_action_end"]},
    ),
    PlanSensorDescription(
        key="planned_export",
        translation_key="planned_export",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=lambda c: c.data["export_kwh_planned"],
    ),
    PlanSensorDescription(
        key="net_today_estimate",
        translation_key="net_today_estimate",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=_net_today_est,
        attrs_fn=lambda c: {"remaining_plan": {k: round(v, 3) for k, v in _remaining_today(c.data).items()}},
    ),
    PlanSensorDescription(
        key="net_tomorrow_plan",
        translation_key="net_tomorrow_plan",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda c: _net_plan(c, 1),
        attrs_fn=lambda c: _day_attrs(c, 1),
    ),
    PlanSensorDescription(
        key="dispatch_hours_before_ready",
        translation_key="dispatch_hours_before_ready",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.get("dispatch_hours_before_ready"),
        attrs_fn=lambda c: {"ready_time": c.data.get("ready_time")},
        available_fn=lambda c: "ready_time" in c.data,
    ),
    PlanSensorDescription(
        key="weather_load_today",
        translation_key="weather_load_today",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.get("weather_load_today"),
        attrs_fn=lambda c: _weather_slots(c, 0),
        available_fn=lambda c: "weather_model" in c.data,
    ),
    PlanSensorDescription(
        key="weather_load_tomorrow",
        translation_key="weather_load_tomorrow",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda c: c.data.get("weather_load_tomorrow"),
        attrs_fn=lambda c: _weather_slots(c, 1),
        available_fn=lambda c: "weather_model" in c.data,
    ),
    PlanSensorDescription(
        key="heating_sensitivity",
        translation_key="heating_sensitivity",
        native_unit_of_measurement="kW/°C",
        suggested_display_precision=3,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda c: (c.data.get("weather_model") or {}).get("heating_kw_per_c"),
        attrs_fn=lambda c: {k: v for k, v in (c.data.get("weather_model") or {}).items() if k != "daily"},
        available_fn=lambda c: "weather_model" in c.data,
    ),
)


def _weather_slots(c: BatteryPlanCoordinator, offset: int) -> dict[str, Any]:
    """Half-hourly weather load (kW) and forecast temperature for that local day."""
    day = (dt_util.now() + timedelta(days=offset)).date()
    out = []
    for f in c.data.get("forecast") or []:
        t = dt_util.parse_datetime(f["t"])
        if t is None or dt_util.as_local(t - timedelta(minutes=30)).date() != day:
            continue
        out.append({"t": f["t"], "kw": round((f.get("wh") or 0) + (f.get("wc") or 0), 2), "temp": f.get("tf")})
    return {"status": (c.data.get("weather_model") or {}).get("status"), "slots": out}


async def async_setup_entry(
    hass: HomeAssistant, entry: BatteryPlanConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    ents: list[SensorEntity] = []
    for d in PLAN_SENSORS:
        if d.key == "dispatch_hours_before_ready" and not (
            c.opts.get(CONF_DISPATCHING_ENTITY) and c.opts.get(CONF_TARGET_TIME_ENTITY)
        ):
            continue
        if d.key in ("weather_load_today", "weather_load_tomorrow", "heating_sensitivity") and not c.opts.get(
            CONF_WEATHER_ENTITY
        ):
            continue
        ents.append(PlanSensor(c, entry, d))
    if c.costs.enabled:
        ents += [CostSensor(c, entry, k) for k in ("import_cost_today", "export_value_today", "net_today", "net_yesterday")]
    keys = ["target_grid_setpoint", "target_charge_current", "control_status"]
    if c.opts.get(CONF_CHARGE_SOC_ENTITY):
        keys.insert(2, "target_charge_soc")
    ents += [ControlSensor(c, entry, k) for k in keys]
    ents.append(ActionSensor(c, entry))
    async_add_entities(ents)


class PlanSensor(CoordinatorEntity[BatteryPlanCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _unrecorded_attributes = BIG
    entity_description: PlanSensorDescription

    def __init__(self, c: BatteryPlanCoordinator, entry: BatteryPlanConfigEntry, d: PlanSensorDescription) -> None:
        super().__init__(c)
        self.entity_description = d
        self._attr_unique_id = f"{entry.entry_id}_{d.key}"
        self._attr_device_info = device_info(entry)
        if d.device_class == SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = c.hass.config.currency
            self._attr_suggested_display_precision = 2

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None and self.entity_description.available_fn(self.coordinator)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator) if fn else None


class CostSensor(CoordinatorEntity[BatteryPlanCoordinator], SensorEntity):
    """Actual grid cost: Energy dashboard cost statistics, or grid power x live rates."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_suggested_display_precision = 2

    def __init__(self, c: BatteryPlanCoordinator, entry: BatteryPlanConfigEntry, key: str) -> None:
        super().__init__(c)
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = device_info(entry)
        self._attr_native_unit_of_measurement = c.hass.config.currency
        if key != "net_yesterday":
            self._attr_state_class = SensorStateClass.TOTAL

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.costs.add_listener(self._tick)

    @callback
    def _tick(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return True

    @property
    def last_reset(self) -> datetime | None:
        if self._key == "net_yesterday":
            return None
        return dt_util.start_of_local_day()

    @property
    def native_value(self) -> float | None:
        t = self.coordinator.costs
        if self._key == "import_cost_today":
            return round(t.today["import_cost"], 3)
        if self._key == "export_value_today":
            return round(t.today["export_value"], 3)
        if self._key == "net_today":
            v = t.net(t.today)
        else:
            v = t.net(t.yesterday)
        return None if v is None else round(v, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        t = self.coordinator.costs
        d = t.yesterday if self._key == "net_yesterday" else t.today
        if not d:
            return {}
        return {
            "date": d["date"],
            "import_kwh": round(d["import_kwh"], 2),
            "import_cost": round(d["import_cost"], 2),
            "export_kwh": round(d["export_kwh"], 2),
            "export_value": round(d["export_value"], 2),
            "unpriced_kwh": round(d["unpriced_kwh"], 2),
            # taken off import_cost: confirmed dispatch half hours billed off-peak in full
            "dispatch_reprice": round(d.get("dispatch_reprice", 0.0), 2),
            "standing_charge": self.coordinator.standing_charge(),
            "source": self.coordinator.costs.source,
        }


class ControlSensor(SensorEntity):
    """Live control targets (always computed: a dry run when control is off)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, c: BatteryPlanCoordinator, entry: BatteryPlanConfigEntry, key: str) -> None:
        self._ctl = c.controller
        self._key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = device_info(entry)
        if key == "target_grid_setpoint":
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key == "target_charge_current":
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key == "target_charge_soc":
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = STATUSES

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._ctl.add_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> Any:
        if self._key == "target_grid_setpoint":
            return self._ctl.setpoint_w
        if self._key == "target_charge_current":
            return self._ctl.current_a
        if self._key == "target_charge_soc":
            return self._ctl.charge_soc
        return self._ctl.status

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self._ctl.attributes() if self._key == "control_status" else None


class ActionSensor(CoordinatorEntity[BatteryPlanCoordinator], SensorEntity):
    """What the battery is actually doing now; its history gives past actions.

    Export when the grid is exporting; charge (or IOG top-up, if that's the plan)
    when the battery is charging - or, without a battery power sensor, when the
    plan says charge and the grid is importing; otherwise house on battery, or
    'grid' when the plan has the battery at its minimum. Thresholds have
    hysteresis (on above 0.5 kW, off below 0.2 kW) so the state doesn't flap, and
    a new state must last ``ACTION_SETTLE`` (a load switching off shows as a few
    hundred W of export for one meter reading before the ESS catches up).
    """

    _attr_has_entity_name = True
    _attr_translation_key = "battery_action"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = MODES

    def __init__(self, c: BatteryPlanCoordinator, entry: BatteryPlanConfigEntry) -> None:
        super().__init__(c)
        self._attr_unique_id = f"{entry.entry_id}_battery_action"
        self._attr_device_info = device_info(entry)
        self._state: str | None = None
        self._pending: tuple[str | None, datetime] | None = None  # (state, first seen)
        self._recheck: Callable[[], None] | None = None

    async def async_will_remove_from_hass(self) -> None:
        if self._recheck:
            self._recheck()
            self._recheck = None
        await super().async_will_remove_from_hass()

    @callback
    def _on_settle(self, _now: datetime) -> None:
        self._recheck = None
        if self._classify():
            self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.action_entity_id = self.entity_id
        self.coordinator.async_update_listeners()  # plan sensor picks up action_entity
        ids = [e for e in (self.coordinator.opts.get(CONF_GRID_POWER_ENTITY), self.coordinator.opts.get(CONF_BATTERY_POWER_ENTITY)) if e]
        if ids:
            self.async_on_remove(async_track_state_change_event(self.hass, ids, self._on_power))
        self._classify()

    @callback
    def _on_power(self, _event: Event[EventStateChangedData]) -> None:
        if self._classify():
            self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        self._classify()
        super()._handle_coordinator_update()

    def _kw(self, key: str, invert_key: str) -> float | None:
        eid = self.coordinator.opts.get(key)
        st = self.hass.states.get(eid) if eid else None
        if st is None or st.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            v = float(st.state)
        except ValueError:
            return None
        unit = (st.attributes.get("unit_of_measurement") or "W").lower()
        kw = v / 1000 if unit == "w" else v
        return -kw if self.coordinator.opts.get(invert_key) else kw

    def _classify(self) -> bool:
        slot = self.coordinator.current_slot() if self.coordinator.data else None
        plan = slot["mode"] if slot else None
        grid = self._kw(CONF_GRID_POWER_ENTITY, CONF_GRID_POWER_INVERT)
        batt = self._kw(CONF_BATTERY_POWER_ENTITY, CONF_BATTERY_POWER_INVERT)
        prev = self._state

        def on(v: float | None, was: bool) -> bool:
            return v is not None and v > (ACTION_OFF_KW if was else ACTION_ON_KW)

        exporting = on(None if grid is None else -grid, prev in ("export", "saving"))
        if batt is not None:
            charging = on(batt, prev in ("charge", "iog"))
        else:
            charging = plan in ("charge", "iog") and on(grid, prev in ("charge", "iog"))
        # the battery charging with no grid import behind it: PV (spilling to export or not)
        o = self.coordinator.opts
        has_pv = bool(o.get(CONF_PV_FORECAST_ENTRY) or o.get(CONF_PV_ENTITY))
        solar = has_pv and batt is not None and on(batt, prev == "solar") and not on(grid, False)
        if grid is None and batt is None:
            new = plan  # no power sensors: mirror the plan
        elif solar and plan not in ("charge", "iog"):
            new = "solar"
        elif exporting:
            new = "saving" if plan == "saving" else "export"
        elif charging:
            new = "iog" if plan == "iog" else "charge"
        elif plan in ("charge", "hold", "iog"):
            new = "hold"
        elif plan == "grid":
            new = "grid"
        else:
            new = "self"
        if new == prev:
            self._pending = None
            return False
        now = dt_util.utcnow()
        if prev is not None and (grid is not None or batt is not None):
            if self._pending is None or self._pending[0] != new:
                self._pending = (new, now)
            if now - self._pending[1] < ACTION_SETTLE:
                if self._recheck is None:
                    self._recheck = async_call_later(self.hass, ACTION_SETTLE, self._on_settle)
                return False
        self._pending = None
        self._state = new
        return True

    @property
    def native_value(self) -> str | None:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        slot = self.coordinator.current_slot() if self.coordinator.data else None
        return {"planned": slot["mode"] if slot else None}
