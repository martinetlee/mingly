import json
import os
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from backend.config import DATA_DIR
from backend.llm.briefing_prompts import (
    DEFAULT_BRIEFING_CHAT_PROMPT,
    DEFAULT_TRACKED_CHAT_PROMPT,
)

router = APIRouter()

_PREFS_FILE = DATA_DIR / "prefs.json"


def _load_prefs() -> dict:
    if _PREFS_FILE.exists():
        try:
            return json.loads(_PREFS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_prefs(data: dict) -> None:
    _PREFS_FILE.write_text(json.dumps(data))
    os.chmod(_PREFS_FILE, 0o600)


def get_notif_pref() -> str:
    """Returns 'all', 'tracked', or 'off'."""
    return _load_prefs().get("notif_pref", "tracked")


class NotifPrefRequest(BaseModel):
    pref: str  # 'all' | 'tracked' | 'off'


class BriefingScheduleRequest(BaseModel):
    schedule: str  # 'daily' | 'weekly'


class BriefingTimeRequest(BaseModel):
    time: str  # "HH:MM" in 24h format, e.g. "09:00"


@router.get("/prefs")
async def get_prefs():
    return _load_prefs()


@router.post("/prefs/notifications")
async def set_notif_pref(req: NotifPrefRequest):
    if req.pref not in ("all", "tracked", "off"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid preference")
    prefs = _load_prefs()
    prefs["notif_pref"] = req.pref
    _save_prefs(prefs)
    return {"success": True, "pref": req.pref}


@router.post("/prefs/briefing-schedule")
async def set_briefing_schedule(req: BriefingScheduleRequest):
    if req.schedule not in ("daily", "weekly"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid schedule")
    prefs = _load_prefs()
    prefs["briefing_schedule"] = req.schedule
    _save_prefs(prefs)
    return {"success": True, "schedule": req.schedule}


@router.post("/prefs/briefing-time")
async def set_briefing_time(req: BriefingTimeRequest):
    """Set the hour/minute at which the briefing auto-runs (local time, 24h HH:MM)."""
    import re
    if not re.match(r"^\d{2}:\d{2}$", req.time):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid time format. Use HH:MM.")
    prefs = _load_prefs()
    prefs["briefing_time"] = req.time
    _save_prefs(prefs)
    return {"success": True, "time": req.time}


class BriefingDayRequest(BaseModel):
    day: str  # 'monday' | 'tuesday' | ... | 'sunday'


@router.post("/prefs/briefing-day")
async def set_briefing_day(req: BriefingDayRequest):
    """Set the day of week on which the weekly briefing auto-runs."""
    valid = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if req.day not in valid:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid day")
    prefs = _load_prefs()
    prefs["briefing_day"] = req.day
    _save_prefs(prefs)
    return {"success": True, "day": req.day}


class BriefingMaxTokensRequest(BaseModel):
    max_tokens: int


class BriefingPromptRequest(BaseModel):
    prompt: str


class BriefingPromptsRequest(BaseModel):
    tracked_prompt: str
    briefing_prompt: str


def get_briefing_max_tokens() -> int:
    """Returns the configured max_tokens for briefing generation (default 4000)."""
    val = _load_prefs().get("briefing_max_tokens", 4000)
    try:
        return max(256, min(32000, int(val)))
    except (TypeError, ValueError):
        return 4000


def get_briefing_prompt_override() -> Optional[str]:
    """Returns the user-provided briefing prompt override, if any."""
    val = _load_prefs().get("briefing_prompt_override")
    if isinstance(val, str):
        val = val.strip()
        return val or None
    return None


def _normalize_prompt(v: object) -> Optional[str]:
    if isinstance(v, str):
        s = v.strip()
        return s or None
    return None


def get_briefing_prompt_overrides() -> dict:
    prefs = _load_prefs()
    return {
        "tracked": _normalize_prompt(prefs.get("briefing_prompt_tracked")),
        "briefing": _normalize_prompt(prefs.get("briefing_prompt_briefing")),
    }


@router.post("/prefs/briefing-max-tokens")
async def set_briefing_max_tokens(req: BriefingMaxTokensRequest):
    """Set max_tokens used when calling the LLM for each briefing summary."""
    from fastapi import HTTPException
    if not (256 <= req.max_tokens <= 32000):
        raise HTTPException(status_code=400, detail="max_tokens must be between 256 and 32000")
    prefs = _load_prefs()
    prefs["briefing_max_tokens"] = req.max_tokens
    _save_prefs(prefs)
    return {"success": True, "max_tokens": req.max_tokens}


@router.get("/prefs/briefing-prompt")
async def get_briefing_prompt():
    """Get briefing prompt override. Empty means use system default prompt."""
    prompt = get_briefing_prompt_override() or ""
    return {"prompt": prompt, "is_custom": bool(prompt)}


@router.post("/prefs/briefing-prompt")
async def set_briefing_prompt(req: BriefingPromptRequest):
    """
    Set briefing prompt override.
    Send empty string to clear override and revert to default.
    """
    prefs = _load_prefs()
    prompt = (req.prompt or "").strip()
    if prompt:
        prefs["briefing_prompt_override"] = prompt
    else:
        prefs.pop("briefing_prompt_override", None)
    _save_prefs(prefs)
    return {"success": True, "is_custom": bool(prompt)}


@router.get("/prefs/briefing-prompts")
async def get_briefing_prompts():
    """
    Return tracked/briefing prompts with default + custom + effective values.
    """
    overrides = get_briefing_prompt_overrides()
    tracked_custom = overrides["tracked"] or ""
    briefing_custom = overrides["briefing"] or ""
    return {
        "tracked": {
            "default": DEFAULT_TRACKED_CHAT_PROMPT,
            "custom": tracked_custom,
            "effective": tracked_custom or DEFAULT_TRACKED_CHAT_PROMPT,
            "is_custom": bool(tracked_custom),
        },
        "briefing": {
            "default": DEFAULT_BRIEFING_CHAT_PROMPT,
            "custom": briefing_custom,
            "effective": briefing_custom or DEFAULT_BRIEFING_CHAT_PROMPT,
            "is_custom": bool(briefing_custom),
        },
    }


@router.post("/prefs/briefing-prompts")
async def set_briefing_prompts(req: BriefingPromptsRequest):
    """
    Set tracked/briefing prompt overrides.
    Send empty string for either field to use default for that category.
    """
    prefs = _load_prefs()
    tracked = (req.tracked_prompt or "").strip()
    briefing = (req.briefing_prompt or "").strip()

    if tracked:
        prefs["briefing_prompt_tracked"] = tracked
    else:
        prefs.pop("briefing_prompt_tracked", None)

    if briefing:
        prefs["briefing_prompt_briefing"] = briefing
    else:
        prefs.pop("briefing_prompt_briefing", None)

    _save_prefs(prefs)
    return {
        "success": True,
        "tracked_is_custom": bool(tracked),
        "briefing_is_custom": bool(briefing),
    }


@router.post("/prefs/briefing-last-run")
async def set_briefing_last_run():
    """Mark that the briefing was just run (stores current UTC ISO timestamp)."""
    from datetime import datetime
    prefs = _load_prefs()
    prefs["briefing_last_run"] = datetime.utcnow().isoformat()
    _save_prefs(prefs)
    return {"success": True, "briefing_last_run": prefs["briefing_last_run"]}
