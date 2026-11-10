# Battery Plan: handover for Claude Code

Home Assistant custom integration (HACS). It plans a home battery's overnight charge and
evening exports against Octopus Energy tariffs, forecasts SOC and daily costs, ships a
Lovelace card, and can optionally drive a Victron ESS grid setpoint, DVCC charge current
and an all-day ESS charge schedule's target SOC. Current version: **0.6.6**.

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
TZ=Europe/London pytest -q                 # 61 tests, ~18 s (Linux/macOS: the HA plugin needs fcntl)
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
| `helpers.py` | `_as_dt`, `_float`, `_secs`, `BAD` (shared parsing helpers). |
| `weather_load.py` | Pure weather-dependent load model: per-hour baseline + heating (and optional cooling) kW/°C past learned balance points, temperature smoothed over 6 h, quality gates (≥7 days, ≥48 active hours, ≥3 % variance explained, sensitivity ≤ 3 kW/°C). `slot_adjustments()` turns an hourly forecast into per-slot load adjustments. |
| `control.py` | `Controller`: every 30 s `compute()` builds a `_Ctx`, then `_charge_current` (spread in the window; spread during IOG top-ups to finish 15 min early; BMS capacity; planned rate in free sessions and charges bought ahead), `_charge_schedule_soc` (target for an all-day ESS schedule: raised for dispatches/free sessions/bought-ahead charges and optionally the window, else parked), `_setpoint` (SOC-tracking exports per half hour, held for the last minutes; overrides, export floor, saving sessions, hold = live load, idle/charge setpoints). Two independent switches (`setpoint_on`, `current_on`), configurable deadbands, revert on switch-off/unload/HA stop, EV inhibit relay, MQTT heartbeat. |
| `sensor.py` | Plan sensors, cost sensors, control target/status sensors, battery action sensor (history drives the card's past bands), weather load today/tomorrow, heating sensitivity. Large attributes are in `BIG` (`_unrecorded_attributes`). |
| `switch.py` / `select.py` / `number.py` / `binary_sensor.py` | Control switches (setpoint keeps unique id `<entry>_control`; both restore their state), override select, custom setpoint, EV inhibit and final-IOG-slot binary sensors. |
| `config_flow.py` | Two-step setup (name, then sectioned settings) and an options flow with the same form. `FIELDS` = selector + required flag. |
| `__init__.py` | Serves `www/battery-plan-card.js` and registers/updates it as a storage-mode Lovelace resource (falls back to `add_extra_js_url` for YAML mode). |
| `www/battery-plan-card.js` | The card: SOC history + forecast, plan/past-action bands, stepped export floor and min SOC line, load strip (base/heating/cooling bars, forecast temperature, actual load from 5-min statistics), cost table (today = actual so far + plan; unpriced import shown, never priced), collapsed weather-model panel. Draws at its real width (ResizeObserver). |
| `docs/node-red-watchdog.json` | Node-RED flow for the GX, on its own tab: resets the setpoint to −50 W and DVCC to the maximum if the MQTT heartbeat stops for 2 min. Set the DVCC value for your system. |

## Known issues

- **DC chargers on the AC output can count as house load.** If the GX consumption sensors
  include their charging, the charge window's load is inflated (overstated planned import
  cost; buy-sell-buy squeezed by `inverter_max_kw`). Dispatch/session masking (0.3.3) covers
  the battery-hours slots. Fix it on the GX side so consumption excludes the chargers (done on
  the reference install, 9 Nov 2026; the 7-day profile clears over a week), or add a charger
  power sensor (DC volts × amps ÷ efficiency) to the load exclusions.
- The manifest has no `categories`; HACS only warns about it.

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
4. **Solar**: needs a solar forecast in the planner (Solcast/Forecast.Solar).
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
