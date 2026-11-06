"""Override select for Battery Plan."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import BatteryPlanConfigEntry
from .const import OVERRIDES
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: BatteryPlanConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    async_add_entities([OverrideSelect(entry)])


class OverrideSelect(SelectEntity, RestoreEntity):
    """auto follows the plan; the others replace it (export floor still applies)."""

    _attr_has_entity_name = True
    _attr_translation_key = "override"
    _attr_options = OVERRIDES
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: BatteryPlanConfigEntry) -> None:
        self._ctl = entry.runtime_data.controller
        self._attr_unique_id = f"{entry.entry_id}_override"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state in OVERRIDES and last.state != "auto":
            await self._ctl.async_set_override(last.state)
        self.async_on_remove(self._ctl.add_listener(self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def current_option(self) -> str:
        return self._ctl.override

    async def async_select_option(self, option: str) -> None:
        await self._ctl.async_set_override(option)
