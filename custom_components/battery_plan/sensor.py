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
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import BatteryPlanConfigEntry
from .const import CONF_DISPATCHING_ENTITY, CONF_TARGET_TIME_ENTITY, DOMAIN, VERSION
from .coordinator import BatteryPlanCoordinator
from .planner import MODES

# Big attributes stay out of the recorder database
BIG = frozenset({"forecast", "plan", "days", "load_profile_kw", "iog_dispatches"})


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
        attrs_fn=lambda c: {k: v for k, v in c.data.items() if k != "soc_at_next_charge"},
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
)


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
        ents.append(PlanSensor(c, entry, d))
    if c.costs.enabled:
        ents += [CostSensor(c, entry, k) for k in ("import_cost_today", "export_value_today", "net_today", "net_yesterday")]
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
    """Actual grid cost from the grid power sensor x live rates."""

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
            "standing_charge": self.coordinator.standing_charge(),
        }
