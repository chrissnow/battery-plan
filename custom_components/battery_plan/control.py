"""Turns the plan into live control targets and (optionally) writes them.

Every 30 s, and whenever the plan updates, the controller works out:
- a grid setpoint (W): export power in export/saving slots, the charge setpoint
  in charge/hold/IOG slots, otherwise the idle setpoint;
- a charge current limit (A): the spread rate inside the charge window, else max.

These are always published as sensors (dry run). Only when the control switch is
on are they written to the configured number entities. Safeguards:
- live SOC at or below the export floor -> idle setpoint, whatever the plan says;
- export capped at the configured maximum;
- plan stale / unavailable or SOC unavailable -> fallback (idle setpoint, max current);
- turning control off or unloading -> idle setpoint and max current written once;
- writes skipped when the change is within the deadband;
- optional MQTT heartbeat ("on" while controlling, "off" when stopped) for an
  external watchdog.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_VOLTAGE,
    CONF_CHARGE_SETPOINT_W,
    CONF_CURRENT_ENTITY,
    CONF_HEARTBEAT_TOPIC,
    CONF_IDLE_SETPOINT_W,
    CONF_SETPOINT_DEADBAND_W,
    CONF_SETPOINT_ENTITY,
    CONF_SOC_ENTITY,
    CONF_STALE_MIN,
    CONTROL_INTERVAL,
)
from .planner import charge_finish_by, spread_charge_kw

if TYPE_CHECKING:
    from .coordinator import BatteryPlanCoordinator

_LOGGER = logging.getLogger(__name__)

STATUS_DISABLED = "disabled"
STATUS_ACTIVE = "active"
STATUS_FALLBACK = "fallback"
STATUSES = [STATUS_DISABLED, STATUS_ACTIVE, STATUS_FALLBACK]


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Controller:
    """Computes and (when enabled) applies the live setpoint and charge current."""

    def __init__(self, hass: HomeAssistant, coordinator: BatteryPlanCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.enabled = False
        self.setpoint_w: float | None = None
        self.current_a: float | None = None
        self.mode: str | None = None
        self.reason = "starting"
        self.fallback = False
        self.last_write: datetime | None = None
        self._spread: float | None = None
        self._written: dict[str, float] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []

    # ---------- lifecycle ----------
    async def async_start(self) -> None:
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, CONTROL_INTERVAL))
        self._unsubs.append(self.coordinator.async_add_listener(self._on_plan))
        self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._on_stop))
        await self.async_run()

    async def _on_stop(self, _event: Event) -> None:
        self._unsubs = [u for u in self._unsubs if u is not None]
        if self.enabled:
            await self._revert("Home Assistant stopping")

    async def async_stop(self) -> None:
        for u in self._unsubs:
            try:
                u()
            except ValueError:  # one-shot listener already fired
                pass
        self._unsubs.clear()
        if self.enabled:
            await self._revert("integration unloaded")

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    @property
    def status(self) -> str:
        if not self.enabled:
            return STATUS_DISABLED
        return STATUS_FALLBACK if self.fallback else STATUS_ACTIVE

    async def async_set_enabled(self, on: bool) -> None:
        if on == self.enabled:
            return
        self.enabled = on
        if on:
            self._written.clear()
            await self.async_run()
        else:
            await self._revert("control switched off")
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

    def compute(self, now: datetime) -> None:
        """Set setpoint_w, current_a, mode, reason, fallback."""
        o = self.coordinator.opts
        idle, max_a = self._defaults()
        volts = float(o[CONF_BATTERY_VOLTAGE])
        data = self.coordinator.data
        self.fallback = True
        self.setpoint_w, self.current_a, self.mode = idle, max_a, None
        if not self.coordinator.last_update_success or not data:
            self.reason = "plan unavailable"
            return
        gen = dt_util.parse_datetime(data.get("generated_at") or "")
        if gen is None or now - gen > timedelta(minutes=float(o[CONF_STALE_MIN])):
            self.reason = "plan is stale"
            return
        st = self.hass.states.get(o[CONF_SOC_ENTITY])
        soc = _f(st.state) if st and st.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE) else None
        if soc is None:
            self.reason = "SOC unavailable"
            return
        slot = next((f for f in data.get("forecast") or [] if (dt_util.parse_datetime(f["t"]) or now) > now), None)
        if slot is None:
            self.reason = "no plan for now"
            return

        cfg = self.coordinator.planner_config()
        finish = charge_finish_by(cfg, now, dt_util.get_default_time_zone())
        if finish is not None and cfg.charge_mode == "spread":
            self._spread = spread_charge_kw(cfg, soc, now, finish, self._spread)
            self.current_a = round(self._spread * 1000 / volts, 1)
        else:
            self._spread = None
            self.current_a = round(max_a, 1)

        self.mode = slot["mode"]
        self.fallback = False
        if self.mode in ("export", "saving"):
            cap = max(cfg.max_export_kw, cfg.saving_export_kw or 0)
            if soc <= cfg.reserve_soc:
                self.setpoint_w = idle
                self.reason = f"at export floor ({soc:.1f}% <= {cfg.reserve_soc:.0f}%)"
            else:
                self.setpoint_w = -round(min(float(slot.get("exp") or 0), cap) * 1000)
                self.reason = f"{self.mode} {abs(self.setpoint_w) / 1000:.2f} kW"
        elif self.mode in ("charge", "hold", "iog"):
            self.setpoint_w = float(o[CONF_CHARGE_SETPOINT_W])
            self.reason = f"{self.mode} slot"
        else:
            self.setpoint_w = idle
            self.reason = "house on battery" if self.mode == "self" else f"{self.mode} slot"

    # ---------- applying ----------
    async def async_run(self) -> None:
        now = dt_util.utcnow()
        self.compute(now)
        if self.enabled:
            o = self.coordinator.opts
            await self._write(o.get(CONF_SETPOINT_ENTITY), self.setpoint_w, float(o[CONF_SETPOINT_DEADBAND_W]))
            await self._write(o.get(CONF_CURRENT_ENTITY), self.current_a, 1.0)
            await self._heartbeat("on")
        self._notify()

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

    async def _revert(self, why: str) -> None:
        o = self.coordinator.opts
        idle, max_a = self._defaults()
        self._written.clear()
        await self._write(o.get(CONF_SETPOINT_ENTITY), idle, 0, force=True)
        await self._write(o.get(CONF_CURRENT_ENTITY), round(max_a, 1), 0, force=True)
        await self._heartbeat("off")
        self.reason = why
        _LOGGER.info("Battery Plan control stopped (%s): setpoint %s W, charge current %.0f A", why, idle, max_a)

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
            "control_enabled": self.enabled,
            "last_write": self.last_write.isoformat() if self.last_write else None,
        }
