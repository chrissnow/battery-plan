# Battery Plan

A Home Assistant integration that plans a home battery's overnight charge and evening exports against Octopus Energy tariffs, forecasts state of charge, and estimates what each day will cost or earn. It ships with a Lovelace card.

![Battery Plan card](docs/card.png)

It plans and reports by default. It can **optionally** drive your inverter's grid setpoint and charge current, but that's off until you turn on its Control switch (see [Control](#control-optional)).

## What it does

Every 5 minutes, and whenever rates or Intelligent Octopus dispatches change, it simulates the next 36 hours in half-hour slots:

- **Overnight charge window**: charges to your target, at full power or spread so it finishes shortly before the window ends (like a DVCC charge-current spreader). The house runs from grid during the window.
- **Intelligent Octopus Go dispatches**: planned dispatches outside the window are treated as full-rate top-ups (optional).
- **Octoplus**: saving sessions export at a higher power; free-electricity sessions charge at full power, and the battery is run down beforehand to make room.
- **Exports**: before each charge window it keeps enough for the house load plus an export floor, then exports the rest in the best-priced half hours paying at least your minimum price. The floor can be higher during the day (e.g. 25% from 05:25 to 22:25, 20% overnight), and the minimum price can be lower before a free-electricity session so the battery empties to make room.
- **House load**: learned as a half-hourly profile from your power sensors' statistics (last N days), minus any excluded sensors such as an EV charger, or a flat figure.
- **Costs**: planned import cost and export earnings per day, from your import and export rates, and optionally the actual cost from a grid power sensor.

## Requirements

- Home Assistant 2025.4 or newer
- The [Octopus Energy](https://github.com/BottlecapDave/HomeAssistant-OctopusEnergy) integration (BottlecapDave): the planner reads its rate event entities, dispatching sensor and Octoplus calendars
- A battery state-of-charge sensor (any inverter)

## Installation

1. HACS → ⋮ → **Custom repositories** → add this repository's URL with type **Integration**.
2. Install **Battery Plan** and restart Home Assistant.
3. **Settings → Devices & services → Add integration → Battery Plan**.

The card is registered automatically. Add it with **Add card → Battery Plan**, or:

```yaml
type: custom:battery-plan-card
entity: sensor.battery_plan_soc_at_next_charge
```

## Settings

| Section | Setting | Notes |
|:--|:--|:--|
| Battery | SOC sensor, usable capacity, charge target, export floor, optional daytime floor with start/end times, minimum SOC | Exports stop at the floor in force at the time; the battery stops discharging at the minimum |
| Tariff | Import rate events, export rate events, standing charge sensor | Pick the Octopus Energy `…_current_day_rates` **and** `…_next_day_rates` events for each meter |
| Overnight charging | Window start and length, full power or spread, max/min/make-up power, finish-early minutes, an optional "long balance" night, charge efficiency | |
| Export | Max export power, saving-session power, limited-export power, minimum export price, minimum price before a free session (p/kWh), SOC tracking, discharge efficiency | |
| House load | Power sensors (summed, e.g. AC loads per phase), sensors to exclude (e.g. EV charger), days of history, flat fallback | Uses 5-minute statistics where the recorder still has them, hourly otherwise |
| Intelligent Octopus & Octoplus | Dispatching sensor, whether dispatches charge the battery, ready-time entity, saving-session and free-electricity calendars | All optional |
| Actual costs | Grid power sensor (import positive), battery power sensor (charging positive), invert switches | Optional; enables actual cost sensors and a more accurate Battery action sensor |
| Control (optional) | Grid setpoint entity, charge current entity, idle and charging setpoints, deadband, battery voltage, stale timeout, MQTT heartbeat topic, EV charger allow switch (+ invert) | Nothing is written until the Control switch is on |

Everything can be changed later under **Configure**.

## Entities

Entity IDs below assume the default name "Battery Plan".

| Entity | Description |
|:--|:--|
| `sensor.battery_plan_soc_at_next_charge` | Predicted SOC when the next charge window starts. Attributes hold the full plan: `forecast` (per half hour), `plan` (blocks), `days` (planned cost per day), `load_profile_kw` |
| `sensor.battery_plan_lowest_soc_ahead` | Lowest SOC in the forecast |
| `sensor.battery_plan_next_action` | `charge`, `export`, `iog`, `saving`, `hold`, `grid` or `self`, with `start`/`end` attributes |
| `sensor.battery_plan_planned_export` | Planned export over the horizon (kWh) |
| `sensor.battery_plan_net_today_estimate` | Today's actual so far (if a grid power sensor is set) plus the plan for the rest of today, minus standing charge |
| `sensor.battery_plan_net_tomorrow_plan` | Tomorrow's planned export earnings minus import cost and standing charge |
| `sensor.battery_plan_import_cost_today`, `…_export_earnings_today`, `…_net_today`, `…_net_yesterday` | Actual costs from the grid power sensor × live rates (needs a grid power sensor) |
| `sensor.battery_plan_dispatch_hours_before_ready` | Hours of planned IOG dispatch in the 24 h before the ready time, with overlaps merged (needs dispatching + ready-time entities) |
| `binary_sensor.battery_plan_final_slot_scheduled` | On when a dispatch covers the half hour before the ready time |
| `sensor.battery_plan_battery_action` | What the battery is actually doing now (`export`, `charge`, `iog`, `saving`, `hold`, `grid`, `self`), from grid/battery power and the plan. Its history gives the card's past bands |
| `sensor.battery_plan_target_grid_setpoint` | Grid setpoint the controller wants now (W): always computed, written only when Control is on |
| `sensor.battery_plan_target_charge_current` | Charge current limit the controller wants now (A) |
| `sensor.battery_plan_control_status` | `disabled` (dry run), `active` or `fallback`, with a `reason` attribute |
| `switch.battery_plan_control` | Turns control on. Off by default; created only when a setpoint or current entity is set |
| `select.battery_plan_override` | `auto` (follow the plan), `hold`, `max_export`, `limited_export`, `saving`, `zero` or `custom`. Export overrides still stop at the floor |
| `number.battery_plan_custom_setpoint` | Grid setpoint (W) used by the `custom` override |
| `binary_sensor.battery_plan_ev_inhibit` | On while exporting 200 W or more outside the charge window, so an EV charger doesn't drain the battery |

## Control (optional)

Every 30 seconds, and whenever the plan changes, the integration works out:

- **Grid setpoint**: in export slots, with SOC tracking on, the power that brings the battery to the plan's SOC at the end of the slot, after the live house load (so a big load shrinks the export instead of overshooting); otherwise the planned export power. Saving sessions export at the saving-session power; charge, hold and IOG slots use the *charging setpoint*; otherwise the *idle setpoint*. The override select replaces this when not `auto`.
- **EV charger**: if an allow switch is set (e.g. a GX relay wired to a Zappi eSense input), it's turned off while the EV inhibit sensor is on and back on otherwise.
- **Charge current limit**: the spread rate (energy still needed ÷ time to finish + make-up, converted with the battery voltage) inside the charge window, otherwise the maximum.

These always appear as sensors, so you can compare them with what your current setup does before handing over. Turning on `switch.battery_plan_control` writes them to the number entities you chose (e.g. Victron's ESS AC power setpoint and DVCC max charge current).

Safeguards:

- Live SOC at or below the export floor → idle setpoint, whatever the plan says.
- Export capped at your maximum (or saving-session) export power.
- Plan stale or unavailable, or SOC unavailable → **fallback**: idle setpoint and maximum charge current.
- Turning control off, unloading the integration or stopping Home Assistant → idle setpoint, maximum current and EV charger allowed, written once.
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
| `ld` | Expected house load (kW) |
| `fl` | Export floor in force (%) |

Other planners can publish the same attributes to use the card.

## Limitations

- Rule-based, not an optimiser: it exports surplus in the best-priced slots, but doesn't trade off charging more to export more.
- No solar forecast yet.
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
