"""Small parsing helpers shared by the coordinator and the load inputs."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.util import dt as dt_util

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
