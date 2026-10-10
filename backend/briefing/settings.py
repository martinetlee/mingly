"""Briefing preferences: schedule time, token budget and prompt overrides."""

import re
from typing import Optional, Tuple

from backend import prefs

_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
DEFAULT_BRIEFING_TIME = "09:00"


def parse_briefing_time(value: object) -> Optional[Tuple[int, int]]:
    """Strictly parse "HH:MM" (00-23 / 00-59). Returns (hour, minute) or None."""
    if not isinstance(value, str):
        return None
    m = _HHMM.fullmatch(value)
    return (int(m.group(1)), int(m.group(2))) if m else None


def get_briefing_time() -> Tuple[int, int]:
    """Configured briefing time; an invalid stored value falls back to 09:00."""
    return parse_briefing_time(prefs.get_pref("briefing_time")) or parse_briefing_time(DEFAULT_BRIEFING_TIME)  # type: ignore[return-value]


def get_briefing_max_tokens() -> int:
    """Returns the configured max_tokens for briefing generation (default 4000)."""
    val = prefs.load_prefs().get("briefing_max_tokens", 4000)
    try:
        return max(256, min(32000, int(val)))
    except (TypeError, ValueError):
        return 4000


def get_briefing_prompt_override() -> Optional[str]:
    """Returns the user-provided briefing prompt override, if any."""
    val = prefs.load_prefs().get("briefing_prompt_override")
    if isinstance(val, str):
        val = val.strip()
        return val or None
    return None


def normalize_prompt(v: object) -> Optional[str]:
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def get_briefing_prompt_overrides() -> dict:
    data = prefs.load_prefs()
    return {
        "tracked": normalize_prompt(data.get("briefing_prompt_tracked")),
        "briefing": normalize_prompt(data.get("briefing_prompt_briefing")),
    }
