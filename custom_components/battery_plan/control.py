"""Turns the plan into live control targets and (optionally) writes them.

Every 30 s, and whenever the plan updates, the controller works out:
- a grid setpoint (W): in export slots, the grid export needed to reach the plan's
  SOC at the end of the slot given the live house load (or the planned export
  power if tracking is off); in saving-session slots the planned export; the
  charge setpoint in charge/hold/IOG slots; otherwise the idle setpoint. An
  override select (hold, max/limited export, saving, zero, custom) replaces the plan;
- a charge current limit (A): the spread rate inside the charge window and, outside
  it, inside an IOG dispatch (finishing the finish-early minutes before it ends), the
  planned rate in a free session (capped free import) or a charge bought ahead of an
  export, else max;
- a charge schedule target SOC (e.g. Victron ESS schedule 5, left on all day): raised
  to start a scheduled charge (so DC chargers that follow ESS schedules join in) in an
  IOG dispatch, and in a free session or a charge bought ahead of an export (to the
  plan's SOC at the end of that charge), else parked
  below the SOC. The overnight window is left to the inverter's own schedules, unless
  "charge overnight with the charge schedule" is on: then it is raised in the window
  too, except in half hours the plan exports in (buy, sell and buy back);
- EV inhibit: on while exporting at least 200 W outside the charge window.

These are always published as sensors (dry run). Two switches decide what is
written: grid setpoint control (setpoint + EV charger) and charge current control
(current + charge schedule SOC).
Safeguards:
- live SOC at or below the export floor (day or night value) -> no export, whatever
  the plan or override says;
- export capped at the configured maximum;
- plan stale / unavailable -> fallback idle setpoint (the charge current still
  spreads, as it only needs SOC); SOC unavailable -> idle setpoint and max current;
- turning a switch off or unloading -> idle setpoint / max current written once;
- writes skipped when the change is within the deadband;
- optional MQTT heartbeat ("on" while controlling, "off" when stopped) for an
  external watchdog.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
import logging
import math
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_VOLTAGE,
    CONF_CHARGE_SETPOINT_W,
    CONF_CHARGE_SOC_ENTITY,
    CONF_CHARGE_SOC_IDLE,
    CONF_CHARGE_SOC_WINDOW,
    CONF_CONSUMED_AH_ENTITY,
    CONF_CURRENT_ENTITY,
    CONF_EV_ALLOW_ENTITY,
    CONF_EV_ALLOW_INVERT,
    CONF_EXPORT_TRACKING,
    CONF_HEARTBEAT_TOPIC,
    CONF_IDLE_SETPOINT_W,
    CONF_LIMITED_EXPORT_KW,
    CONF_LOAD_ENTITIES,
    CONF_LOAD_EXCLUDE,
    CONF_LOAD_KW,
    CONF_SETPOINT_DEADBAND_W,
    CONF_CURRENT_DEADBAND_A,
    CONF_SETPOINT_ENTITY,
    CONF_SOC_ENTITY,
    CONF_STALE_MIN,
    CONTROL_INTERVAL,
    EV_INHIBIT_W,
)
from .planner import charge_finish_by, floor_at, spread_charge_kw

if TYPE_CHECKING:
    from .coordinator import BatteryPlanCoordinator

_LOGGER = logging.getLogger(__name__)

RUN_RATE_P = 1.0  # export half hours within this of a run's first rate (p/kWh) share its tracking target

STATUS_DISABLED = "disabled"
STATUS_ACTIVE = "active"
STATUS_FALLBACK = "fallback"
STATUSES = [STATUS_DISABLED, STATUS_ACTIVE, STATUS_FALLBACK]


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class _Ctx:
    """What one control pass works from."""

    now: datetime
    o: dict[str, Any]
    cfg: Any
    data: dict | None
    soc: float | None
    volts: float
    idle: float
    max_a: float
    park: float  # charge schedule SOC when no charge is wanted
    window_end: datetime | None  # in the charge window: when to finish
    topup: datetime | None  # in a dispatch outside it: when to finish

    @property
    def finish(self) -> datetime | None:
        return self.window_end or self.topup


class Controller:
    """Computes and (when enabled) applies the live setpoint and charge current."""

    def __init__(self, hass: HomeAssistant, coordinator: BatteryPlanCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.setpoint_on = False
        self.current_on = False
        self.capacity_ah: float | None = None
        self.setpoint_w: float | None = None
        self.current_a: float | None = None
        self.charge_soc: float | None = None
        self.mode: str | None = None
        self.reason = "starting"
        self.fallback = False
        self.last_write: datetime | None = None
        self.override = "auto"
        self.custom_w = 0.0
        self.ev_inhibit = False
        self.floor: float | None = None
        self._ev_written: bool | None = None
        self._spread: float | None = None
        self._topup_spread: float | None = None
        self.topup_until: datetime | None = None
        self._written: dict[str, float] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []

    # ---------- lifecycle ----------
    async def async_start(self) -> None:
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, CONTROL_INTERVAL))
        self._unsubs.append(self.coordinator.async_add_listener(self._on_plan))
        self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop))
        await self.async_run()

    @property
    def enabled(self) -> bool:
        return self.setpoint_on or self.current_on

    async def _on_stop(self, _event: Event) -> None:
        self._unsubs = [u for u in self._unsubs if u is not None]
        await self._revert("Home Assistant stopping", self.setpoint_on, self.current_on)

    async def async_stop(self) -> None:
        for u in self._unsubs:
            try:
                u()
            except ValueError:  # one-shot listener already fired
                pass
        self._unsubs.clear()
        await self._revert("integration unloaded", self.setpoint_on, self.current_on)

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @property
    def status(self) -> str:
        if not self.enabled:
            return STATUS_DISABLED
        return STATUS_FALLBACK if self.fallback else STATUS_ACTIVE

    async def async_set_enabled(self, on: bool, part: str = "setpoint") -> None:
        """Turn grid setpoint ("setpoint") or charge current ("current") control on/off."""
        if part == "current":
            if on == self.current_on:
                return
            self.current_on = on
        else:
            if on == self.setpoint_on:
                return
            self.setpoint_on = on
        if on:
            self._written.clear()
            await self.async_run()
        else:
            await self._revert(f"{'charge current' if part == 'current' else 'grid setpoint'} control switched off",
                               part != "current", part == "current")
        self._notify()

    async def async_set_override(self, mode: str, custom_w: float | None = None) -> None:
        self.override = mode
        if custom_w is not None:
            self.custom_w = custom_w
        await self.async_run()

    async def async_set_custom(self, watts: float) -> None:
        self.custom_w = watts
        if self.override == "custom":
            await self.async_run()
        else:
            self._notify()

    # ---------- computing ----------
    @callback
    def _on_plan(self) -> None:
        self.hass.async_create_task(self.async_run())

    async def _tick(self, _now: datetime) -> None:
        await self.async_run()

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _defaults(self) -> tuple[float, float]:
        o = self.coordinator.opts
        cfg = self.coordinator.planner_config()
        return float(o[CONF_IDLE_SETPOINT_W]), cfg.max_charge_kw * 1000 / float(o[CONF_BATTERY_VOLTAGE])

    def _context(self, now: datetime) -> _Ctx:
        o = self.coordinator.opts
        cfg = self.coordinator.planner_config()
        idle, max_a = self._defaults()
        st = self.hass.states.get(o[CONF_SOC_ENTITY])
        soc = _f(st.state) if st and st.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE) else None
        data = self.coordinator.data
        window_end = charge_finish_by(cfg, now, dt_util.get_default_time_zone())
        return _Ctx(
            now=now,
            o=o,
            cfg=cfg,
            data=data,
            soc=soc,
            volts=float(o[CONF_BATTERY_VOLTAGE]),
            idle=idle,
            max_a=max_a,
            park=float(o.get(CONF_CHARGE_SOC_IDLE) or 0),
            window_end=window_end,
            topup=None if window_end is not None else self._topup_finish(now, data, cfg),
        )

    def compute(self, now: datetime) -> None:
        """Set setpoint_w, current_a, charge_soc, mode, reason, fallback."""
        c = self._context(now)
        self.topup_until = c.topup
        self.fallback, self.mode, self.setpoint_w = True, None, c.idle
        # charge current and schedule: only need SOC, the charge window and dispatches, not a fresh plan
        self.current_a = self._charge_current(c)
        self.charge_soc = self._charge_schedule_soc(c, None)
        slot, why = self._plan_slot(c)
        if slot is None:
            self.reason = why
            return
        self.mode, self.fallback = slot["mode"], False
        self.floor = floor_at(c.cfg, now, dt_util.get_default_time_zone())
        self.charge_soc = self._charge_schedule_soc(c, slot)
        if self.mode == "charge" and c.finish is None:
            # free session or bought ahead of an export: at the planned rate
            kw = max(c.cfg.min_charge_kw, float(slot.get("kw") or 0))
            self.current_a = round(min(c.max_a, kw * 1000 / c.volts), 1)
        self.setpoint_w, self.reason = self._setpoint(c, slot)

    def _plan_slot(self, c: _Ctx) -> tuple[dict | None, str]:
        """The plan's current half hour, or None and why it can't be followed."""
        if not self.coordinator.last_update_success or not c.data:
            return None, "plan unavailable"
        gen = dt_util.parse_datetime(c.data.get("generated_at") or "")
        if gen is None or c.now - gen > timedelta(minutes=float(c.o[CONF_STALE_MIN])):
            return None, "plan is stale"
        if c.soc is None:
            return None, "SOC unavailable"
        slot = next((f for f in c.data.get("forecast") or [] if (dt_util.parse_datetime(f["t"]) or c.now) > c.now), None)
        return slot, "no plan for now"

    def _charge_current(self, c: _Ctx) -> float:
        """Charge current limit (A): the spread rate inside the window or a dispatch (finishing
        the finish-early minutes before it ends), else the maximum."""
        cfg = c.cfg
        if c.soc is None or c.finish is None or cfg.charge_mode != "spread":
            self._spread = self._topup_spread = None
            return round(c.max_a, 1)
        ah = self.coordinator.bms_capacity_ah(c.soc)
        if ah is not None or not c.o.get(CONF_CONSUMED_AH_ENTITY):
            self.capacity_ah = ah  # near full the ratio isn't usable: keep the last value
        spread_cfg = replace(cfg, capacity_kwh=self.capacity_ah * c.volts / 1000) if self.capacity_ah else cfg
        if c.window_end is not None:
            self._topup_spread = None
            kw = self._spread = spread_charge_kw(spread_cfg, c.soc, c.now, c.window_end, self._spread)
        else:
            self._spread = None
            kw = self._topup_spread = spread_charge_kw(spread_cfg, c.soc, c.now, c.topup, self._topup_spread)
        return round(kw * 1000 / c.volts, 1)

    def _charge_schedule_soc(self, c: _Ctx, slot: dict | None) -> float:
        """Target SOC for the charge schedule: raised to charge, else parked below the SOC."""
        cfg, park = c.cfg, c.park
        if slot is not None and slot["mode"] == "charge" and c.finish is None:
            # free session or bought ahead of an export: to the plan's SOC at the end of this charge run
            fc = c.data.get("forecast") or []
            i, peak = fc.index(slot), slot["soc"]
            while i < len(fc) and fc[i]["mode"] == "charge":
                peak = max(peak, fc[i]["soc"])
                i += 1
            return max(park, min(cfg.target_soc, float(math.ceil(peak))))
        if c.topup is not None:
            return max(park, cfg.target_soc)  # dispatch: charge as the schedule would
        if c.window_end is not None and c.o.get(CONF_CHARGE_SOC_WINDOW):
            if slot is not None and slot["mode"] in ("export", "saving"):
                return park  # buy, sell and buy back: stop the scheduled charge while exporting
            return max(park, cfg.target_soc)
        return park

    def _setpoint(self, c: _Ctx, slot: dict) -> tuple[float, str]:
        """Grid setpoint (W) and why: the override if any, else what the plan's slot asks for."""
        cfg, o, soc, mode = c.cfg, c.o, c.soc, slot["mode"]
        floor = self.floor
        cap = max(cfg.max_export_kw, cfg.saving_export_kw or 0)

        def export(kw: float, why: str) -> tuple[float, str]:
            if soc <= floor:
                return c.idle, f"{why}: at export floor ({soc:.1f}% <= {floor:.0f}%)"
            if kw < 0.1:
                return c.idle, f"{why}: nothing to export"
            return -round(min(kw, cap) * 1000), f"{why} {min(kw, cap):.2f} kW"

        ov = self.override
        if ov == "hold":
            return c.idle, "override: hold"
        if ov == "zero":
            return 0.0, "override: zero"
        if ov == "max_export":
            return export(cfg.max_export_kw, "override: max export")
        if ov == "limited_export":
            return export(float(o[CONF_LIMITED_EXPORT_KW]), "override: limited export")
        if ov == "saving":
            return export(cfg.saving_export_kw or cfg.max_export_kw, "override: saving export")
        if ov == "custom":
            if self.custom_w < 0:
                return export(-self.custom_w / 1000, "override: custom")
            return float(self.custom_w), "override: custom"
        if mode == "export" and o.get(CONF_EXPORT_TRACKING, True):
            last = self._export_run_end(c, slot)
            end = dt_util.parse_datetime(last["t"]) or c.now
            hrs = max((end - c.now).total_seconds() / 3600, 5 / 60)
            target = max(float(last["soc"]), floor)
            load = self._live_load_kw(slot)
            kw = (soc - target) / 100 * cfg.capacity_kwh / hrs * cfg.discharge_eff - load
            return export(kw, f"export to {target:.0f}% by {dt_util.as_local(end):%H:%M}, load {load:.1f} kW:")
        if mode in ("export", "saving"):
            return export(float(slot.get("exp") or 0), mode)
        if mode == "hold" and c.finish is None:
            # bought in advance for a later export: the house runs from grid, the battery rests
            load = self._live_load_kw(slot, whole=True)
            return round(load * 1000), f"hold for a later export: house on grid, load {load:.1f} kW"
        if mode in ("charge", "hold", "iog"):
            return float(o[CONF_CHARGE_SETPOINT_W]), f"{mode} slot"
        return c.idle, "house on battery" if mode == "self" else f"{mode} slot"

    @staticmethod
    def _export_run_end(c: _Ctx, slot: dict) -> dict:
        """The last half hour of the export run ``slot`` starts: following export half hours at
        about the same planned power (within 0.5 kW) and export rate (within ``RUN_RATE_P`` of
        the first). Tracking to its end, not each half hour's, avoids tapering off at every
        half-hour boundary and jumping back (and the writes that go with it). A half hour paid
        noticeably more or less, or planned at a different power (e.g. a run's smaller last half
        hour), keeps its own target, so a forecast error still lands where the plan put it."""
        fc = (c.data or {}).get("forecast") or []
        try:
            i = fc.index(slot)
        except ValueError:
            return slot
        kw = float(slot.get("exp") or 0)
        rate = slot.get("rate")

        def same(f: dict) -> bool:
            if f["mode"] != "export" or abs(float(f.get("exp") or 0) - kw) > 0.5:
                return False
            return rate is None or f.get("rate") is None or abs(float(f["rate"]) - float(rate)) <= RUN_RATE_P

        while i + 1 < len(fc) and same(fc[i + 1]):
            i += 1
        return fc[i]

    def _topup_finish(self, now: datetime, data: dict | None, cfg: Any) -> datetime | None:
        """Inside an IOG dispatch (if they charge the battery) outside the charge window: when to
        finish, i.e. the end of that block less the finish-early minutes.

        Free sessions aren't spread: they have a cap on free import, so the plan says how much
        to charge in them (its "charge" slots, followed like a charge bought ahead)."""
        if not data:
            return None
        spans: list[tuple[datetime, datetime]] = []
        raw = list(data.get("iog_dispatches") or []) if cfg.dispatch_charges_battery else []
        for s_, e_ in raw:
            s, e = dt_util.parse_datetime(s_), dt_util.parse_datetime(e_)
            if s and e and e > s:
                spans.append((s, e))
        spans.sort()
        merged: list[list[datetime]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1] + timedelta(minutes=1):
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        for s, e in merged:
            if s <= now < e:
                return e - timedelta(minutes=cfg.finish_early_min)
        return None

    def _live_load_kw(self, slot: dict, whole: bool = False) -> float:
        """House load now: load sensors minus excluded sensors (all of it if ``whole``), else the planned load."""
        o = self.coordinator.opts
        ids = o.get(CONF_LOAD_ENTITIES) or []
        planned = float(slot.get("ld") if slot.get("ld") is not None else o[CONF_LOAD_KW])
        if not ids:
            return planned
        total = 0.0
        for eid in ids:
            kw = self._kw(eid)
            if kw is None:
                return planned
            total += kw
        for eid in [] if whole else o.get(CONF_LOAD_EXCLUDE) or []:
            total -= self._kw(eid) or 0.0
        return max(0.0, total)

    def _kw(self, eid: str) -> float | None:
        st = self.hass.states.get(eid)
        if st is None:
            return None
        v = _f(st.state)
        if v is None:
            return None
        unit = (st.attributes.get("unit_of_measurement") or "W").lower()
        return v / 1000 if unit == "w" else v

    # ---------- applying ----------
    async def async_run(self) -> None:
        now = dt_util.utcnow()
        self.compute(now)
        in_window = charge_finish_by(self.coordinator.planner_config(), now, dt_util.get_default_time_zone()) is not None
        self.ev_inhibit = (
            not self.fallback and self.setpoint_w is not None and self.setpoint_w <= EV_INHIBIT_W and not in_window
        )
        o = self.coordinator.opts
        if self.setpoint_on:
            await self._write(o.get(CONF_SETPOINT_ENTITY), self.setpoint_w, float(o[CONF_SETPOINT_DEADBAND_W]))
            await self._write_ev(not self.ev_inhibit)
        if self.current_on:
            await self._write(o.get(CONF_CURRENT_ENTITY), self.current_a, float(o.get(CONF_CURRENT_DEADBAND_A, 10)))
            await self._write(o.get(CONF_CHARGE_SOC_ENTITY), self.charge_soc, 1.0)
        if self.enabled:
            await self._heartbeat("on")
        self._notify()

    async def _write_ev(self, allow: bool, force: bool = False) -> None:
        o = self.coordinator.opts
        eid = o.get(CONF_EV_ALLOW_ENTITY)
        if not eid:
            return
        on = allow != bool(o.get(CONF_EV_ALLOW_INVERT))
        st = self.hass.states.get(eid)
        if not force and self._ev_written == on and st is not None and st.state == ("on" if on else "off"):
            return
        domain = eid.split(".", 1)[0]
        try:
            await self.hass.services.async_call(domain, "turn_on" if on else "turn_off", {"entity_id": eid}, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Battery Plan could not switch %s: %s", eid, err)
            return
        self._ev_written = on

    async def _write(self, eid: str | None, value: float | None, deadband: float, force: bool = False) -> None:
        if not eid or value is None:
            return
        st = self.hass.states.get(eid)
        cur = _f(st.state) if st else None
        last = self._written.get(eid)
        if not force and cur is not None and abs(cur - value) < max(deadband, 1e-9) and (last is None or abs(last - value) < max(deadband, 1e-9)):
            return
        if cur is not None and st is not None:
            lo, hi = _f(st.attributes.get("min")), _f(st.attributes.get("max"))
            if lo is not None:
                value = max(lo, value)
            if hi is not None:
                value = min(hi, value)
        try:
            await self.hass.services.async_call("number", "set_value", {"entity_id": eid, "value": value}, blocking=True)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Battery Plan could not set %s to %s: %s", eid, value, err)
            return
        self._written[eid] = value
        self.last_write = dt_util.utcnow()
        _LOGGER.debug("Battery Plan set %s to %s (%s)", eid, value, self.reason)

    async def _revert(self, why: str, setpoint: bool, current: bool) -> None:
        """Hand back: idle setpoint + EV allowed and/or maximum charge current."""
        if not (setpoint or current):
            return
        o = self.coordinator.opts
        idle, max_a = self._defaults()
        if setpoint:
            self._written.pop(o.get(CONF_SETPOINT_ENTITY), None)
            await self._write(o.get(CONF_SETPOINT_ENTITY), idle, 0, force=True)
            await self._write_ev(True, force=True)
            self._ev_written = None
            _LOGGER.info("Battery Plan grid setpoint control stopped (%s): setpoint %s W", why, idle)
        if current:
            self._written.pop(o.get(CONF_CURRENT_ENTITY), None)
            await self._write(o.get(CONF_CURRENT_ENTITY), round(max_a, 1), 0, force=True)
            park = float(o.get(CONF_CHARGE_SOC_IDLE) or 0)
            cfg = self.coordinator.planner_config()
            if o.get(CONF_CHARGE_SOC_WINDOW) and charge_finish_by(cfg, dt_util.utcnow(), dt_util.get_default_time_zone()):
                park = max(park, cfg.target_soc)  # no backstop schedule: keep the overnight charge going
            if o.get(CONF_CHARGE_SOC_ENTITY):
                self._written.pop(o[CONF_CHARGE_SOC_ENTITY], None)
                await self._write(o[CONF_CHARGE_SOC_ENTITY], park, 0, force=True)
            _LOGGER.info("Battery Plan charge current control stopped (%s): %.0f A, charge schedule %s%%", why, max_a, park)
        if not self.enabled:
            await self._heartbeat("off")
        self.reason = why

    async def _heartbeat(self, payload: str) -> None:
        topic = self.coordinator.opts.get(CONF_HEARTBEAT_TOPIC)
        if not topic or "mqtt" not in self.hass.config.components:
            return
        try:
            await self.hass.services.async_call("mqtt", "publish", {"topic": topic, "payload": payload}, blocking=False)
        except HomeAssistantError as err:
            _LOGGER.debug("heartbeat: %s", err)

    def attributes(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "override": self.override,
            "export_floor": self.floor,
            "ev_inhibit": self.ev_inhibit,
            "control_enabled": self.enabled,
            "setpoint_control": self.setpoint_on,
            "charge_current_control": self.current_on,
            "charge_schedule_soc": self.charge_soc,
            "bms_capacity_ah": round(self.capacity_ah) if self.capacity_ah else None,
            "top_up_finish_by": self.topup_until.isoformat() if self.topup_until else None,
            "charge_window": self.coordinator.schedule(),
            "last_write": self.last_write.isoformat() if self.last_write else None,
        }
