"""Constants for Battery Plan."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "battery_plan"
VERSION = "0.6.4"
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
SEC_CONTROL = "control"

# Battery
CONF_SOC_ENTITY = "soc_entity"
CONF_CAPACITY_KWH = "capacity_kwh"
CONF_TARGET_SOC = "target_soc"
CONF_RESERVE_SOC = "reserve_soc"
CONF_MIN_SOC = "min_soc"
CONF_DAY_RESERVE_SOC = "day_reserve_soc"
CONF_DAY_RESERVE_START = "day_reserve_start"
CONF_DAY_RESERVE_END = "day_reserve_end"

# Tariff (Octopus Energy integration rate event entities)
CONF_IMPORT_RATES = "import_rates"
CONF_EXPORT_RATES = "export_rates"
CONF_STANDING_CHARGE = "standing_charge_entity"

# Charging
CONF_SCHEDULE_START_ENTITY = "schedule_start_entity"
CONF_SCHEDULE_DURATION_ENTITY = "schedule_duration_entity"
CONF_SCHEDULE_SOC_ENTITY = "schedule_soc_entity"
CONF_CONSUMED_AH_ENTITY = "consumed_ah_entity"
CONF_CHARGE_START = "charge_start"
CONF_CHARGE_HOURS = "charge_hours"
CONF_WINDOW_FROM_RATES = "charge_window_from_rates"
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
CONF_EXPORT_MARGIN = "export_margin"
CONF_EXPORT_PEAK_IMPORT = "export_peak_import"
CONF_BUY_AHEAD_H = "buy_ahead_hours"
CONF_NIGHT_CYCLE = "night_cycle"
CONF_CHARGE_SOC_WINDOW = "charge_schedule_window"
CONF_LIMITED_EXPORT_KW = "limited_export_kw"
CONF_EXPORT_TRACKING = "export_tracking"
CONF_EXPORT_RUN_RATE_P = "export_run_rate_p"
CONF_DISCHARGE_EFF = "discharge_eff"

# Load
CONF_LOAD_ENTITIES = "load_entities"
CONF_LOAD_EXCLUDE = "load_exclude_entities"
CONF_LOAD_DAYS = "load_days"
CONF_LOAD_KW = "load_kw"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_OUTDOOR_TEMP_ENTITY = "outdoor_temp_entity"
CONF_WEATHER_LOAD_ENTITIES = "weather_load_entities"
CONF_WEATHER_COOLING = "weather_cooling"
CONF_WEATHER_DAYS = "weather_days"
WEATHER_FIT_REFRESH = timedelta(hours=6)
WEATHER_FORECAST_REFRESH = timedelta(minutes=30)

# Octopus extras
CONF_DISPATCHING_ENTITY = "dispatching_entity"
CONF_DISPATCH_CHARGES = "dispatch_charges_battery"
CONF_TARGET_TIME_ENTITY = "target_time_entity"
CONF_SAVING_CALENDAR = "saving_calendar"
CONF_FREE_CALENDAR = "free_calendar"
CONF_FREE_CAP_KWH = "free_cap_kwh"
CONF_POWER_UP_CALENDAR = "power_up_calendar"

# Control (optional, off by default)
CONF_SETPOINT_ENTITY = "grid_setpoint_entity"
CONF_CURRENT_ENTITY = "charge_current_entity"
CONF_CHARGE_SOC_ENTITY = "charge_schedule_soc_entity"
CONF_CHARGE_SOC_IDLE = "charge_schedule_idle_soc"
CONF_IDLE_SETPOINT_W = "idle_setpoint_w"
CONF_CHARGE_SETPOINT_W = "charge_setpoint_w"
CONF_SETPOINT_DEADBAND_W = "setpoint_deadband_w"
CONF_CURRENT_DEADBAND_A = "current_deadband_a"
CONF_BATTERY_VOLTAGE = "battery_voltage"
CONF_STALE_MIN = "stale_minutes"
CONF_HEARTBEAT_TOPIC = "heartbeat_topic"
CONF_EV_ALLOW_ENTITY = "ev_allow_entity"
CONF_EV_ALLOW_INVERT = "ev_allow_invert"
EV_INHIBIT_W = -200
OVERRIDES = ["auto", "hold", "max_export", "limited_export", "saving", "zero", "custom"]
CONTROL_INTERVAL = timedelta(seconds=30)

# Actuals
CONF_GRID_POWER_ENTITY = "grid_power_entity"
CONF_GRID_POWER_INVERT = "grid_power_invert"
CONF_BATTERY_POWER_ENTITY = "battery_power_entity"
CONF_BATTERY_POWER_INVERT = "battery_power_invert"
CONF_USE_ENERGY_COSTS = "use_energy_costs"
ACTION_ON_KW = 0.5
ACTION_OFF_KW = 0.2

CHARGE_MODES = ["spread", "max"]
WEEKDAYS = ["none", "mon", "tue", "wed", "thu", "fri", "sat", "sun"]

DEFAULTS: dict = {
    CONF_TARGET_SOC: 100,
    CONF_RESERVE_SOC: 20,
    CONF_DAY_RESERVE_START: "05:25:00",
    CONF_DAY_RESERVE_END: "22:25:00",
    CONF_LIMITED_EXPORT_KW: 4.0,
    CONF_EXPORT_TRACKING: True,
    CONF_EXPORT_RUN_RATE_P: 1.0,
    CONF_EXPORT_PEAK_IMPORT: False,
    CONF_BUY_AHEAD_H: 3,
    CONF_NIGHT_CYCLE: False,
    CONF_CHARGE_SOC_WINDOW: False,
    CONF_EV_ALLOW_INVERT: False,
    CONF_MIN_SOC: 5,
    CONF_CHARGE_START: "23:30:00",
    CONF_CHARGE_HOURS: 6,
    CONF_WINDOW_FROM_RATES: False,
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
    CONF_EXPORT_MARGIN: 5,
    CONF_DISCHARGE_EFF: 0.9,
    CONF_LOAD_DAYS: 7,
    CONF_LOAD_KW: 0.8,
    CONF_WEATHER_COOLING: False,
    CONF_WEATHER_DAYS: 21,
    CONF_DISPATCH_CHARGES: True,
    CONF_FREE_CAP_KWH: 16,
    CONF_GRID_POWER_INVERT: False,
    CONF_BATTERY_POWER_INVERT: False,
    CONF_USE_ENERGY_COSTS: True,
    CONF_IDLE_SETPOINT_W: -50,
    CONF_CHARGE_SETPOINT_W: 0,
    CONF_SETPOINT_DEADBAND_W: 100,
    CONF_CURRENT_DEADBAND_A: 10,
    CONF_BATTERY_VOLTAGE: 51.2,
    CONF_STALE_MIN: 15,
    CONF_CHARGE_SOC_IDLE: 0,
}

SECTIONS: dict[str, list[str]] = {
    SEC_BATTERY: [
        CONF_SOC_ENTITY,
        CONF_CAPACITY_KWH,
        CONF_TARGET_SOC,
        CONF_RESERVE_SOC,
        CONF_DAY_RESERVE_SOC,
        CONF_DAY_RESERVE_START,
        CONF_DAY_RESERVE_END,
        CONF_MIN_SOC,
    ],
    SEC_TARIFF: [CONF_IMPORT_RATES, CONF_EXPORT_RATES, CONF_STANDING_CHARGE],
    SEC_CHARGING: [
        CONF_SCHEDULE_START_ENTITY,
        CONF_SCHEDULE_DURATION_ENTITY,
        CONF_SCHEDULE_SOC_ENTITY,
        CONF_CONSUMED_AH_ENTITY,
        CONF_WINDOW_FROM_RATES,
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
    SEC_EXPORT: [
        CONF_MAX_EXPORT_KW,
        CONF_SAVING_EXPORT_KW,
        CONF_LIMITED_EXPORT_KW,
        CONF_EXPORT_MARGIN,
        CONF_EXPORT_PEAK_IMPORT,
        CONF_BUY_AHEAD_H,
        CONF_NIGHT_CYCLE,
        CONF_EXPORT_TRACKING,
        CONF_EXPORT_RUN_RATE_P,
        CONF_DISCHARGE_EFF,
    ],
    SEC_LOAD: [
        CONF_LOAD_ENTITIES,
        CONF_LOAD_EXCLUDE,
        CONF_LOAD_DAYS,
        CONF_LOAD_KW,
        CONF_WEATHER_ENTITY,
        CONF_OUTDOOR_TEMP_ENTITY,
        CONF_WEATHER_LOAD_ENTITIES,
        CONF_WEATHER_COOLING,
        CONF_WEATHER_DAYS,
    ],
    SEC_OCTOPUS: [
        CONF_DISPATCHING_ENTITY,
        CONF_DISPATCH_CHARGES,
        CONF_TARGET_TIME_ENTITY,
        CONF_SAVING_CALENDAR,
        CONF_FREE_CALENDAR,
        CONF_FREE_CAP_KWH,
        CONF_POWER_UP_CALENDAR,
    ],
    SEC_ACTUALS: [
        CONF_USE_ENERGY_COSTS,
        CONF_GRID_POWER_ENTITY,
        CONF_GRID_POWER_INVERT,
        CONF_BATTERY_POWER_ENTITY,
        CONF_BATTERY_POWER_INVERT,
    ],
    SEC_CONTROL: [
        CONF_SETPOINT_ENTITY,
        CONF_CURRENT_ENTITY,
        CONF_CHARGE_SOC_ENTITY,
        CONF_CHARGE_SOC_IDLE,
        CONF_CHARGE_SOC_WINDOW,
        CONF_IDLE_SETPOINT_W,
        CONF_CHARGE_SETPOINT_W,
        CONF_SETPOINT_DEADBAND_W,
        CONF_CURRENT_DEADBAND_A,
        CONF_BATTERY_VOLTAGE,
        CONF_STALE_MIN,
        CONF_HEARTBEAT_TOPIC,
        CONF_EV_ALLOW_ENTITY,
        CONF_EV_ALLOW_INVERT,
    ],
}
