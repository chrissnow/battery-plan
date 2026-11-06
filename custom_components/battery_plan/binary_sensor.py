"""Binary sensors for Battery Plan."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import BatteryPlanConfigEntry
from .const import CONF_DISPATCHING_ENTITY, CONF_TARGET_TIME_ENTITY
from .coordinator import BatteryPlanCoordinator
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: BatteryPlanConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    ents: list[BinarySensorEntity] = [EvInhibitSensor(c, entry)]
    if c.opts.get(CONF_DISPATCHING_ENTITY) and c.opts.get(CONF_TARGET_TIME_ENTITY):
        ents.append(FinalSlotSensor(c, entry))
    async_add_entities(ents)


class FinalSlotSensor(CoordinatorEntity[BatteryPlanCoordinator], BinarySensorEntity):
    """On when a planned dispatch covers the half hour before the ready time."""

    _attr_has_entity_name = True
    _attr_translation_key = "final_slot_scheduled"

    def __init__(self, c: BatteryPlanCoordinator, entry: BatteryPlanConfigEntry) -> None:
        super().__init__(c)
        self._attr_unique_id = f"{entry.entry_id}_final_slot_scheduled"
        self._attr_device_info = device_info(entry)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None and "ready_time" in self.coordinator.data

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.get("final_slot_scheduled")

    @property
    def extra_state_attributes(self) -> dict:
        return {"ready_time": self.coordinator.data.get("ready_time")}


class EvInhibitSensor(BinarySensorEntity):
    """On while exporting (>= 200 W) outside the charge window: pause the EV charger."""

    _attr_has_entity_name = True
    _attr_translation_key = "ev_inhibit"
    _attr_should_poll = False

    def __init__(self, c: BatteryPlanCoordinator, entry: BatteryPlanConfigEntry) -> None:
        self._ctl = c.controller
        self._attr_unique_id = f"{entry.entry_id}_ev_inhibit"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._ctl.add_listener(self.async_write_ha_state))

    @property
    def is_on(self) -> bool:
        return self._ctl.ev_inhibit
