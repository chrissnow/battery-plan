"""Data coordinator: gathers HA state, runs the planner, tracks actual costs."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import planner as P
from .const import (
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFF,
    CONF_CHARGE_HOURS,
    CONF_CHARGE_MODE,
    CONF_CHARGE_START,
    CONF_CONSUMED_AH_ENTITY,
    CONF_SCHEDULE_DURATION_ENTITY,
    CONF_SCHEDULE_SOC_ENTITY,
    CONF_SCHEDULE_START_ENTITY,
    CONF_BATTERY_VOLTAGE,
    CONF_DISCHARGE_EFF,
    CONF_DISPATCH_CHARGES,
    CONF_DISPATCHING_ENTITY,
    CONF_EXPORT_RATES,
    CONF_FINISH_EARLY_FULL_MIN,
    CONF_FINISH_EARLY_MIN,
    CONF_FREE_CALENDAR,
    CONF_GRID_POWER_ENTITY,
    CONF_GRID_POWER_INVERT,
    CONF_USE_ENERGY_COSTS,
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
    CONF_DAY_RESERVE_SOC,
    CONF_DAY_RESERVE_START,
    CONF_DAY_RESERVE_END,
    CONF_MIN_EXPORT_PRICE_PRE_FREE,
    CONF_RESERVE_SOC,
    CONF_SAVING_CALENDAR,
    CONF_SAVING_EXPORT_KW,
    CONF_SOC_ENTITY,
    CONF_STANDING_CHARGE,
    CONF_TARGET_SOC,
    CONF_TARGET_TIME_ENTITY,
    DEFAULTS,
    DOMAIN,
    LOAD_PROFILE_REFRESH,
    UPDATE_INTERVAL,
    WEEKDAYS,
)

_LOGGER = logging.getLogger(__name__)
BAD = (None, "", STATE_UNKNOWN, STATE_UNAVAILABLE)


def _as_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        d = dt_util.parse_datetime(v)
        if d is not None and d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    return None


def _float(v: Any, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _secs(v: Any, default: str) -> int:
    t = dt_util.parse_time(str(v or default)) or dt_util.parse_time(default)
    return t.hour * 3600 + t.minute * 60 + t.second


class BatteryPlanCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Runs the plan every few minutes and on tariff/dispatch changes."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.opts: dict[str, Any] = {**DEFAULTS, **entry.options}
        self._load: list[float] | None = None
        self._load_at: datetime | None = None
        self._load_source = "flat"
        self._load_masked = 0
        self.costs = CostTracker(hass, entry, self)
        self.action_entity_id: str | None = None
        from .control import Controller

        self.controller = Controller(hass, self)

    # ---------- configuration ----------
    def schedule(self) -> dict[str, Any]:
        """Charge window and target, from the inverter's schedule entities where set and valid.

        Returns {"start": seconds after midnight, "duration": seconds, "target": %,
        "source": {...}} falling back to the settings for anything unavailable.
        """
        o = self.opts
        start = _secs(o.get(CONF_CHARGE_START), "23:30:00")
        dur = int(float(o[CONF_CHARGE_HOURS]) * 3600)
        target = float(o[CONF_TARGET_SOC])
        src = {"start": "settings", "duration": "settings", "target": "settings"}
        if (eid := o.get(CONF_SCHEDULE_START_ENTITY)) and (st := self.hass.states.get(eid)):
            v = st.attributes.get("timestamp") if eid.startswith("input_datetime.") else None
            secs = None
            if v is not None:
                secs = int(float(v)) % 86400 if float(v) < 86400 else None
            if secs is None and ":" in str(st.state):
                t = dt_util.parse_time(str(st.state))
                secs = t.hour * 3600 + t.minute * 60 + t.second if t else None
            if secs is None and (n := _float(st.state)) is not None and 0 <= n < 86400:
                secs = int(n)  # Victron reports seconds after midnight
            if secs is not None:
                start, src["start"] = secs, eid
        if (eid := o.get(CONF_SCHEDULE_DURATION_ENTITY)) and (st := self.hass.states.get(eid)):
            n = _float(st.state)
            if n is not None and n > 0:
                unit = (st.attributes.get("unit_of_measurement") or "").lower()
                mult = {"s": 1, "sec": 1, "seconds": 1, "h": 3600, "hours": 3600}.get(unit, 60)
                if 0 < n * mult <= 86400:
                    dur, src["duration"] = int(n * mult), eid
        if (eid := o.get(CONF_SCHEDULE_SOC_ENTITY)) and (st := self.hass.states.get(eid)):
            n = _float(st.state)
            if n is not None and 10 <= n <= 100:
                target, src["target"] = n, eid
        return {"start": start, "duration": dur, "target": target, "source": src}

    def bms_capacity_ah(self, soc: float | None) -> float | None:
        """Usable capacity from the BMS: consumed Ah / (100 - SOC), like the Node-RED spreader.

        Only when the battery is far enough from full for the ratio to be meaningful,
        and within 50-150 % of the configured capacity.
        """
        eid = self.opts.get(CONF_CONSUMED_AH_ENTITY)
        st = self.hass.states.get(eid) if eid else None
        used = _float(st.state) if st else None
        if used is None or soc is None or soc > 95:
            return None
        ah = abs(used) * 100 / (100 - soc)
        cfg_ah = float(self.opts[CONF_CAPACITY_KWH]) * 1000 / float(self.opts[CONF_BATTERY_VOLTAGE])
        return ah if 0.5 * cfg_ah <= ah <= 1.5 * cfg_ah else None

    def planner_config(self) -> P.PlannerConfig:
        o = self.opts
        lb = o.get(CONF_LONG_BALANCE_DAY, "none")
        sch = self.schedule()
        return P.PlannerConfig(
            capacity_kwh=float(o[CONF_CAPACITY_KWH]),
            target_soc=sch["target"],
            reserve_soc=float(o[CONF_RESERVE_SOC]),
            min_soc=float(o[CONF_MIN_SOC]),
            day_reserve_soc=_float(o.get(CONF_DAY_RESERVE_SOC)),
            day_reserve_start=_secs(o.get(CONF_DAY_RESERVE_START), "05:25:00"),
            day_reserve_end=_secs(o.get(CONF_DAY_RESERVE_END), "22:25:00"),
            min_export_price_pre_free=_float(o.get(CONF_MIN_EXPORT_PRICE_PRE_FREE)),
            charge_start=sch["start"],
            charge_duration=sch["duration"],
            charge_mode=o[CONF_CHARGE_MODE],
            max_charge_kw=float(o[CONF_MAX_CHARGE_KW]),
            min_charge_kw=float(o[CONF_MIN_CHARGE_KW]),
            makeup_kw=float(o[CONF_MAKEUP_KW]),
            finish_early_min=float(o[CONF_FINISH_EARLY_MIN]),
            finish_early_full_min=float(o[CONF_FINISH_EARLY_FULL_MIN]),
            long_balance_weekday=None if lb in (None, "none") else WEEKDAYS.index(lb) - 1,
            long_balance_min=float(o[CONF_LONG_BALANCE_MIN]),
            max_export_kw=float(o[CONF_MAX_EXPORT_KW]),
            saving_export_kw=_float(o.get(CONF_SAVING_EXPORT_KW)),
            min_export_price=float(o[CONF_MIN_EXPORT_PRICE]),
            discharge_eff=float(o[CONF_DISCHARGE_EFF]),
            charge_eff=float(o[CONF_CHARGE_EFF]),
            dispatch_charges_battery=bool(o[CONF_DISPATCH_CHARGES]),
        )

    def watched_entities(self) -> list[str]:
        """Entities whose change should trigger a re-plan."""
        o = self.opts
        ids: list[str] = [*o.get(CONF_IMPORT_RATES, []), *o.get(CONF_EXPORT_RATES, [])]
        for k in (CONF_DISPATCHING_ENTITY, CONF_TARGET_TIME_ENTITY, CONF_SCHEDULE_START_ENTITY,
                  CONF_SCHEDULE_DURATION_ENTITY, CONF_SCHEDULE_SOC_ENTITY):
            if o.get(k):
                ids.append(o[k])
        return ids

    # ---------- input readers ----------
    def rates(self, entity_ids: Iterable[str]) -> list[P.Rate]:
        out: dict[datetime, P.Rate] = {}
        for eid in entity_ids:
            st = self.hass.states.get(eid)
            if st is None:
                continue
            for r in st.attributes.get("rates") or []:
                s, e = _as_dt(r.get("start")), _as_dt(r.get("end"))
                v = _float(r.get("value_inc_vat"))
                if s and e and v is not None:
                    out[s] = P.Rate(s, e, v)
        return [out[k] for k in sorted(out)]

    def dispatches(self) -> list[tuple[datetime, datetime]]:
        eid = self.opts.get(CONF_DISPATCHING_ENTITY)
        st = self.hass.states.get(eid) if eid else None
        if st is None:
            return []
        out = []
        for d in st.attributes.get("planned_dispatches") or []:
            s, e = _as_dt(d.get("start")), _as_dt(d.get("end"))
            if s and e and e > s:
                out.append((s, e))
        return sorted(out)

    def ready_time(self, now: datetime) -> datetime | None:
        eid = self.opts.get(CONF_TARGET_TIME_ENTITY)
        st = self.hass.states.get(eid) if eid else None
        if st is None:
            return None
        raw = st.attributes.get("raw_value") or st.state
        if raw in BAD:
            return None
        t = dt_util.parse_time(str(raw))
        if t is None:
            return None
        local = dt_util.as_local(now)
        ready = local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if ready <= local:
            ready += timedelta(days=1)
        return ready

    async def _calendar_window(self, eid: str | None, now: datetime) -> tuple[datetime, datetime] | None:
        if not eid or self.hass.states.get(eid) is None:
            return None
        try:
            resp = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {"entity_id": eid, "start_date_time": now - timedelta(hours=12), "end_date_time": now + timedelta(hours=36)},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            _LOGGER.debug("calendar %s: %s", eid, err)
            return None
        evs = ((resp or {}).get(eid) or {}).get("events") or []
        wins = []
        for ev in evs:
            s, e = _as_dt(ev.get("start")), _as_dt(ev.get("end"))
            if s and e and e > now:
                wins.append((s, e))
        return min(wins) if wins else None

    async def _stats(self, ids: set[str], start: datetime, end: datetime, period: str) -> dict:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        return await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, end, ids, period, {"power": "kW"}, {"mean"}
        )

    @staticmethod
    def _buckets(rows: list[dict], hourly: bool, masked: set[int] | None = None) -> list[float | None]:
        """Average kW per local half-hour (48) from statistics rows.

        ``masked`` holds UTC timestamps of half-hour slots to leave out (IOG
        dispatches, Octoplus sessions); a half hour with nothing left falls
        back to all its rows.
        """
        masked = masked or set()
        sums, counts = [0.0] * 48, [0] * 48
        all_sums, all_counts = [0.0] * 48, [0] * 48
        for row in rows:
            m, st = row.get("mean"), row.get("start")
            if m is None or st is None:
                continue
            ts = st if isinstance(st, datetime) else datetime.fromtimestamp(float(st), timezone.utc)
            loc = dt_util.as_local(ts)
            slot0 = int(ts.timestamp()) // 1800 * 1800
            if hourly:
                pairs = ((loc.hour * 2, slot0), (loc.hour * 2 + 1, slot0 + 1800))
            else:
                pairs = ((loc.hour * 2 + (loc.minute >= 30), slot0),)
            v = max(0.0, float(m))
            for k, slot in pairs:
                all_sums[k] += v
                all_counts[k] += 1
                if slot in masked or (hourly and slot0 in masked):
                    continue
                sums[k] += v
                counts[k] += 1
        base: list[float | None] = [sums[i] / counts[i] if counts[i] else None for i in range(48)]
        out = list(base)
        known = [i for i in range(48) if base[i] is not None]
        for i in range(48):
            if base[i] is not None or not all_counts[i]:
                continue
            if not known:  # everything masked: use it all
                out[i] = all_sums[i] / all_counts[i]
                continue
            # masked on every day (e.g. a daily dispatch): bridge from the nearest clean half hours
            kp = next(k for k in range(1, 49) if base[(i - k) % 48] is not None)
            kn = next(k for k in range(1, 49) if base[(i + k) % 48] is not None)
            out[i] = (base[(i - kp) % 48] * kn + base[(i + kn) % 48] * kp) / (kp + kn)
        return out

    def _in_charge_window(self, ts: datetime) -> bool:
        loc = dt_util.as_local(ts)
        sch = self.schedule()
        sod = loc.hour * 3600 + loc.minute * 60 + loc.second
        return (sod - sch["start"]) % 86400 < sch["duration"]

    async def _masked_slots(self, start: datetime, end: datetime) -> set[int]:
        """Half-hour slots (UTC epoch) that don't show normal house load.

        - Intelligent Octopus dispatches outside the charge window (the battery
          or other chargers top up then, and some of that is metered as load)
        - Octoplus free-electricity and saving sessions
        - outside the charge window, times our Battery action sensor saw the
          battery charging from the grid
        """
        from homeassistant.components.recorder import get_instance, history

        sources: dict[str, set[str]] = {}
        for key in (CONF_DISPATCHING_ENTITY,):
            if eid := self.opts.get(key):
                sources[eid] = {"on"}
        for key in (CONF_FREE_CALENDAR, CONF_SAVING_CALENDAR):
            if eid := self.opts.get(key):
                sources[eid] = {"on"}
        from homeassistant.helpers import entity_registry as er

        action = self.action_entity_id or er.async_get(self.hass).async_get_entity_id(
            "sensor", DOMAIN, f"{self.config_entry.entry_id}_battery_action"
        )
        if action:
            sources[action] = {"charge", "iog"}
        if not sources:
            return set()
        states = await get_instance(self.hass).async_add_executor_job(
            lambda: history.get_significant_states(
                self.hass, start, end, list(sources), significant_changes_only=False, no_attributes=True
            )
        )
        sessions = {self.opts.get(CONF_FREE_CALENDAR), self.opts.get(CONF_SAVING_CALENDAR)} - {None}
        masked: set[int] = set()
        for eid, rows in states.items():
            on = sources.get(eid, set())
            rows = [r for r in rows if hasattr(r, "state")]
            for i, r in enumerate(rows):
                if r.state not in on:
                    continue
                s = max(r.last_changed, start)
                e = rows[i + 1].last_changed if i + 1 < len(rows) else end
                t = int(s.timestamp()) // 1800 * 1800
                while t < e.timestamp():
                    slot = datetime.fromtimestamp(t, timezone.utc)
                    if eid in sessions or not self._in_charge_window(slot + timedelta(minutes=15)):
                        masked.add(t)
                    t += 1800
        return masked

    async def _load_profile(self, now: datetime) -> list[float]:
        """Half-hourly house load (kW): sum of load sensors minus excluded sensors.

        Uses 5-minute statistics where the recorder still has them, hourly
        long-term statistics otherwise, and the flat figure as a last resort.
        """
        flat = float(self.opts[CONF_LOAD_KW])
        ids = list(self.opts.get(CONF_LOAD_ENTITIES) or [])
        excl = [e for e in self.opts.get(CONF_LOAD_EXCLUDE) or [] if e not in ids]
        if not ids:
            self._load_source = "flat"
            return [flat] * 48
        if self._load is not None and self._load_at and now - self._load_at < LOAD_PROFILE_REFRESH:
            return self._load
        start = now - timedelta(days=int(self.opts[CONF_LOAD_DAYS]))
        try:
            short = await self._stats(set(ids + excl), start, now, "5minute")
            long = await self._stats(set(ids + excl), start, now, "hour")
        except Exception as err:  # noqa: BLE001 - recorder missing or query failed
            _LOGGER.warning("Load profile from statistics failed, using flat %.2f kW: %s", flat, err)
            self._load_source = "flat"
            return [flat] * 48

        try:
            masked = await self._masked_slots(start, now)
        except Exception as err:  # noqa: BLE001 - history unavailable: learn from everything
            _LOGGER.debug("Battery Plan could not read dispatch/session history: %s", err)
            masked = set()
        self._load_masked = len(masked)

        def per_entity(eid: str) -> list[float | None]:
            fine = self._buckets(short.get(eid, []), hourly=False, masked=masked)
            coarse = self._buckets(long.get(eid, []), hourly=True, masked=masked)
            return [f if f is not None else c for f, c in zip(fine, coarse)]

        loads = [per_entity(e) for e in ids]
        subs = [per_entity(e) for e in excl]
        prof: list[float] = []
        learned = 0
        for i in range(48):
            vals = [v[i] for v in loads if v[i] is not None]
            if not vals:
                prof.append(flat)
                continue
            learned += 1
            kw = sum(vals) - sum(v[i] or 0.0 for v in subs)
            prof.append(round(max(0.0, kw), 3))
        self._load, self._load_at = prof, now
        fine_any = any(short.get(e) for e in ids)
        self._load_source = ("statistics_5min" if fine_any else "statistics_hourly") if learned else "flat"
        return prof

    # ---------- update ----------
    async def _async_update_data(self) -> dict[str, Any]:
        self.opts = {**DEFAULTS, **self.config_entry.options}
        now = dt_util.utcnow()
        st = self.hass.states.get(self.opts[CONF_SOC_ENTITY])
        soc = _float(st.state) if st else None
        if soc is None:
            raise UpdateFailed(f"Battery SOC {self.opts[CONF_SOC_ENTITY]} is unavailable")
        cfg = self.planner_config()
        imp = self.rates(self.opts.get(CONF_IMPORT_RATES, []))
        exp = self.rates(self.opts.get(CONF_EXPORT_RATES, []))
        disp = self.dispatches()
        inp = P.PlannerInputs(
            now=now,
            tz=dt_util.get_default_time_zone(),
            soc=soc,
            load_kw=await self._load_profile(now),
            export_rates=exp,
            import_rates=imp,
            dispatches=disp,
            saving_session=await self._calendar_window(self.opts.get(CONF_SAVING_CALENDAR), now),
            free_session=await self._calendar_window(self.opts.get(CONF_FREE_CALENDAR), now),
        )
        result = await self.hass.async_add_executor_job(P.plan, cfg, inp)
        result["load_profile_kw"] = inp.load_kw
        result["load_source"] = self._load_source
        result["charge_window"] = self.schedule()
        result["load_masked_slots"] = self._load_masked
        result["standing_charge"] = self.standing_charge()
        result["soc_entity"] = self.opts[CONF_SOC_ENTITY]
        result["action_entity"] = self.action_entity_id

        ready = self.ready_time(now)
        if ready is not None:
            r_utc = ready.astimezone(timezone.utc)
            result["ready_time"] = ready.isoformat()
            result["dispatch_hours_before_ready"] = round(P.hours_before(disp, r_utc), 2)
            result["final_slot_scheduled"] = P.covers(disp, r_utc - timedelta(minutes=30), r_utc)
        self.costs.set_rates(imp, exp)
        return result

    def current_slot(self, now: datetime | None = None) -> dict[str, Any] | None:
        """Forecast slot covering now."""
        now = now or dt_util.utcnow()
        for f in (self.data or {}).get("forecast") or []:
            t = dt_util.parse_datetime(f["t"])
            if t and t > now:
                return f
        return None

    def standing_charge(self) -> float | None:
        eid = self.opts.get(CONF_STANDING_CHARGE)
        st = self.hass.states.get(eid) if eid else None
        return _float(st.state) if st else None


async def energy_grid_stats(hass: HomeAssistant) -> dict[str, list[str]] | None:
    """Grid meters and their cost statistics from the Energy dashboard.

    Returns {"import": [...], "import_cost": [...], "export": [...], "export_value": [...]}
    or None when the dashboard has no priced grid source. Handles both the
    flat grid format and the older flow_from / flow_to lists.
    """
    try:
        from homeassistant.components.energy.data import async_get_manager

        prefs = (await async_get_manager(hass)).data or {}
    except Exception:  # noqa: BLE001 - energy not set up
        return None
    auto = (hass.data.get("energy") or {}).get("cost_sensors", {})
    out: dict[str, list[str]] = {"import": [], "import_cost": [], "export": [], "export_value": []}
    for src in prefs.get("energy_sources", []):
        if src.get("type") != "grid":
            continue
        froms = src.get("flow_from") or ([src] if src.get("stat_energy_from") else [])
        tos = src.get("flow_to") or ([src] if src.get("stat_energy_to") else [])
        for f in froms:
            cost = f.get("stat_cost") or auto.get(f["stat_energy_from"])
            if cost:
                out["import"].append(f["stat_energy_from"])
                out["import_cost"].append(cost)
        for t in tos:
            comp = t.get("stat_compensation") or auto.get(t["stat_energy_to"])
            if comp:
                out["export"].append(t["stat_energy_to"])
                out["export_value"].append(comp)
    return out if out["import_cost"] else None


def energy_prefs_priced(prefs: dict) -> bool:
    """True if the Energy dashboard has a grid import with a price or cost statistic."""
    for src in prefs.get("energy_sources", []):
        if src.get("type") != "grid":
            continue
        for f in src.get("flow_from") or [src]:
            if f.get("stat_cost") or f.get("entity_energy_price") or f.get("number_energy_price") is not None:
                return True
    return False


class CostTracker:
    """Today's / yesterday's actual grid cost.

    From the Energy dashboard's cost statistics when it has priced grid meters
    (so the figures match it, with full history), otherwise by integrating the
    grid power sensor x live rates.
    """

    STORE_VERSION = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: BatteryPlanCoordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.store: Store[dict[str, Any]] = Store(hass, self.STORE_VERSION, f"{DOMAIN}.{entry.entry_id}.costs")
        self.today: dict[str, Any] = self._blank(dt_util.now().date().isoformat())
        self.yesterday: dict[str, Any] | None = None
        self._imp: list[P.Rate] = []
        self._exp: list[P.Rate] = []
        self._last_t: datetime | None = None
        self._last_kw: float | None = None
        self._listeners: list = []
        self._entity_cbs: list = []
        self._energy_ok = False

    @staticmethod
    def _blank(day: str) -> dict[str, Any]:
        return {"date": day, "import_kwh": 0.0, "import_cost": 0.0, "export_kwh": 0.0, "export_value": 0.0, "unpriced_kwh": 0.0}

    @property
    def enabled(self) -> bool:
        return self.source is not None

    @property
    def source(self) -> str | None:
        if self._energy_ok:
            return "energy_dashboard"
        if self.coordinator.opts.get(CONF_GRID_POWER_ENTITY):
            return "grid_power"
        return None

    async def async_detect(self) -> None:
        """Decide the source before the sensors are created."""
        self._energy_ok = False
        if not self.coordinator.opts.get(CONF_USE_ENERGY_COSTS, True):
            return
        try:
            from homeassistant.components.energy.data import async_get_manager

            self._energy_ok = energy_prefs_priced((await async_get_manager(self.hass)).data or {})
        except Exception:  # noqa: BLE001
            self._energy_ok = False

    async def _async_energy_refresh(self, *_: Any) -> None:
        stats = await energy_grid_stats(self.hass)
        if not stats:
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistic_during_period

        now = dt_util.now()
        today0 = dt_util.start_of_local_day(now)
        yday0 = dt_util.start_of_local_day(today0 - timedelta(hours=12))

        def change(ids: list[str], start: datetime, end: datetime, unit: dict | None) -> float:
            tot = 0.0
            for sid in ids:
                r = statistic_during_period(self.hass, start, end, sid, {"change"}, unit)
                tot += float(r.get("change") or 0.0)
            return tot

        def collect() -> tuple[dict, dict]:
            out = []
            for start, end in ((today0, now), (yday0, today0)):
                d = self._blank(start.date().isoformat())
                d["import_kwh"] = change(stats["import"], start, end, {"energy": "kWh"})
                d["import_cost"] = change(stats["import_cost"], start, end, None)
                d["export_kwh"] = change(stats["export"], start, end, {"energy": "kWh"})
                d["export_value"] = change(stats["export_value"], start, end, None)
                out.append(d)
            return out[0], out[1]

        self.today, self.yesterday = await get_instance(self.hass).async_add_executor_job(collect)
        self._notify()

    def set_rates(self, imp: list[P.Rate], exp: list[P.Rate]) -> None:
        if imp:
            self._imp = imp
        if exp:
            self._exp = exp

    def add_listener(self, cb) -> None:
        self._entity_cbs.append(cb)

    def _notify(self) -> None:
        for cb in self._entity_cbs:
            cb()

    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        if data.get("today"):
            self.today = data["today"]
        self.yesterday = data.get("yesterday")
        self._rollover(dt_util.now())
        await self.async_detect()
        if self._energy_ok:
            self._listeners.append(
                async_track_time_interval(self.hass, self._async_energy_refresh, timedelta(minutes=5))
            )
            self.hass.async_create_task(self._async_energy_refresh())
            return
        eid = self.coordinator.opts.get(CONF_GRID_POWER_ENTITY)
        if not eid:
            return
        self._listeners.append(async_track_state_change_event(self.hass, [eid], self._on_power))
        self._listeners.append(async_track_time_interval(self.hass, self._on_tick, timedelta(minutes=1)))
        st = self.hass.states.get(eid)
        self._last_kw = self._kw(st.state, st.attributes) if st else None
        self._last_t = dt_util.utcnow()

    async def async_stop(self) -> None:
        for unsub in self._listeners:
            unsub()
        self._listeners.clear()
        if self._energy_ok:
            return
        self._accumulate(dt_util.utcnow())
        await self.store.async_save(self._data())

    def _data(self) -> dict[str, Any]:
        return {"today": self.today, "yesterday": self.yesterday}

    def _kw(self, state: str, attrs: dict) -> float | None:
        v = _float(state)
        if v is None:
            return None
        unit = (attrs.get("unit_of_measurement") or "W").lower()
        kw = v / 1000 if unit == "w" else v * 1000 if unit == "mw" else v
        return -kw if self.coordinator.opts.get(CONF_GRID_POWER_INVERT) else kw

    @staticmethod
    def _price(rates: list[P.Rate], t: datetime) -> float | None:
        for r in rates:
            if r.start <= t < r.end:
                return r.price
        return None

    def _rollover(self, now_local: datetime) -> None:
        day = now_local.date().isoformat()
        if self.today["date"] != day:
            yday = (now_local.date() - timedelta(days=1)).isoformat()
            self.yesterday = self.today if self.today["date"] == yday else None
            self.today = self._blank(day)

    def _accumulate(self, now: datetime) -> None:
        if self._last_t is None or self._last_kw is None:
            self._last_t = now
            return
        t = self._last_t
        kw = self._last_kw
        while t < now:
            # split at half-hour boundaries (rate changes) and local midnight
            nxt = datetime.fromtimestamp((t.timestamp() // 1800 + 1) * 1800, timezone.utc)
            seg_end = min(nxt, now)
            local = dt_util.as_local(t)
            self._rollover(local)
            h = (seg_end - t).total_seconds() / 3600
            if kw > 0:
                e = kw * h
                p = self._price(self._imp, t)
                self.today["import_kwh"] += e
                if p is None:
                    self.today["unpriced_kwh"] += e
                else:
                    self.today["import_cost"] += e * p
            elif kw < 0:
                e = -kw * h
                p = self._price(self._exp, t)
                self.today["export_kwh"] += e
                if p is None:
                    self.today["unpriced_kwh"] += e
                else:
                    self.today["export_value"] += e * p
            t = seg_end
        self._last_t = now

    @callback
    def _on_power(self, event: Event[EventStateChangedData]) -> None:
        now = dt_util.utcnow()
        self._accumulate(now)
        st = event.data["new_state"]
        self._last_kw = self._kw(st.state, st.attributes) if st else None
        self.store.async_delay_save(self._data, 120)

    @callback
    def _on_tick(self, now: datetime) -> None:
        self._accumulate(dt_util.utcnow())
        self.store.async_delay_save(self._data, 120)
        self._notify()

    def net(self, d: dict[str, Any] | None) -> float | None:
        if not d:
            return None
        sc = self.coordinator.standing_charge() or 0.0
        return d["export_value"] - d["import_cost"] - sc

