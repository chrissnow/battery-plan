"""Constants for Battery Plan."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "battery_plan"
VERSION = "0.1.1"
UPDATE_INTERVAL = timedelta(minutes=5)
LOAD_PROFILE_REFRESH = timedelta(hours=1)
CARD_URL = "/battery_plan/battery-plan-card.js"

# Sections in the settings form
SEC_BATTERY = "battery"
SEC_TARIFF = "tariff"
SEC_CHARGING = "charging"
SEC_EXPORT = "export"
SEC_LOAD = "load"
SEC_OCTOPUS = "octopus"
SEC_ACTUALS = "actuals"

# Battery
CONF_SOC_ENTITY = "soc_entity"
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_TARGET_SOC = "target_soc"
CONF_RESERVE_SOC = "reserve_soc"
CONF_MIN_SOC = "min_soc"

# Tariff (Octopus Energy integration rate event entities)
CONF_IMPORT_RATES = "import_rates"
CONF_EXPORT_RATES = "export_rates"
CONF_STANDING_CHARGE = "standing_charge_entity"

# Charging
CONF_CHARGE_START = "charge_start"
CONF_CHARGE_HOURS = "charge_hours"
CONF_CHARGE_MODE = "charge_mode"
CONF_MAX_CHARGE_KW = "max_charge_kw"
CONF_MIN_CHARGE_KW = "min_charge_kw"
CONF_MAKEUP_KW = "makeup_kw"
CONF_FINISH_EARLY_MIN = "finish_early_min"
CONF_FINISH_EARLY_FULL_MIN = "finish_early_full_min"
CONF_LONG_BALANCE_DAY = "long_balance_day"
CONF_LONG_BALANCE_MIN = "long_balance_min"
CONF_CHARGE_EFF = "charge_eff"

# Export
CONF_MAX_EXPORT_KW = "max_export_kw"
CONF_SAVING_EXPORT_KW = "saving_export_kw"
CONF_MIN_EXPORT_PRICE = "min_export_price"
CONF_DISCHARGE_EFF = "discharge_eff"

# Load
CONF_LOAD_ENTITIES = "load_entities"
CONF_LOAD_EXCLUDE = "load_exclude_entities"
CONF_LOAD_DAYS = "load_days"
CONF_LOAD_KW = "load_kw"

# Octopus extras
CONF_DISPATCHING_ENTITY = "dispatching_entity"
CONF_DISPATCH_CHARGES = "dispatch_charges_battery"
CONF_TARGET_TIME_ENTITY = "target_time_entity"
CONF_SAVING_CALENDAR = "saving_calendar"
CONF_FREE_CALENDAR = "free_calendar"

# Actuals
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_GRID_POWER_INVERT = "grid_power_invert"

CHARGE_MODES = ["spread", "max"]
WEEKDAYS = ["none", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULTS: dict = {
    CONF_TARGET_SOC: 100,
    CONF_RESERVE_SOC: 20,
    CONF_MIN_SOC: 5,
    CONF_CHARGE_START: "23:30:00",
    CONF_CHARGE_HOURS: 6,
    CONF_CHARGE_MODE: "max",
    CONF_MAX_CHARGE_KW: 5.0,
    CONF_MIN_CHARGE_KW: 0.5,
    CONF_MAKEUP_KW: 0.25,
    CONF_FINISH_EARLY_MIN: 15,
    CONF_FINISH_EARLY_FULL_MIN: 30,
    CONF_LONG_BALANCE_DAY: "none",
    CONF_LONG_BALANCE_MIN: 60,
    CONF_CHARGE_EFF: 0.93,
    CONF_MAX_EXPORT_KW: 5.0,
    CONF_MIN_EXPORT_PRICE: 12.5,
    CONF_DISCHARGE_EFF: 0.9,
    CONF_LOAD_DAYS: 7,
    CONF_LOAD_KW: 0.8,
    CONF_DISPATCH_CHARGES: True,
    CONF_GRID_POWER_INVERT: False,
}

SECTIONS: dict[str, list[str]] = {
    SEC_BATTERY: [CONF_SOC_ENTITY, CONF_CAPACITY_KWH, CONF_TARGET_SOC, CONF_RESERVE_SOC, CONF_MIN_SOC],
    SEC_TARIFF: [CONF_IMPORT_RATES, CONF_EXPORT_RATES, CONF_STANDING_CHARGE],
    SEC_CHARGING: [
        CONF_CHARGE_START,
        CONF_CHARGE_HOURS,
        CONF_CHARGE_MODE,
        CONF_MAX_CHARGE_KW,
        CONF_MIN_CHARGE_KW,
        CONF_MAKEUP_KW,
        CONF_FINISH_EARLY_MIN,
        CONF_FINISH_EARLY_FULL_MIN,
        CONF_LONG_BALANCE_DAY,
        CONF_LONG_BALANCE_MIN,
        CONF_CHARGE_EFF,
    ],
    SEC_EXPORT: [CONF_MAX_EXPORT_KW, CONF_SAVING_EXPORT_KW, CONF_MIN_EXPORT_PRICE, CONF_DISCHARGE_EFF],
    SEC_LOAD: [CONF_LOAD_ENTITIES, CONF_LOAD_EXCLUDE, CONF_LOAD_DAYS, CONF_LOAD_KW],
    SEC_OCTOPUS: [
        CONF_DISPATCHING_ENTITY,
        CONF_DISPATCH_CHARGES,
        CONF_TARGET_TIME_ENTITY,
        CONF_SAVING_CALENDAR,
        CONF_FREE_CALENDAR,
    ],
    SEC_ACTUALS: [CONF_GRID_POWER_ENTITY, CONF_GRID_POWER_INVERT],
}
