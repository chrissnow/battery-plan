"""Solar inputs for the planner: the forecast from an integration's energy platform (what the
Energy dashboard shows: Forecast.Solar, Solcast, ...), corrected by its own track record.

A mixin of the coordinator. With a PV sensor set, each day's forecast (as it stood by 06:00 that
day) is kept with what the panels made, and ``solar.calibrate`` turns the last weeks of that into
a bias and a dull day; today's actual so far nudges the rest of today (``solar.nowcast``)."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import solar as S
from .const import CONF_PV_ENTITY, CONF_PV_FORECAST_ENTRY, CONF_PV_SCALE, DOMAIN, PV_FORECAST_REFRESH

_LOGGER = logging.getLogger(__name__.rsplit(".", 1)[0] + ".coordinator")

HISTORY_DAYS = 28
KEEP_DAYS = 35
DAY_AHEAD_UNTIL = timedelta(hours=6)  # a day's forecast is the one as it stood by 06:00 that day


class SolarInputsMixin:
    """Solar forecast and its calibration (uses the coordinator's hass, opts and config_entry)."""

    _pv_raw: dict[int, float] | None = None
    _pv_at: datetime | None = None
    _pv_store: Store | None = None
    _pv_hist: dict[str, dict[str, Any]] | None = None
    pv_info: dict[str, Any] | None = None

    async def _pv_forecast_raw(self) -> dict[int, float] | None:
        """The forecast as kW per half hour, or None if it can't be read."""
        from homeassistant.loader import async_get_integration

        entry_id = self.opts.get(CONF_PV_FORECAST_ENTRY)
        entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is None:
            return None
        try:
            integration = await async_get_integration(self.hass, entry.domain)
            platform = await integration.async_get_platform("energy")
            data = await platform.async_get_solar_forecast(self.hass, entry_id)
        except Exception as err:  # noqa: BLE001 - no energy platform, or the service failed
            _LOGGER.debug("Battery Plan: no solar forecast from %s: %s", entry.domain, err)
            return None
        hours: dict[datetime, float] = {}
        for k, wh in ((data or {}).get("wh_hours") or {}).items():
            t = dt_util.parse_datetime(str(k))
            if t is not None and isinstance(wh, (int, float)):
                hours[dt_util.as_utc(t)] = float(wh)
        return S.slot_kw(hours) if hours else None

    async def _pv_energy_kwh(self, start: datetime, end: datetime, fine: bool) -> float | None:
        """What the PV sensor says was made between two times (kWh): an energy sensor's change,
        or a power sensor's means (5-minute if ``fine``, else hourly)."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistic_during_period

        eid = self.opts.get(CONF_PV_ENTITY)
        st = self.hass.states.get(eid) if eid else None
        if st is None or end <= start:
            return None
        unit = str(st.attributes.get("unit_of_measurement") or "").lower()
        try:
            if unit in ("wh", "kwh", "mwh"):
                r = await get_instance(self.hass).async_add_executor_job(
                    statistic_during_period, self.hass, start, end, eid, {"change"}, {"energy": "kWh"}
                )
                v = r.get("change")
                return None if v is None else max(0.0, float(v))
            period, hrs = ("5minute", 5 / 60) if fine else ("hour", 1.0)
            rows = (await self._stats({eid}, start, end, period)).get(eid, [])
            if not rows:
                return None
            return sum(max(0.0, float(r["mean"])) * hrs for r in rows if r.get("mean") is not None)
        except Exception as err:  # noqa: BLE001 - recorder missing or query failed
            _LOGGER.debug("Battery Plan could not read PV statistics: %s", err)
            return None

    async def _pv_history(self, now: datetime, raw: dict[int, float]) -> dict[str, dict[str, Any]]:
        """Keep each day's forecast (up to 06:00 that day) and fill in past days' actuals."""
        if self._pv_store is None:
            self._pv_store = Store(self.hass, 1, f"{DOMAIN}.{self.config_entry.entry_id}.pv")
        if self._pv_hist is None:
            self._pv_hist = ((await self._pv_store.async_load()) or {}).get("days", {})
        hist = self._pv_hist
        tz = dt_util.get_default_time_zone()
        local = now.astimezone(tz)
        today = local.date()
        changed = False
        for d, kwh in S.daily_kwh(raw, tz).items():
            day0 = datetime.fromisoformat(d).replace(tzinfo=tz)
            kwh = round(kwh, 2)
            if now < day0 + DAY_AHEAD_UNTIL and kwh > 0 and hist.get(d, {}).get("fc") != kwh:
                hist.setdefault(d, {})["fc"] = kwh
                changed = True
        for d in sorted(hist):
            day = datetime.fromisoformat(d).date()
            if (today - day).days > KEEP_DAYS:
                del hist[d]
                changed = True
            elif "fc" in hist[d] and hist[d].get("actual") is None and not hist[d].get("tried"):
                day0 = datetime.combine(day, datetime.min.time(), tz)
                if now < day0 + timedelta(days=1, hours=1):  # the last hour's statistics come at ~:12
                    continue
                actual = await self._pv_energy_kwh(day0, day0 + timedelta(days=1), fine=False)
                if actual is None:
                    hist[d]["tried"] = True  # no statistics for that day: don't ask every 5 minutes
                else:
                    hist[d]["actual"] = round(actual, 2)
                changed = True
        if changed:
            await self._pv_store.async_save({"days": hist})
        return hist

    async def _pv_inputs(self, now: datetime) -> tuple[dict[int, float], float]:
        """The solar forecast for the planner (kW per slot start) and the dull-day fraction."""
        if not self.opts.get(CONF_PV_FORECAST_ENTRY):
            self.pv_info = None
            return {}, S.DEFAULT_DULL
        if self._pv_at is None or now - self._pv_at >= PV_FORECAST_REFRESH or self._pv_raw is None:
            raw = await self._pv_forecast_raw()
            if raw is not None:
                self._pv_raw, self._pv_at = raw, now
        raw = self._pv_raw
        if not raw:
            self.pv_info = {"status": "no forecast", "entry": self.opts.get(CONF_PV_FORECAST_ENTRY)}
            return {}, S.DEFAULT_DULL
        tz = dt_util.get_default_time_zone()
        scale = float(self.opts.get(CONF_PV_SCALE) or 1.0)
        bias, dull, n, today_f = scale, S.DEFAULT_DULL, 0, 1.0
        info: dict[str, Any] = {"status": "forecast", "scale": scale}
        if self.opts.get(CONF_PV_ENTITY):
            hist = await self._pv_history(now, raw)
            recent = {
                d: (v["fc"], v.get("actual"))
                for d, v in hist.items()
                if "fc" in v and (now.astimezone(tz).date() - datetime.fromisoformat(d).date()).days <= HISTORY_DAYS
            }
            b, dull, n = S.calibrate(recent)
            if n >= S.MIN_DAYS:
                bias = b
                info["status"] = "calibrated"
            else:
                info["status"] = f"learning ({n}/{S.MIN_DAYS} days)"
            day0 = dt_util.start_of_local_day(now.astimezone(tz))
            fc_so_far = S.kwh_between(raw, day0, now) * bias
            actual = await self._pv_energy_kwh(day0, now, fine=True)
            fc_today = S.daily_kwh(raw, tz).get(day0.date().isoformat(), 0.0) * bias
            if actual is not None:
                today_f = S.nowcast(fc_so_far, actual, fc_today)
                info["today_actual_kwh"] = round(actual, 2)
            info["today_forecast_so_far_kwh"] = round(fc_so_far, 2)
            info["history_days"] = n
        pv = S.apply(raw, bias, today_f, now, tz)
        days = S.daily_kwh(pv, tz)
        self.pv_info = info | {
            "bias": round(bias, 3),
            "dull": round(dull, 3),
            "today_ratio": round(today_f, 3),
            "forecast_kwh": {d: round(v, 1) for d, v in sorted(days.items())},
        }
        return pv, dull
