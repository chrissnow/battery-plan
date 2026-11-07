# Battery Plan

A Home Assistant integration that plans a home battery's overnight charge and evening exports against Octopus Energy tariffs, forecasts state of charge, and estimates what each day will cost or earn. It ships with a Lovelace card.

![Battery Plan card](docs/card.png)

It plans and reports by default. It can **optionally** drive your inverter's grid setpoint and charge current, each with its own switch, both off until you turn them on (see [Control](#control-optional)).

## What it does

Every 5 minutes, and whenever rates or Intelligent Octopus dispatches change, it simulates the next 36 hours in half-hour slots:

- **Overnight charge window**: charges to your target, at full power or spread so it finishes shortly before the window ends (like a DVCC charge-current spreader). The house runs from grid during the window.
- **Intelligent Octopus Go dispatches**: planned dispatches outside the window are treated as full-rate top-ups (optional).
- **Octoplus**: saving sessions export at a higher power; free-electricity sessions charge at full power, and the battery is run down beforehand to make room.
- **Exports**: before each charge window it keeps enough for the house load plus an export floor, then exports the rest in the best-priced half hours paying at least your minimum price. The floor can be higher during the day (e.g. 25% from 05:25 to 22:25, 20% overnight), and the minimum price can be lower before a free-electricity session so the battery empties to make room.
- **House load**: learned as a half-hourly profile from your power sensors' statistics (last N days), minus any excluded sensors such as an EV charger, or a flat figure. Half hours that aren't normal house load are left out: Intelligent Octopus dispatches and grid charging outside the charge window, and Octoplus free-electricity and saving sessions. A half hour that's affected every day (e.g. a daily 07:30 dispatch) is filled in from the half hours either side.
- **Weather-dependent load** (optional): learns how much your load rises in cold (and optionally hot) weather and adjusts the house load for the forecast temperature. See [Weather-dependent load](#weather-dependent-load-optional).
- **Costs**: planned import cost and export earnings per day, from your import and export rates. Actual costs come from the Energy dashboard's grid cost statistics when it has priced grid meters (so they match it), otherwise from a grid power sensor × live rates.

## Requirements

- Home Assistant 2025.4 or newer
- The [Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) integration (BottlecapDave): the planner reads its rate event entities, dispatching sensor and Octoplus calendars
- A battery state-of-charge sensor (any inverter)

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repository's URL with type **Integration**.
2. Install **Battery Plan** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Battery Plan**. Only the Battery and Tariff sections need filling in: the SOC sensor, capacity and import rate events. Everything else has defaults you can change later under **Configure**.

The card is added to your dashboard resources automatically (and updated with each release). If a dashboard shows "Configuration error" right after an update, reload it or, in the mobile app, use **Settings → Companion app → Troubleshooting → Reset frontend cache**. Add it with **Add card → Battery Plan**, or:

```yaml
type: custom:battery-plan-card
entity: sensor.battery_plan_soc_at_next_charge
```

## Settings

| Section | Setting | Notes |
|:--|:--|:--|
| Battery | SOC sensor, usable capacity, charge target, export floor, optional daytime floor with start/end times, minimum SOC | Exports stop at the floor in force at the time; the battery stops discharging at the minimum |
| Tariff | Import rate events, export rate events, standing charge sensor | Pick the Octopus Energy `…_current_day_rates` **and** `…_next_day_rates` events for each meter |
| Overnight charging | Optional inverter schedule entities (start, duration, target SOC) and BMS consumed-Ah sensor; window start and length, full power or spread, max/min/make-up power, finish-early minutes, an optional "long balance" night, charge efficiency | Schedule entities (e.g. Victron ESS schedule) override the window and target whenever they're available; the consumed-Ah sensor sizes spread charging from the BMS capacity |
| Export | Max export power, saving-session power, limited-export power, minimum export price, minimum price before a free session (p/kWh), SOC tracking, discharge efficiency | |
| House load | Power sensors (summed, e.g. AC loads per phase), sensors to exclude (e.g. EV charger), days of history, flat fallback; weather forecast, outdoor temperature sensor, weather-dependent load sensors, cooling on/off, days to learn from | Uses 5-minute statistics where the recorder still has them, hourly otherwise. The weather settings are all optional |
| Intelligent Octopus & Octoplus | Dispatching sensor, whether dispatches charge the battery, ready-time entity, saving-session and free-electricity calendars | All optional |
| Actual costs | Use Energy dashboard costs (on by default), grid power sensor (import positive), battery power sensor (charging positive), invert switches | Actual cost sensors appear if the Energy dashboard has priced grid meters or a grid power sensor is set. The battery power sensor makes the Battery action sensor more accurate |
| Control (optional) | Grid setpoint entity, charge current entity, idle and charging setpoints, deadband, battery voltage, stale timeout, MQTT heartbeat topic, EV charger allow switch (+ invert) | Nothing is written until the matching control switch is on |

Everything can be changed later under **Configure**.

## Entities

Entity IDs below assume the default name "Battery Plan".

| Entity | Description |
|:--|:--|
| `sensor.battery_plan_soc_at_next_charge` | Predicted SOC when the next charge window starts. Attributes hold the full plan: `forecast` (per half hour), `plan` (blocks), `days` (planned cost per day), `load_profile_kw`, `load_masked_slots` (half hours left out of the load profile) |
| `sensor.battery_plan_lowest_soc_ahead` | Lowest SOC in the forecast |
| `sensor.battery_plan_next_action` | `charge`, `export`, `iog`, `saving`, `hold`, `grid` or `self`, with `start`/`end` attributes |
| `sensor.battery_plan_planned_export` | Planned export over the horizon (kWh) |
| `sensor.battery_plan_net_today_estimate` | Today's actual so far (if a grid power sensor is set) plus the plan for the rest of today, minus standing charge |
| `sensor.battery_plan_net_tomorrow_plan` | Tomorrow's planned export earnings minus import cost and standing charge |
| `sensor.battery_plan_import_cost_today`, `…_export_earnings_today`, `…_net_today`, `…_net_yesterday` | Actual costs: from the Energy dashboard's grid cost and compensation statistics (`source: energy_dashboard`, refreshed every 5 minutes), or grid power × live rates (`source: grid_power`). Net figures subtract the standing charge |
| `sensor.battery_plan_dispatch_hours_before_ready` | Hours of planned IOG dispatch in the 24 h before the ready time, with overlaps merged (needs dispatching + ready-time entities) |
| `binary_sensor.battery_plan_final_slot_scheduled` | On when a dispatch covers the half hour before the ready time |
| `sensor.battery_plan_battery_action` | What the battery is actually doing now (`export`, `charge`, `iog`, `saving`, `hold`, `grid`, `self`), from grid/battery power and the plan. Its history gives the card's past bands |
| `sensor.battery_plan_target_grid_setpoint` | Grid setpoint the controller wants now (W): always computed, written only when Control is on |
| `sensor.battery_plan_target_charge_current` | Charge current limit the controller wants now (A) |
| `sensor.battery_plan_control_status` | `disabled` (dry run), `active` or `fallback`, with a `reason` attribute |
| `sensor.battery_plan_weather_load_today`, `…_tomorrow` | Estimated heating/cooling load (kWh), with half-hourly `slots` (kW and forecast temperature). Only with a weather entity set |
| `sensor.battery_plan_heating_sensitivity` | Diagnostic: learned kW per °C, with the balance point, fit quality, days learned from and `status` (`active` or `not_enough_data` with a `reason`) |
| `switch.battery_plan_grid_setpoint_control` | Writes the grid setpoint (and the EV charger switch). Off by default; created when a setpoint entity is set. Installs from before 0.4.0 keep the id `switch.battery_plan_control` |
| `switch.battery_plan_charge_current_control` | Writes the charge current limit. Off by default; created when a charge current entity is set |
| `select.battery_plan_override` | `auto` (follow the plan), `hold`, `max_export`, `limited_export`, `saving`, `zero` or `custom`. Export overrides still stop at the floor |
| `number.battery_plan_custom_setpoint` | Grid setpoint (W) used by the `custom` override |
| `binary_sensor.battery_plan_ev_inhibit` | On while exporting 200 W or more outside the charge window, so an EV charger doesn't drain the battery |

## Weather-dependent load (optional)

Pick a weather entity with hourly forecasts (e.g. `weather.home`) under **House load** and the plan adjusts the house load for the forecast temperature:

- **What it learns from**: your weather-dependent load sensors if you set them (e.g. heat pump heating input power), otherwise the whole-house load. Outdoor temperature comes from your sensor if set, otherwise the weather entity's recorded temperature.
- **Model**: a baseline for each hour of the day (so your own setback and boost times are kept) plus one sensitivity in kW per °C below a heating balance point, and optionally above a cooling one. Balance points are learned (10–18 °C heating, 18–26 °C cooling). The outdoor temperature is smoothed over the previous hours, as buildings respond slowly. Dispatches, Octoplus sessions and, for whole-house learning, the charge window are left out.
- **Forecast**: each half hour's load = the 7-day schedule + sensitivity × how much colder (or hotter) the forecast is than the same hour over those 7 days.
- **Only when supported by the data**: at least 7 days, enough hours past the balance point, a sensible sensitivity, and the weather explaining at least 3% of the hour-to-hour variation. Otherwise the status says why and the plan uses the plain schedule. A gas-heated home or a mild month just gets "not in use yet".
- **Hot water** usually runs off-peak and barely depends on the weather, so leave its sensors out.

The card then shows a load strip under the SOC chart (base load in grey, heating in orange, cooling in blue, forecast temperature dashed, past actual load as a line), a "Heating adds … kWh" line, the heating included in each day's cost row, and a collapsed **Weather model** panel with daily load against temperature and the fitted line.

## Control (optional)

Every 30 seconds, and whenever the plan changes, the integration works out:

- **Grid setpoint**: in export slots, with SOC tracking on, the power that brings the battery to the plan's SOC at the end of the slot, after the live house load (so a big load shrinks the export instead of overshooting); otherwise the planned export power. Saving sessions export at the saving-session power; charge, hold and IOG slots use the *charging setpoint*; otherwise the *idle setpoint*. The override select replaces this when not `auto`.
- **EV charger**: if an allow switch is set (e.g. a GX relay wired to a Zappi eSense input), it's turned off while the EV inhibit sensor is on and back on otherwise.
- **Charge current limit**: the spread rate (energy still needed ÷ time to finish + make-up, converted with the battery voltage) inside the charge window. Outside it, during an Intelligent Octopus dispatch or a free-electricity session, the same spread so the battery fills the finish-early minutes before the slot ends instead of at full power. Otherwise the maximum. With a consumed-Ah sensor, the energy still needed comes from the BMS capacity (consumed Ah ÷ (100 − SOC)), as Victron DVCC spreader flows do.

These always appear as sensors, so you can compare them with what your current setup does before handing over. The two switches write them to the number entities you chose (e.g. Victron's ESS AC power setpoint and DVCC max charge current), so you can hand over the charge current alone and keep another tool on the setpoint, or the other way round.

Safeguards:

- Live SOC at or below the export floor → idle setpoint, whatever the plan says.
- Export capped at your maximum (or saving-session) export power.
- Plan stale or unavailable → **fallback** idle setpoint (the charge current still spreads, as it only needs SOC). SOC unavailable → idle setpoint and maximum charge current.
- Turning a switch off, unloading the integration or stopping Home Assistant → what that switch controlled is handed back once: idle setpoint and EV charger allowed, or maximum charge current.
- Changes within the deadband aren't written.
- Optional MQTT heartbeat: `on` every 30 s while controlling, `off` when stopped. [`docs/node-red-watchdog.json`](docs/node-red-watchdog.json) is a Node-RED flow for a Victron GX that resets the grid setpoint to −50 W if the heartbeat stops for 2 minutes (for example, if Home Assistant crashes).

Charging itself is left to your inverter's own schedule (e.g. Victron ESS scheduled charge); the integration only sets the charge current. Turn off any other automation that writes the same setpoint before enabling control, or they'll fight.

### Forecast format

Each `forecast` item is one half-hour slot (`t` is the slot's end):

| Key | Meaning |
|:--|:--|
| `t` | ISO time (UTC) |
| `soc` | SOC at `t` (%) |
| `kw` | Battery power (+ charging, − discharging) |
| `exp` | Grid export power (kW) |
| `ik`, `ek` | Grid import / export energy in the slot (kWh) |
| `mode` | `self`, `charge`, `hold`, `iog`, `export`, `saving`, `grid` |
| `rate`, `ip` | Export and import price (p/kWh) |
| `ld` | Expected house load (kW), including any weather adjustment |
| `wh`, `wc`, `tf` | Heating and cooling part of the load (kW) and forecast temperature (°C), with the weather model on |
| `fl` | Export floor in force (%) |

Other planners can publish the same attributes to use the card.

## Limitations

- Rule-based, not an optimiser: it exports surplus in the best-priced slots, but doesn't trade off charging more to export more.
- No solar forecast yet.
- The weather model's sensitivity is shared across the day; it's learned from at most the recorder's history (default 10 days for raw weather-entity temperatures, longer with an outdoor sensor's statistics).
- One charge window per day.
- Dispatches are assumed to charge the battery at full power. Turn that off if your setup doesn't.

## Development

```bash
pip install -r requirements_test.txt
TZ=Europe/London pytest
```

`tests/parity` checks the Python planner against the original Node-RED planner it was ported from (needs Node.js).

## Licence

MIT
