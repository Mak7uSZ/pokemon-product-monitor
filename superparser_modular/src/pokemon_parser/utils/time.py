from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def load_timezone(name: str) -> tzinfo:
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    try:
        import pytz
        return pytz.timezone(name)
    except Exception:
        return timezone.utc


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_local_str(tz: Optional[tzinfo]) -> str:
    try:
        if tz is not None:
            return datetime.now(tz).strftime("%H:%M:%S")
    except Exception:
        pass
    return datetime.now().strftime("%H:%M:%S")


def is_turbo_time(tz: tzinfo, start_hour: int, start_minute: int) -> bool:
    now = datetime.now(tz)
    if now.hour > start_hour:
        return True
    return now.hour == start_hour and now.minute >= start_minute
