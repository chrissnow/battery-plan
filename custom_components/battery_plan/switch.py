"""Control switch for Battery Plan (off by default)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON, EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import BatteryPlanConfigEntry
from .const import CONF_CURRENT_ENTITY, CONF_SETPOINT_ENTITY
from .sensor import device_info


async def async_setup_entry(
    hass: HomeAssistant, entry: BatteryPlanConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    c = entry.runtime_data
    ents: list[ControlSwitch] = []
    if c.opts.get(CONF_SETPOINT_ENTITY):
        ents.append(ControlSwitch(entry, "setpoint"))
    if c.opts.get(CONF_CURRENT_ENTITY):
        ents.append(ControlSwitch(entry, "current"))
    async_add_entities(ents)


class ControlSwitch(SwitchEntity, RestoreEntity):
    """When on, Battery Plan writes the grid setpoint (+ EV charger) or the charge current."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: BatteryPlanConfigEntry, part: str) -> None:
        self._ctl = entry.runtime_data.controller
        self._part = part
        # the grid setpoint switch keeps the original "control" id so existing setups keep their entity
        self._attr_translation_key = "control" if part == "setpoint" else "charge_current_control"
        self._attr_unique_id = f"{entry.entry_id}_control" if part == "setpoint" else f"{entry.entry_id}_charge_current_control"
        self._attr_device_info = device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == STATE_ON:
            await self._ctl.async_set_enabled(True, self._part)
        self.async_on_remove(self._ctl.add_listener(self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._ctl.current_on if self._part == "current" else self._ctl.setpoint_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._ctl.attributes()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._ctl.async_set_enabled(True, self._part)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._ctl.async_set_enabled(False, self._part)
