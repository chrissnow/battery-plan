"""Custom setpoint for the Battery Plan 'custom' override."""
from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BatteryPlanConfigEntry
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: BatteryPlanConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([CustomSetpoint(entry)])


class CustomSetpoint(RestoreNumber):
    """Grid setpoint used when the override is 'custom' (negative = export)."""

    _attr_has_entity_name = True
    _attr_translation_key = "custom_setpoint"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = -15000
    _attr_native_max_value = 15000
    _attr_native_step = 50
    _attr_mode = NumberMode.BOX
    _attr_should_poll = False

    def __init__(self, entry: BatteryPlanConfigEntry) -> None:
        self._ctl = entry.runtime_data.controller
        self._attr_unique_id = f"{entry.entry_id}_custom_setpoint"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            self._ctl.custom_w = float(last.native_value)

    @property
    def native_value(self) -> float:
        return self._ctl.custom_w

    async def async_set_native_value(self, value: float) -> None:
        await self._ctl.async_set_custom(value)
        self.async_write_ha_state()
