# Battery Plan

A Home Assistant integration that plans a home battery's overnight charge and evening exports against Octopus Energy tariffs, forecasts state of charge, and estimates what each day will cost or earn. It ships with a Lovelace card.

![Battery Plan card](docs/card.png)

It **plans and reports only**. It doesn't control your inverter: use its sensors in your own automations (or Node-RED) to act on the plan.

## What it does

Every 5 minutes, and whenever rates or Intelligent Octopus dispatches change, it simulates the next 36 hours in half-hour slots:

- **Overnight charge window**: charges to your target, at full power or spread so it finishes shortly before the window ends (like a DVCC charge-current spreader). The house runs from grid during the window.
- **Intelligent Octopus Go dispatches**: planned dispatches outside the window are treated as full-rate top-ups (optional).
- **Octoplus**: saving sessions export at a higher power; free-electricity sessions charge at full power, and the battery is run down beforehand to make room.
- **Exports**: before each charge window it keeps enough for the house load plus an export floor, then exports the rest in the best-priced half hours paying at least your minimum price.
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
| Battery | SOC sensor, usable capacity, charge target, export floor, minimum SOC | Exports stop at the floor; the battery stops discharging at the minimum |
| Tariff | Import rate events, export rate events, standing charge sensor | Pick the Octopus Energy `…_current_day_rates` **and** `…_next_day_rates` events for each meter |
| Overnight charging | Window start and length, full power or spread, max/min/make-up power, finish-early minutes, an optional "long balance" night, charge efficiency | |
| Export | Max export power, saving-session power, minimum export price (p/kWh), discharge efficiency | |
| House load | Power sensors (summed, e.g. AC loads per phase), sensors to exclude (e.g. EV charger), days of history, flat fallback | Uses 5-minute statistics where the recorder still has them, hourly otherwise |
| Intelligent Octopus & Octoplus | Dispatching sensor, whether dispatches charge the battery, ready-time entity, saving-session and free-electricity calendars | All optional |
| Actual costs | Grid power sensor (import positive), invert switch | Optional; enables actual cost sensors |

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
