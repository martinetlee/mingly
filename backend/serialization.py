"""Shared response serialization helpers.

The DB stores naive datetimes that are UTC. Every API response must emit them
as explicit UTC (trailing ``Z``) so the frontend never parses them as local
time.
"""

from datetime import datetime, timezone
from typing import Optional


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"
