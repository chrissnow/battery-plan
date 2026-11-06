"""Config and options flow for Battery Plan."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import selector as sel

from .const import (
    CHARGE_MODES,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFF,
    CONF_CHARGE_HOURS,
    CONF_CHARGE_MODE,
    CONF_CHARGE_START,
    CONF_DISCHARGE_EFF,
    CONF_DISPATCH_CHARGES,
    CONF_DISPATCHING_ENTITY,
    CONF_EXPORT_RATES,
    CONF_FINISH_EARLY_FULL_MIN,
    CONF_FINISH_EARLY_MIN,
    CONF_FREE_CALENDAR,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_INVERT,
    CONF_BATTERY_POWER_ENTITY,
    CONF_BATTERY_POWER_INVERT,
    CONF_SETPOINT_ENTITY,
    CONF_CURRENT_ENTITY,
    CONF_IDLE_SETPOINT_W,
    CONF_CHARGE_SETPOINT_W,
    CONF_SETPOINT_DEADBAND_W,
    CONF_BATTERY_VOLTAGE,
    CONF_STALE_MIN,
    CONF_HEARTBEAT_TOPIC,
    CONF_IMPORT_RATES,
    CONF_LOAD_DAYS,
    CONF_LOAD_ENTITIES,
    CONF_LOAD_EXCLUDE,
    CONF_LOAD_KW,
    CONF_LONG_BALANCE_DAY,
    CONF_LONG_BALANCE_MIN,
    CONF_MAKEUP_KW,
    CONF_MAX_CHARGE_KW,
    CONF_MAX_EXPORT_KW,
    CONF_MIN_CHARGE_KW,
    CONF_MIN_EXPORT_PRICE,
    CONF_MIN_SOC,
    CONF_RESERVE_SOC,
    CONF_SAVING_CALENDAR,
    CONF_SAVING_EXPORT_KW,
    CONF_SOC_ENTITY,
    CONF_STANDING_CHARGE,
    CONF_TARGET_SOC,
    CONF_TARGET_TIME_ENTITY,
    DEFAULTS,
    DOMAIN,
    SEC_ACTUALS,
    SEC_BATTERY,
    SEC_CHARGING,
    SEC_EXPORT,
    SEC_LOAD,
    SEC_OCTOPUS,
    SEC_TARIFF,
    SECTIONS,
    WEEKDAYS,
)


def _num(lo: float, hi: float, step: float, unit: str | None = None) -> sel.NumberSelector:
    cfg: dict[str, Any] = {"min": lo, "max": hi, "step": step, "mode": sel.NumberSelectorMode.BOX}
    if unit:
        cfg["unit_of_measurement"] = unit
    return sel.NumberSelector(sel.NumberSelectorConfig(**cfg))


def _ent(domain: str | list[str], multiple: bool = False, device_class: str | None = None) -> sel.EntitySelector:
    cfg: dict[str, Any] = {"domain": domain, "multiple": multiple}
    if device_class:
        cfg["device_class"] = device_class
    return sel.EntitySelector(sel.EntitySelectorConfig(**cfg))


# key -> (selector, required)
FIELDS: dict[str, tuple[Any, bool]] = {
    CONF_SOC_ENTITY: (_ent("sensor", device_class="battery"), True),
    CONF_CAPACITY_KWH: (_num(1, 500, 0.1, "kWh"), True),
    CONF_TARGET_SOC: (_num(10, 100, 1, "%"), True),
    CONF_RESERVE_SOC: (_num(0, 100, 1, "%"), True),
    CONF_MIN_SOC: (_num(0, 100, 1, "%"), True),
    CONF_IMPORT_RATES: (_ent("event", multiple=True), True),
    CONF_EXPORT_RATES: (_ent("event", multiple=True), False),
    CONF_STANDING_CHARGE: (_ent("sensor"), False),
    CONF_CHARGE_START: (sel.TimeSelector(), True),
    CONF_CHARGE_HOURS: (_num(0.5, 24, 0.5, "h"), True),
    CONF_CHARGE_MODE: (
        sel.SelectSelector(sel.SelectSelectorConfig(options=CHARGE_MODES, translation_key="charge_mode")),
        True,
    ),
    CONF_MAX_CHARGE_KW: (_num(0.1, 100, 0.01, "kW"), True),
    CONF_MIN_CHARGE_KW: (_num(0, 100, 0.01, "kW"), True),
    CONF_MAKEUP_KW: (_num(0, 20, 0.01, "kW"), True),
    CONF_FINISH_EARLY_MIN: (_num(0, 240, 1, "min"), True),
    CONF_FINISH_EARLY_FULL_MIN: (_num(0, 240, 1, "min"), True),
    CONF_LONG_BALANCE_DAY: (
        sel.SelectSelector(sel.SelectSelectorConfig(options=WEEKDAYS, translation_key="weekday")),
        True,
    ),
    CONF_LONG_BALANCE_MIN: (_num(0, 480, 1, "min"), True),
    CONF_CHARGE_EFF: (_num(0.5, 1, 0.01), True),
    CONF_MAX_EXPORT_KW: (_num(0, 100, 0.1, "kW"), True),
    CONF_SAVING_EXPORT_KW: (_num(0, 100, 0.1, "kW"), False),
    CONF_MIN_EXPORT_PRICE: (_num(-100, 200, 0.1, "p/kWh"), True),
    CONF_DISCHARGE_EFF: (_num(0.5, 1, 0.01), True),
    CONF_LOAD_ENTITIES: (_ent("sensor", multiple=True, device_class="power"), False),
    CONF_LOAD_EXCLUDE: (_ent("sensor", multiple=True, device_class="power"), False),
    CONF_LOAD_DAYS: (_num(1, 28, 1, "d"), True),
    CONF_LOAD_KW: (_num(0, 50, 0.05, "kW"), True),
    CONF_DISPATCHING_ENTITY: (_ent("binary_sensor"), False),
    CONF_DISPATCH_CHARGES: (sel.BooleanSelector(), True),
    CONF_TARGET_TIME_ENTITY: (_ent(["select", "time"]), False),
    CONF_SAVING_CALENDAR: (_ent("calendar"), False),
    CONF_FREE_CALENDAR: (_ent("calendar"), False),
    CONF_GRID_POWER_ENTITY: (_ent("sensor", device_class="power"), False),
    CONF_GRID_POWER_INVERT: (sel.BooleanSelector(), True),
    CONF_BATTERY_POWER_ENTITY: (_ent("sensor", device_class="power"), False),
    CONF_BATTERY_POWER_INVERT: (sel.BooleanSelector(), True),
    CONF_SETPOINT_ENTITY: (_ent("number"), False),
    CONF_CURRENT_ENTITY: (_ent("number"), False),
    CONF_IDLE_SETPOINT_W: (_num(-5000, 5000, 1, "W"), True),
    CONF_CHARGE_SETPOINT_W: (_num(-5000, 5000, 1, "W"), True),
    CONF_SETPOINT_DEADBAND_W: (_num(0, 2000, 10, "W"), True),
    CONF_BATTERY_VOLTAGE: (_num(10, 1000, 0.1, "V"), True),
    CONF_STALE_MIN: (_num(2, 120, 1, "min"), True),
    CONF_HEARTBEAT_TOPIC: (sel.TextSelector(), False),
}

COLLAPSED = {SEC_BATTERY: False, SEC_TARIFF: False}


def build_schema(values: dict[str, Any], sections: list[str] | None = None) -> vol.Schema:
    """Settings form: one collapsible section per group."""
    out: dict[Any, Any] = {}
    for sec_key, keys in SECTIONS.items():
        if sections is not None and sec_key not in sections:
            continue
        inner: dict[Any, Any] = {}
        for key in keys:
            selector, required = FIELDS[key]
            cur = values.get(key, DEFAULTS.get(key))
            if required:
                inner[vol.Required(key, default=cur) if cur is not None else vol.Required(key)] = selector
            else:
                # suggested_value lets the user clear an optional entity
                inner[vol.Optional(key, description={"suggested_value": cur})] = selector
        key = vol.Required(sec_key) if sec_key in COLLAPSED else vol.Optional(sec_key, default={})
        out[key] = section(vol.Schema(inner), {"collapsed": COLLAPSED.get(sec_key, True)})
    return vol.Schema(out)


def flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    """Section-nested form data -> flat options dict (drops cleared optionals)."""
    flat: dict[str, Any] = {}
    for sec_key, keys in SECTIONS.items():
        data = user_input.get(sec_key) or {}
        for key in keys:
            v = data.get(key)
            if v in (None, "", []):
                continue
            flat[key] = v
    return flat


def validate(flat: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    if flat.get(CONF_RESERVE_SOC, 0) >= flat.get(CONF_TARGET_SOC, 100):
        errors["base"] = "reserve_above_target"
    elif flat.get(CONF_MIN_SOC, 0) > flat.get(CONF_RESERVE_SOC, 0):
        errors["base"] = "min_above_reserve"
    elif flat.get(CONF_MIN_CHARGE_KW, 0) > flat.get(CONF_MAX_CHARGE_KW, 0):
        errors["base"] = "min_charge_above_max"
    return errors


class BatteryPlanConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two steps: name, then settings."""

    VERSION = 1

    def __init__(self) -> None:
        self._name = "Battery Plan"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._name = user_input[CONF_NAME]
            return await self.async_step_settings()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_NAME, default=self._name): sel.TextSelector()}),
        )

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        values: dict[str, Any] = {}
        if user_input is not None:
            values = flatten(user_input)
            errors = validate(values)
            if not errors:
                return self.async_create_entry(title=self._name, data={CONF_NAME: self._name}, options=values)
        return self.async_show_form(step_id="settings", data_schema=build_schema(values), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BatteryPlanOptionsFlow()


class BatteryPlanOptionsFlow(OptionsFlow):
    """Edit all settings."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        values = dict(self.config_entry.options)
        if user_input is not None:
            values = flatten(user_input)
            errors = validate(values)
            if not errors:
                return self.async_create_entry(data=values)
        return self.async_show_form(step_id="init", data_schema=build_schema(values), errors=errors)
