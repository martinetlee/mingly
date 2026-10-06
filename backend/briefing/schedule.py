"""When is the scheduled briefing due? Pure functions, unit-tested.

All times are naive LOCAL datetimes: ``briefing_time`` is a local wall-clock
time and ``briefing_last_run`` is stored with ``datetime.now()``.
"""

import logging
from datetime import datetime, time, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULT_TIME = time(9, 0)


def parse_time(value: Optional[str]) -> time:
    """'HH:MM' → time; anything invalid falls back to 09:00 (never raises)."""
    try:
        hh, mm = str(value).strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, TypeError, AttributeError):
        if value:
            logger.warning("Invalid briefing_time %r; using 09:00", value)
        return DEFAULT_TIME


def parse_day(value: Optional[str]) -> int:
    """Weekday name → 0 (Monday)..6; invalid falls back to Monday."""
    try:
        return WEEKDAYS.index(str(value).strip().lower())
    except ValueError:
        return 0


def parse_last_run(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)  # → naive local
    return dt


def latest_slot(now: datetime, schedule: str, briefing_time: Optional[str], briefing_day: Optional[str]) -> datetime:
    """The most recent scheduled run time that is <= now."""
    at = parse_time(briefing_time)
    slot = now.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    if schedule == "weekly":
        slot -= timedelta(days=(slot.weekday() - parse_day(briefing_day)) % 7)
        if slot > now:
            slot -= timedelta(days=7)
    elif slot > now:
        slot -= timedelta(days=1)
    return slot


def is_due(
    now: datetime,
    schedule: str,
    briefing_time: Optional[str],
    briefing_day: Optional[str],
    last_run: Optional[str],
) -> bool:
    """Due when no run has happened since the latest scheduled slot."""
    last = parse_last_run(last_run)
    if last is None:
        return True
    return last < latest_slot(now, schedule, briefing_time, briefing_day)


def window_days(schedule: str) -> int:
    return 7 if schedule == "weekly" else 1
