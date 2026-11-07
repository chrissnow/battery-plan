"""Battery Plan: plan home battery charge/export against Octopus tariffs."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import ConfigType

from .const import CARD_URL, DOMAIN, VERSION
from .coordinator import BatteryPlanCoordinator

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.SELECT, Platform.NUMBER]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type BatteryPlanConfigEntry = ConfigEntry[BatteryPlanCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve the bundled Lovelace card and register it as a dashboard resource."""
    if hass.http is not None:
        path = Path(__file__).parent / "www" / "battery-plan-card.js"
        await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(path), False)])
    try:
        registered = await _register_card(hass)
    except Exception as err:  # noqa: BLE001 - never block setup over the card
        _LOGGER.warning("Battery Plan could not register its card as a dashboard resource: %s", err)
        registered = False
    if not registered and "frontend" in hass.config.components:
        add_extra_js_url(hass, f"{CARD_URL}?v={VERSION}")  # YAML-mode resources
    return True


async def _register_card(hass: HomeAssistant) -> bool:
    """Add or update our card in storage-mode dashboard resources.

    A resource loads reliably on every dashboard (including the mobile app),
    and the version query makes browsers fetch a new card after an update.
    """
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    ll = hass.data.get(LOVELACE_DATA)
    if ll is None or ll.resource_mode != "storage":
        return False
    res = ll.resources
    if not res.loaded:
        await res.async_load()
        res.loaded = True
    url = f"{CARD_URL}?v={VERSION}"
    mine = [r for r in res.async_items() if str(r.get("url", "")).split("?")[0] == CARD_URL]
    if not mine:
        await res.async_create_item({"res_type": "module", "url": url})
        return True
    for extra in mine[1:]:
        await res.async_delete_item(extra["id"])
    if mine[0].get("url") != url:
        await res.async_update_item(mine[0]["id"], {"res_type": "module", "url": url})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BatteryPlanConfigEntry) -> bool:
    coordinator = BatteryPlanCoordinator(hass, entry)
    await coordinator.costs.async_start()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    @callback
    def _replan(event: Event[EventStateChangedData]) -> None:
        old, new = event.data["old_state"], event.data["new_state"]
        if old is None or new is None or old.state != new.state or old.attributes != new.attributes:
            coordinator.hass.async_create_task(coordinator.async_request_refresh())

    entry.async_on_unload(async_track_state_change_event(hass, coordinator.watched_entities(), _replan))
    entry.async_on_unload(entry.add_update_listener(_reload))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.controller.async_start()
    return True


async def _reload(hass: HomeAssistant, entry: BatteryPlanConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BatteryPlanConfigEntry) -> bool:
    await entry.runtime_data.controller.async_stop()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.costs.async_stop()
    return ok
