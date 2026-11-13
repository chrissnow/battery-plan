"""House load inputs for the planner: the half-hourly load profile from statistics (with
dispatch / session / grid-charge masking) and the weather-dependent load model's fit and
forecast. A mixin of the coordinator, moved out of coordinator.py unchanged."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging

from homeassistant.util import dt as dt_util

from . import weather_load as W
from .const import (
    CONF_DISPATCHING_ENTITY,
    CONF_FREE_CALENDAR,
    CONF_LOAD_DAYS,
    CONF_LOAD_ENTITIES,
    CONF_LOAD_EXCLUDE,
    CONF_LOAD_KW,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_POWER_UP_CALENDAR,
    CONF_SAVING_CALENDAR,
    CONF_WEATHER_COOLING,
    CONF_WEATHER_DAYS,
    CONF_WEATHER_ENTITY,
    CONF_WEATHER_LOAD_ENTITIES,
    DOMAIN,
    LOAD_PROFILE_REFRESH,
    WEATHER_FIT_REFRESH,
    WEATHER_FORECAST_REFRESH,
)
from .helpers import _float

_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")


class LoadInputsMixin:
    """Load profile and weather inputs (uses the coordinator's hass, opts and state)."""

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
        - Octoplus free-electricity, Power Up and saving sessions
        - outside the charge window, times our Battery action sensor saw the
          battery charging from the grid
        """
        from homeassistant.components.recorder import get_instance, history

        sources: dict[str, set[str]] = {}
        for key in (CONF_DISPATCHING_ENTITY,):
            if eid := self.opts.get(key):
                sources[eid] = {"on"}
        for key in (CONF_FREE_CALENDAR, CONF_POWER_UP_CALENDAR, CONF_SAVING_CALENDAR):
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
        sessions = {self.opts.get(k) for k in (CONF_FREE_CALENDAR, CONF_POWER_UP_CALENDAR, CONF_SAVING_CALENDAR)} - {None}
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
        heat = self._heating_ids()
        if not ids:
            self._load_source = "flat"
            self._heat_profile = None
            return [flat] * 48
        if self._load is not None and self._load_at and now - self._load_at < LOAD_PROFILE_REFRESH:
            return self._load
        start = now - timedelta(days=int(self.opts[CONF_LOAD_DAYS]))
        try:
            short = await self._stats(set(ids + excl + heat), start, now, "5minute")
            long = await self._stats(set(ids + excl + heat), start, now, "hour")
        except Exception as err:  # noqa: BLE001 - recorder missing or query failed
            _LOGGER.warning("Load profile from statistics failed, using flat %.2f kW: %s", flat, err)
            self._load_source = "flat"
            self._heat_profile = None
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
        # the dedicated heating (weather-load) sensors' own profile, same days and masking: part of
        # the house load above, shown as heating and used for the weather model's hour weights
        self._heat_profile = None
        if heat and learned:
            hv = [per_entity(e) for e in heat]
            hp = [None if any(v[i] is None for v in hv) else round(sum(v[i] for v in hv), 3) for i in range(48)]
            self._heat_profile = hp if all(v is not None for v in hp) else None
        return prof

    def _heating_ids(self) -> list[str]:
        """Dedicated weather-load (heating/cooling) sensors, when the weather model is on."""
        if not self.opts.get(CONF_WEATHER_ENTITY):
            return []
        return list(self.opts.get(CONF_WEATHER_LOAD_ENTITIES) or [])

    # ---------- weather-dependent load ----------
    def _weather_load_ids(self) -> tuple[list[str], list[str]]:
        """(sensors to learn from, sensors to subtract): weather-dependent loads, else the house load."""
        o = self.opts
        if o.get(CONF_WEATHER_LOAD_ENTITIES):
            return list(o[CONF_WEATHER_LOAD_ENTITIES]), []
        ids = list(o.get(CONF_LOAD_ENTITIES) or [])
        return ids, [e for e in o.get(CONF_LOAD_EXCLUDE) or [] if e not in ids]

    async def _weather_inputs(self, now: datetime, horizon: int) -> dict[int, tuple[float, float, float, float]]:
        if not self.opts.get(CONF_WEATHER_ENTITY):
            self.weather_model = None
            return {}
        if self._w_fit_at is None or now - self._w_fit_at >= WEATHER_FIT_REFRESH:
            try:
                await self._fit_weather(now)
            except Exception as err:  # noqa: BLE001 - never block the plan over the weather model
                _LOGGER.warning("Battery Plan could not learn the weather-dependent load: %s", err)
                self.weather_model = W.WeatherModel(status="not_enough_data", reason=f"error: {err}")
            self._w_fit_at = now
        if self._w_forecast_at is None or now - self._w_forecast_at >= WEATHER_FORECAST_REFRESH:
            self._w_forecast = await self._weather_forecast()
            self._w_forecast_at = now
            if self.weather_model and self.weather_model.active:
                await self._weather_today(now)
        tz = dt_util.get_default_time_zone()
        start0 = datetime.fromtimestamp(int(now.timestamp()) // 1800 * 1800, timezone.utc)
        slots = [start0 + timedelta(minutes=30 * i) for i in range(horizon)]
        active = bool(self.weather_model and self.weather_model.active)
        adj = W.slot_adjustments(self.weather_model, self._w_forecast, self._w_last_sm, slots, tz) if active else {}
        hp = self._heat_profile if self._heating_ids() else None
        if not hp:
            return adj
        # with dedicated heating sensors: each slot's heating in total (its recent profile moved
        # by the weather adjustment), so the card can show heating and the rest of the house apart
        out: dict[int, tuple] = {}
        for s in slots:
            k = int(s.timestamp())
            loc = s.astimezone(tz)
            a = adj.get(k)
            moved = hp[loc.hour * 2 + (loc.minute >= 30)] + (a[0] if a else 0.0)
            out[k] = (*(a if a else (0.0, 0.0, 0.0, None)), max(0.0, moved))
        return out

    async def _weather_forecast(self) -> list[tuple[datetime, float]]:
        eid = self.opts[CONF_WEATHER_ENTITY]
        try:
            resp = await self.hass.services.async_call(
                "weather", "get_forecasts", {"entity_id": eid, "type": "hourly"}, blocking=True, return_response=True
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Battery Plan: no hourly forecast from %s: %s", eid, err)
            return []
        st = self.hass.states.get(eid)
        f_unit = (st.attributes.get("temperature_unit") if st else None) == "°F"
        out = []
        for f in ((resp or {}).get(eid) or {}).get("forecast") or []:
            t = dt_util.parse_datetime(str(f.get("datetime")))
            v = _float(f.get("temperature"))
            if t is None or v is None:
                continue
            out.append((dt_util.as_utc(t), (v - 32) / 1.8 if f_unit else v))
        return sorted(out)

    async def _hourly_temps(self, start: datetime, end: datetime) -> dict[int, float]:
        """Hour start (epoch) -> outdoor temperature (degC)."""
        from homeassistant.components.recorder import get_instance, history

        o = self.opts
        if eid := o.get(CONF_OUTDOOR_TEMP_ENTITY):
            from homeassistant.components.recorder.statistics import statistics_during_period

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period, self.hass, start, end, {eid}, "hour", {"temperature": "°C"}, {"mean"}
            )
            self._w_source = eid
            out = {}
            for row in stats.get(eid, []):
                st, m = row.get("start"), row.get("mean")
                if st is None or m is None:
                    continue
                ts = st.timestamp() if isinstance(st, datetime) else float(st)
                out[int(ts) // 3600 * 3600] = float(m)
            return out
        # the weather entity's own recorded temperature
        wid = o[CONF_WEATHER_ENTITY]
        self._w_source = wid
        states = await get_instance(self.hass).async_add_executor_job(
            lambda: history.get_significant_states(self.hass, start, end, [wid], significant_changes_only=False)
        )
        sums: dict[int, list[float]] = {}
        for st in states.get(wid, []):
            if not hasattr(st, "attributes"):
                continue
            v = _float(st.attributes.get("temperature"))
            if v is None:
                continue
            if st.attributes.get("temperature_unit") == "°F":
                v = (v - 32) / 1.8
            sums.setdefault(int(st.last_updated.timestamp()) // 3600 * 3600, []).append(v)
        out = {h: sum(v) / len(v) for h, v in sums.items()}
        # carry the last reading forward through hours without a change
        if out:
            h, last = min(out), None
            while h <= int(end.timestamp()) // 3600 * 3600:
                if h in out:
                    last = out[h]
                elif last is not None:
                    out[h] = last
                h += 3600
        return out

    async def _fit_weather(self, now: datetime) -> None:
        o = self.opts
        start = now - timedelta(days=int(o[CONF_WEATHER_DAYS]))
        temps = await self._hourly_temps(start, now)
        ids, excl = self._weather_load_ids()
        if not ids:
            self.weather_model = W.WeatherModel(status="not_enough_data", reason="no load or weather-load sensors set")
            return
        stats = await self._stats(set(ids + excl), start, now, "hour")
        per: dict[str, dict[int, float]] = {}
        for eid in ids + excl:
            per[eid] = {}
            for row in stats.get(eid, []):
                st, m = row.get("start"), row.get("mean")
                if st is None or m is None:
                    continue
                ts = st.timestamp() if isinstance(st, datetime) else float(st)
                per[eid][int(ts)] = max(0.0, float(m))
        try:
            masked = await self._masked_slots(start, now)
        except Exception:  # noqa: BLE001
            masked = set()
        whole_house = not o.get(CONF_WEATHER_LOAD_ENTITIES)
        tz = dt_util.get_default_time_zone()
        hours = sorted(temps)
        smoothed = dict(W.smooth([(datetime.fromtimestamp(h, timezone.utc), temps[h]) for h in hours]))
        rows: list[W.Row] = []
        past: dict[str, float] = {}
        for h in hours:
            t = datetime.fromtimestamp(h, timezone.utc)
            sm = smoothed.get(t)
            if sm is None:
                continue
            loc = t.astimezone(tz)
            if all(h in per[e] for e in ids) and h not in masked and h + 1800 not in masked:
                # whole-house: the charge window isn't battery load and holds charger noise
                if not (whole_house and self._in_charge_window(t + timedelta(minutes=30))):
                    load = sum(per[e][h] for e in ids) - sum(per[e].get(h, 0.0) for e in excl)
                    rows.append(W.Row(t, loc.hour, loc.date().isoformat(), sm, temps[h], max(0.0, load)))
            past.setdefault(loc.date().isoformat(), 0.0)
        # dedicated heating sensors: scale each hour by its share of their recent energy
        weights = W.hour_weights(self._heat_profile) if not whole_house and self._heat_profile else None
        self.weather_model = W.fit(rows, bool(o.get(CONF_WEATHER_COOLING)), int(o[CONF_LOAD_DAYS]), now, weights)
        self._w_last_sm = smoothed[datetime.fromtimestamp(hours[-1], timezone.utc)] if hours else None

    async def _weather_today(self, now: datetime) -> None:
        """Weather load that has already happened today, and a fresh seed for smoothing the forecast."""
        tz = dt_util.get_default_time_zone()
        today0 = dt_util.start_of_local_day(now.astimezone(tz))
        temps = await self._hourly_temps(today0 - timedelta(hours=24), now)
        hours = sorted(temps)
        sm = W.smooth([(datetime.fromtimestamp(h, timezone.utc), temps[h]) for h in hours])
        m = self.weather_model
        done = sum(
            m.heat_kw(v, t.astimezone(tz).hour) + m.cool_kw(v, t.astimezone(tz).hour)
            for t, v in sm if v is not None and t >= today0 and t + timedelta(hours=1) <= now
        )
        self._w_past = {today0.date().isoformat(): done}
        if sm and sm[-1][1] is not None:
            self._w_last_sm = sm[-1][1]
