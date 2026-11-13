# Battery Plan: handover for Claude Code

Home Assistant custom integration (HACS). It plans a home battery's overnight charge and
evening exports against Octopus Energy tariffs, forecasts SOC and daily costs, ships a
Lovelace card, and can optionally drive a Victron ESS grid setpoint, DVCC charge current
and an all-day ESS charge schedule's target SOC. With a solar forecast it plans PV too and
lowers the overnight target to leave room for the sun. Current version: **0.8.0**.

The reference install is a Victron Quattro + Cerbo GX system with a large LiFePO4 bank,
DC chargers on the AC output that only charge inside ESS scheduled-charge windows,
Intelligent Octopus Go with Agile Outgoing export, heat pumps and an EV charger.
Install-specific details (entity IDs, settings, live state) are kept out of this public
file.

## Rules

- **No Claude author, co-author or session lines in commits or PRs**: this is an
  organisation rule. Commit with the maintainer's own git identity.
- Only commit and push when asked.
- Keep `const.VERSION` and `manifest.json` `version` in step; bump for every release
  (the card resource URL carries `?v=VERSION`, which is what makes browsers fetch a
  new card).
- Nothing may write to the inverter unless the user turns on a control switch.
- Ask before restarting a live Home Assistant. Restarts take about 2–3 minutes and the
  integration then waits for the SOC sensor (`setup_retry` for about a minute is normal).

## Develop and test

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements_test.txt
TZ=Europe/London pytest -q                 # 82 tests, ~55 s (Linux/macOS: the HA plugin needs fcntl)
pytest --noconftest tests/test_planner.py  # planner only, no Home Assistant needed (works on Windows)
```

On Windows, run the full suite in WSL with a Python 3.13 venv (e.g. `uv venv --python 3.13`
+ `uv pip install -r requirements_test.txt`).

Planner changes: check behaviour with a replay of recent days (rates, load, dispatches and
sessions from HA history, one plan from 05:30 per day). A refactor should give identical
plans; 0.6.0 was checked against 150 saved plans.

The card is plain JS, no build step: `node --check custom_components/battery_plan/www/battery-plan-card.js`.
To eyeball it, load the JS into a page (Playwright or headless Chrome `--screenshot`),
`setConfig`, then set `el.hass = {states: {...}, callWS: async () => ({}), locale:
{language: 'en-GB'}}` with a plan sensor's real attributes. Check at 400 px and 1000 px wide.

Test gotchas learned the hard way:
- Set the HA time zone to Europe/London in tests (the `octopus_states` fixture does;
  bare tests must call `hass.config.async_set_time_zone`).
- The integration's own `number` platform registers `number.set_value`, which
  replaces `async_mock_service(hass, "number", "set_value")`. Mock it **after**
  setup (`tests/test_control.py::setup` does).
- Don't mock `switch.turn_on/off` to observe the EV relay: that also swallows the
  control switch. Tests use an `input_boolean` for the EV entity.
- Recorder fixture: `recorder_mock` must come before `hass`; use
  `async_import_statistics` + `async_wait_recording_done` for statistics.
- Collapsed config-flow sections must **not** have `default={}`: the frontend uses
  a section's own default and then fills none of the fields inside (0.3.1 fix;
  `test_new_user_form_needs_only_battery_and_tariff` mirrors the frontend logic).

## Layout

| File | What it does |
|:--|:--|
| `planner.py` | Pure planner (no HA imports). Half-hour slots over 36 h: charge window (max or DVCC-style spread with finish-early/make-up/5 % hysteresis), IOG dispatch top-ups, free/Power Up/saving sessions, day/night export floor, weather load per slot, inverter output limit. Exports: `_ExportSearch` keeps a kWh only if the whole simulated plan gains at least `export_margin` p/kWh (best price first, 0.5 kW steps, bisected); optional buying at peak (afterwards, then holds/grid charge within `buy_ahead_h`), optional buy-sell-buy inside the window (refilled by the finish-early time). `offpeak_window()` reads the daily off-peak period from import rates. Output: `forecast` (keys `t soc kw exp ik ek mode rate ip ld fl` + `wh wc tf`), `plan` blocks, `days` costs, `free_sessions`. Originally ported from a Node-RED `planner.js`. |
| `coordinator.py` | Reads HA state, runs the planner every 5 min / on rate or dispatch change. Dispatches = planned + confirmed half hours. Charge window from the import rates' off-peak period (option) or inverter schedule entities, target from the schedule SOC entity (`schedule()`), BMS capacity from consumed Ah. Free-session import used so far. `CostTracker`: actual costs from the Energy dashboard's cost statistics (preferred, confirmed dispatch half hours re-priced) or grid power × live rates. |
| `load_inputs.py` | `LoadInputsMixin` (the coordinator inherits it): load profile (7-day half-hourly mean from 5-min/hourly statistics, minus excluded sensors; masks IOG dispatches, grid charging and Octoplus sessions outside the charge window, bridges slots masked every day) and the weather model's fit/forecast plumbing. Moved out of `coordinator.py` unchanged. |
| `solar.py` | Pure solar maths: energy-platform Wh/hour → kW per half hour (`slot_kw`), `calibrate` (bias = median actual/forecast, dull day = 20th percentile / bias, after ≥5 days), `nowcast` (rest of today from today's actual so far, weighted by the share of the day passed), `apply`. |
| `solar_inputs.py` | `SolarInputsMixin`: the forecast from the chosen integration's `energy` platform (`async_get_solar_forecast`, as the Energy dashboard uses; cached 30 min), a Store of each day's forecast as it stood by 06:00 with that day's actual from the PV sensor's statistics (energy change, or power means), calibration and nowcast. Publishes the `solar` attribute. |
| `helpers.py` | `_as_dt`, `_float`, `_secs`, `BAD` (shared parsing helpers). |
| `weather_load.py` | Pure weather-dependent load model: per-hour baseline + heating (and optional cooling) kW/°C past learned balance points (scaled per hour by the heating sensors' recent profile when there are some, `hour_weights`), optional heat pump COP model (`fit_cop`: weather curve for the supply temperature, data-sheet COP ratio; learns heat, forecasts heat ÷ COP), temperature smoothed over 6 h, quality gates (≥7 days, ≥48 active hours, ≥3 % variance explained, sensitivity ≤ 3 kW/°C). `slot_adjustments()` turns an hourly forecast into per-slot load adjustments. |
| `control.py` | `Controller`: every 30 s `compute()` builds a `_Ctx`, then `_charge_current` (spread in the window; spread during IOG top-ups to finish 15 min early; BMS capacity; planned rate in free sessions and charges bought ahead), `_charge_schedule_soc` (target for an all-day ESS schedule: raised for dispatches/free sessions/bought-ahead charges and optionally the window, else parked), `_setpoint` (SOC-tracking exports per half hour, held for the last minutes; overrides, export floor, saving sessions, hold = live load, idle/charge setpoints). Two independent switches (`setpoint_on`, `current_on`), configurable deadbands, revert on switch-off/unload/HA stop, EV inhibit relay, MQTT heartbeat. |
| `sensor.py` | Plan sensors, cost sensors, control target/status sensors, battery action sensor (history drives the card's past bands), weather load today/tomorrow, heating sensitivity. Large attributes are in `BIG` (`_unrecorded_attributes`). |
| `switch.py` / `select.py` / `number.py` / `binary_sensor.py` | Control switches (setpoint keeps unique id `<entry>_control`; both restore their state), override select, custom setpoint, EV inhibit and final-IOG-slot binary sensors. |
| `config_flow.py` | Two-step setup (name, then sectioned settings) and an options flow with the same form. `FIELDS` = selector + required flag. |
| `__init__.py` | Serves `www/battery-plan-card.js` and registers/updates it as a storage-mode Lovelace resource (falls back to `add_extra_js_url` for YAML mode). |
| `www/battery-plan-card.js` | The card: SOC history + forecast, plan/past-action bands, stepped export floor and min SOC line, load strip (base/heating/cooling bars, forecast temperature, actual load from 5-min statistics), cost table (today = actual so far + plan; unpriced import shown, never priced), planned actions table (`show_actions`, default on: blocks with energy, end SOC and value from the forecast), collapsed weather-model panel. Draws at its real width (ResizeObserver). |
| `docs/node-red-watchdog.json` | Node-RED flow for the GX, on its own tab: resets the setpoint to −50 W and DVCC to the maximum if the MQTT heartbeat stops for 2 min. Set the DVCC value for your system. |

## Known issues

- **DC chargers on the AC output can count as house load.** If the GX consumption sensors
  include their charging, the charge window's load is inflated (overstated planned import
  cost; buy-sell-buy squeezed by `inverter_max_kw`). Dispatch/session masking (0.3.3) covers
  the battery-hours slots. Fix it on the GX side so consumption excludes the chargers (done on
  the reference install, 9 Nov 2026; the 7-day profile clears over a week), or add a charger
  power sensor (DC volts × amps ÷ efficiency) to the load exclusions.
- The manifest has no `categories`; HACS only warns about it.

## 0.8.0: heat pump COP model

- Optional (Weather section, with dedicated heating sensors): `hp_supply_entity` (supply/flow
  temperature or its weather-compensation setpoint), `hp_output_entities` (heat output sensors) and
  `hp_cop_ratio` (default 0.47). Several units on one circuit (staged) are one system: summed.
- COP = ratio x ideal (Carnot) COP, condensing at supply + 2 K, evaporating at outdoor - 4 K (fixed
  coil gap; the ratio absorbs the rest). `fit_cop` learns the weather curve from history (supply = a +
  b x outdoor + a shrunk offset per hour of day, clamped to the range seen) and, with heat output
  sensors, the ratio their figures imply. The weather model is then fitted on heat (electricity x
  COP, or the output sensors) and `slot_adjustments(..., cop)` gives each slot's electricity change as
  heat_fc / COP_fc - typical heat / typical COP (both by hour, over `load_days`), so the profile stays
  the anchor. `wh`/`wc` are electricity. Plan attribute `weather_model.basis` = heat | electricity,
  `weather_model.heat_pump` = the curve, ratio and COP at -5/0/5/10 C. Without a supply sensor
  nothing changes.
- Replay, Chris 5-13 Nov (both IVTs, setpoint as supply, integration code, actual temperatures):
  hourly error 0.201 -> 0.178 kW; 12 Nov 23.0 -> 25.7 kWh (actual 33.3), 13 Nov 25.1 -> 28.0 (33.9).
  With the setpoint as supply the learned ratio is ~0.36 (the outlet runs above the setpoint); the
  outlet sensors record statistics from 13 Nov and can replace it later. Fixed ratio 0.47 without
  output sensors: 0.192.
- Chris's HA COP sensors are data-sheet based (0.464 IVT-10, 0.479 IVT-22), not measured.

## 0.7.2: past heating stacked too (card)

- The plan publishes `heating_entities`; the card fetches their 5-minute statistics with the load
  sensors' and stacks the past half hours as house + heating (bars before now, actual load line on
  top). Tooltip: "Load 2.1 kW actual (0.8 house + 1.3 heating)". Without heating sensors the past is
  the load line only, as before.
- The actual SOC history gets a faint neutral fill under it, like the forecast's blue fill.

## 0.7.1: heating by hour of day; heating shown apart from the house

- With dedicated heating sensors (`weather_load_entities`) the load profile code also builds their own
  half-hourly profile (same `load_days`, same masking; `_heat_profile`). The weather model's terms are
  scaled per hour by `w_h` = that hour's share of the profile's energy (mean 1, 10 % flat mixed in,
  `hour_weights`), in the fit, the typical load and the forecast. A heat pump that runs only 20:00-09:00
  (James's IVT-22) now gets its cold-weather extra overnight, not 46 % of it in hours it is off.
  Whole-house learning (no heating sensors) keeps `weights = None`: identical to 0.7.0.
- Replay (forecast at midnight, actual temperatures, 21-day fit), mean hourly error current → weighted:
  James 12 Nov 0.34 → 0.23 kW (the flat model was gated off at 1.8 % explained), 13 Nov 0.34 → 0.24;
  Chris (heating runs all day) 0.48 → 0.45, 0.63 → 0.57. Both installs still under-forecast a cold
  snap (Chris ~10 kWh/day on 12-13 Nov: the IVT-22 started space heating at ~0 °C, the response is
  steeper than linear); open, revisit with more cold days.
- Planner: `weather` tuples may carry a 5th value, the heating sensors' forecast in total
  (profile + weather adjustment); forecast key `ht` (≤ `ld`), days `heating_kwh`. Display only.
  `wh`/`weather_kwh` and the weather-load sensors keep meaning "due to the weather".
- Card: with `ht` the load bars split into house and heating (the heating sensors are part of the
  house load); the line reads "Heating X kWh rest of today · Y kWh tomorrow, Z from the cold".
  Without it the orange part is labelled "from the cold".
- James: switch `weather_load_entities` to the heating-only sensor once it has ≥ 7 days (~21 Nov);
  until then his compressor total includes the 04:00 hot-water boost.

## 0.7.0: solar

- Solar section: `pv_forecast_entry` (a config entry with an `energy` platform: Forecast.Solar, Solcast,
  ...), optional `pv_entity` (PV power or energy, for actuals), `pv_scale`, `pv_ac_coupled`, `pv_export`,
  `pv_optimise_target`, `pv_risk`.
- Planner: each slot has `pv_kw`. PV runs the house first; a surplus charges the battery (to 100 %,
  not the grid-charge target), then is exported (`spill`) unless the export rate is below zero or
  `pv_export` is off. A planned export sells the surplus first. DC PV: × `discharge_eff` to AC, into
  the battery at 0.98; AC-coupled: into the battery at `charge_eff`. `inverter_kw`: AC-coupled PV's
  export doesn't pass through the inverter, DC spill does. New mode `solar` (battery charging from PV).
  Without PV the plan is byte-identical to 0.6.11 (checked on random plans).
- Overnight target by cost (`_choose_target`): candidates from the configured target down to
  max(reserve, min SOC, 10) in 5 % steps; each a whole plan (own exports), scored
  (1 − `pv_risk`) × value(forecast) + `pv_risk` × value(forecast × dull), where value = exports − import
  + end kWh at the cheapest import / charge_eff. Ties go to the higher target; the previous target is
  kept unless another beats it by 2p; inside the window it's frozen (`window_target`). Only the next
  window gets it. ~17 candidates × 2 ≈ 1 s per replan (executor). Output: `plan_target_soc`,
  `target_scores`, `pv_dull`; forecast `pv` (kW) and `pk` (spill kWh); days `pv_kwh`.
- Forecast error is learned, not guessed: the forecast as it stood by 06:00 each day is stored with the
  PV sensor's actual; over 28 days, bias = median ratio (0.5-1.5), dull = 20th percentile / bias
  (0.2-1.0), after 5 days (`pv_scale` and 0.7 until then, or always without a PV sensor). Today's
  actual so far scales the rest of today. Attribute `solar`: status, bias, dull, today_ratio,
  forecast_kwh per day.
- Control: inside the charge window `planner_config()` uses the plan's target if lower (spread and
  charge schedule SOC follow it); dispatch top-ups still use the configured target. The `solar` slot
  gets the idle setpoint: the ESS charges from PV and feeds in the excess itself. Curtailing at a
  negative export rate is planned but not driven (set the GX's feed-in limit).
- Battery action `solar`: battery charging with no grid import, only when solar is configured.
- Card: PV forecast over the load strip, PV kWh per day, solar line (forecast, calibration,
  overnight target), solar-charge rows in planned actions.
- Load sensors must measure gross house consumption (not grid import), or PV is counted twice.

## 0.6.11: no sliver exports mid half hour; battery action settles

- Export search steps are 0.25 kWh in every slot (0.5 kW over a whole half hour, more kW in the
  half hour under way). With 0.5 kW steps, a replan part way through a half hour could sell a
  leftover too small for a step anywhere else (load under the forecast) in the current half hour
  at a poor price, and the controller's SOC tracking turned it into a 0.5-2.4 kW export to the
  slot end (12 Nov: 13:16-13:30 and most other half hours, at ~15p with 27p in the evening).
  Surplus too small for a step now stays in the battery.
- Battery action sensor: a new state must last 30 s (`ACTION_SETTLE`). One meter reading of
  ~0.6 kW export when a load switches off flipped it to export for 5 s (~20 times a day).

## 0.6.10: inverter minimum SOC

- `min_soc_entity` (optional, Battery section, e.g. the GX's ESS minimum SoC number): exports are
  still chosen against `min_soc`, then the final simulation (forecast, costs, plan blocks) uses
  the inverter's minimum if lower, as the battery really carries the house down to it (an evening
  that reached 15 % at 23:00 showed ~1 kWh of peak import the battery would cover). Above
  `min_soc` it replaces it for the whole plan. Plan publishes `hard_min_soc`.

## 0.6.9: planned actions energy (card only)

- The Battery column's kWh is the energy exported (or imported to charge the battery), not the
  plan block's `kwh`, which is the battery's own discharge (export + house load + losses): a
  saving hour at the 12.5 kW inverter limit showed 13.9 kWh, which read as more than possible.

## 0.6.8: planned actions table layout (card only)

- Rows: action → end SOC with the time underneath; columns Battery (what charging costs or
  exporting earns), House (the house's own grid electricity) and Grid (battery + house across
  the meter), each with its kWh underneath (grid net in/out). Hold reads "Hold (house on grid)".

## 0.6.7: peak buying only above the import price; planned actions; weather section

- Buy at peak (`export_peak_import`): an export may leave the house buying at peak (afterwards or
  in advance) only if its rate is at least the peak import price + margin (`beats_peak`). Judged
  on the whole plan, a step whose energy mostly came back cheaply overnight bought a little at
  peak for a 32.5p export against 34.26p import (10 Nov plan: 0.16 kWh at 23:00).
- Card: planned actions table (on by default). Settings: weather-dependent load in its own section.

## 0.6.6: buy-sell-buy at full power, dispatches and the Zappi

- With `night_cycle` on, a window that sells charges at full power up to its last export
  half hour, spread after it; a window with nothing worth selling spreads as before
  (`last_sell` in `_simulate`; the controller's `_window_sells_ahead` matches it). A spread
  charge left too little time to buy back: a replay of 9-10 Nov gave +£0.46 spread vs +£1.46.
- A window half hour with an export it can't make (battery at the floor) now charges instead
  of running the house from the battery.
- Buy-sell-buy never sells in a window half hour with an IOG dispatch (`_Slot.car`; the car
  would take the export), and the EV inhibit applies to window exports only while no dispatch is
  under way (`Controller._dispatch_now`), so the car's off-peak charging is never blocked.
- `reject_dispatch` (option, off): a dispatch outside the window (e.g. in the evening peak) may
  be exported in instead of topping up when that beats the margin; the slot shows `export`, the
  EV is inhibited (the car doesn't charge; Octopus usually reschedules) and the charge schedule
  isn't raised in it. Dispatches inside the window are always kept.
- Replays (plan from 22:00, actual rates/SOC/load): 9-10 Nov export 17.6-19.6p: +£1.46;
  10-11 Nov 15.7-16.3p: +£0.86. Still off on the reference install (needs the schedule 5 steps).

## 0.6.4: fewer writes, inverter limit, card

- Export SOC tracking stays per half hour, but for the last `export_hold_minutes` (option,
  default 5) the export power is held instead of tracked to the end. Tracking to the end
  divides a small SOC difference by a few minutes: in practice it tapered to ~2 kW before
  each boundary and jumped back to full (29 setpoint writes in 41 min). (A run-end tracking
  version with a rate band was pushed but never installed; the hold was preferred.)
- Charge current deadband is an option (`current_deadband_a`, default 10 A; was a fixed 1 A).
  Setpoint deadband was already an option (default 100 W). Schedule SOC deadband stays 1 %.
- `inverter_max_kw` (optional): the inverter's AC output feeds the house first, so each half
  hour's export is planned at no more than it less the house load. The controller still asks
  for the full export (no harm in asking; the ESS delivers what it can). Without it, a plan
  assuming the full export falls behind and replans push energy into later, cheaper half hours.
- Card: the export floor line is labelled as such, with a dotted min SOC line (plan publishes
  `min_soc`). The cost table's today row is the whole day: `actual_today` (the cost tracker's
  figures so far, in the plan data) + the plan for the rest (tooltip has the split).
- Import past the published rates is never priced (no made-up rates): days get
  `unpriced_kwh`, the card shows "—" / "+" and no net for such a day.
- The live export may exceed `max_export_kw`, up to the saving-session power, when SOC
  tracking asks for it; the GX's ESS settings enforce the real limits.

## 0.6.3: confirmed dispatch half hours

- Octopus bills a confirmed dispatch half hour (the Octopus integration's
  `started_dispatches`, half-hour aligned) off-peak to its end, even if the dispatch stops
  part way or started mid-slot (e.g. planned 21:13, slot 21:00 confirmed). `dispatches()` =
  planned + started, merged, so the plan and the top-up use the whole half hour.
- Actual costs (Energy dashboard source): the dashboard prices each moment at the live rate,
  so minutes before a dispatch was known go in at peak. `CostTracker._reprice` takes the
  excess off `import_cost` for confirmed half hours (attribute `dispatch_reprice`). The
  grid-power fallback source doesn't do this.

## 0.6.2: free-electricity cap, Power Ups

- Two calendars: `free_calendar` = Octoplus **free electricity sessions** (capped) and
  `power_up_calendar` = **Power Ups** (no cap).
- The Octopus integration lists the capped weekend free hours in **both** calendars (same
  uids). A Power Up with the same times as a free session is treated as the capped session.
  Likewise its power_down and saving_sessions calendars hold the same events.
- Weekend free hours (Octoplus "Happy Hours") are earned by reducing usage in Saving
  Sessions during the week; their T&Cs cap the free import at 16 kWh, past it import is
  billed at the normal rate.
- `free_cap_kwh` (default 16 kWh) applies **per session**: each calendar event (hour) gets
  its own, even back to back. House load and the car count. Past the cap at peak, the
  session ends (battery runs the house); where import is off-peak anyway (an IOG dispatch,
  or the lowest import rate) it carries on, paid at that rate.
- The import rate sensors show peak throughout a free session (the credit comes on the bill
  afterwards), so `cheap` from the rates still tells peak from off-peak, and the Energy
  dashboard / actual-cost sensors show the session at peak.
- The import used so far in a session in progress comes from the Energy dashboard's grid
  import statistics (else the grid power sensor's 5-min means), replanned every 5 min.
- All sessions of both calendars in the next 36 h are planned (`free_sessions` attribute:
  start, end, kind `free`/`power_up`, `cap_kwh`, `until` = when the plan stops charging for
  free, `free_kwh`, `used_kwh`).
- Control: free sessions aren't spread to the session end. They follow the plan's charge
  slots like a charge bought ahead (planned rate, charge schedule SOC = the plan's SOC at the
  end of the run), so the cap is respected — once the charge schedule SOC entity is set.

## 0.6.0

- One export rule: the export margin (default 5p). A 10-day replay (one plan from 05:30 per
  day, actual load known in advance, so an upper estimate) gave +£1.67/day over 0.5.0,
  +£1.71 with buy at peak, +£2.33 with buy-sell-buy overnight.
- Exporting just before a planned IOG dispatch happens when it clears the margin, but only
  against the dispatch in the half hour before the ready time (`_firm_dispatches`): that slot
  doesn't move, while other dispatch times do. The export search runs on slots built with
  only that dispatch; the plan still charges in all of them. Equal prices go to the latest
  half hour.
- Charge schedule control: the charge current switch writes an ESS schedule's SOC (all day,
  every day, enabled). The GX needs that schedule's "Self-consumption above limit" = PV &
  battery (AllowDischarge), or an active schedule caps discharge to PV power. The GX uses the
  first active schedule in slot order, a DC charger driver may use the highest target.
  Disable anything else that toggles that schedule. For buy-sell-buy, disable the other
  (backstop) schedules and extend the watchdog to raise the schedule in the window / park it
  outside if the heartbeat stops.
- DC chargers that follow ESS schedules only charge inside scheduled-charge windows, never on
  a positive grid setpoint: grid charging must go through a schedule.

## Backlog (decided or open)

1. Charge schedule SOC control live (check "Self-consumption above limit" first), then the
   watchdog for it.
2. **Heating shift** to off-peak: dropped for now (conflicts with weather-compensation
   efficiency gains; backtest £39–71/yr for 2–4 kWh/day).
3. **Efficiency calibration** from Victron AC/DC energy counters. A 4-week DC round trip
   was about 93 %; the planner assumes 0.93 × 0.90.
4. **Balancing rules** (separate task): force 100 % if the battery hasn't been full for N days, or
   if the expected solar surplus is too small to fill it (James's flow: 6 days, 0.5 kWh).
   Also: replay solar against an install with PV history (James's HA) vs a fixed target.
5. Optional weekday/weekend load split (backtest: worse than plain 7-day for now).
6. Weather model: a night/day sensitivity split didn't predict held-back days better than
   the shared one; recheck with more cold-weather data. Optional cooling.
7. Overnight export value: an annual backtest gave about £60/yr for buy-sell-buy; worthless
   when overnight export is 5–9p (the margin handles that).

## Useful facts

- The Octopus public API and most sites are blocked from the cloud sandbox's shell.
  WebFetch works but summarises. Agile CSVs can be downloaded from Octopus (UK local
  times, `dd/mm/yyyy HH:MM`, one month or a range per file).
- Victron schedule durations from `victron_mqtt` are in minutes; schedule start is a
  `time` entity; DVCC limit = `number.…_system_ess_max_charge_current`; grid setpoint =
  `number.…_system_ac_power_set_point`.
- `binary_sensor.…_intelligent_dispatching` is on only during dispatches (not the whole
  off-peak window); it's used for masking.
