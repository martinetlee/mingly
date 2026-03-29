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


class CatchUpSummaryIntervalRequest(BaseModel):
    interval_minutes: int


@router.post("/prefs/catchup-summary-interval")
async def set_catchup_summary_interval(req: CatchUpSummaryIntervalRequest):
    """Set the interval (in minutes) for the catch-up summary cronjob (5-1440)."""
    from fastapi import HTTPException
    if not (5 <= req.interval_minutes <= 1440):
        raise HTTPException(status_code=400, detail="Interval must be between 5 and 1440 minutes")
    prefs = _load_prefs()
    prefs["catchup_summary_interval_minutes"] = req.interval_minutes
    _save_prefs(prefs)
    return {"success": True, "interval_minutes": req.interval_minutes}


@router.post("/prefs/briefing-last-run")
async def set_briefing_last_run():
    """Mark that the briefing was just run (stores current UTC ISO timestamp)."""
    from datetime import datetime
    prefs = _load_prefs()
    prefs["briefing_last_run"] = datetime.now().isoformat()
    _save_prefs(prefs)
    return {"success": True, "briefing_last_run": prefs["briefing_last_run"]}


# ---------------------------------------------------------------------------
# User organizations
# ---------------------------------------------------------------------------

class UserOrganizationsRequest(BaseModel):
    organizations: list[str]


@router.get("/prefs/user-organizations")
async def get_user_organizations():
    """Return the list of organization names the user belongs to."""
    prefs = _load_prefs()
    return {"organizations": prefs.get("user_organizations", [])}


@router.put("/prefs/user-organizations")
async def set_user_organizations(req: UserOrganizationsRequest):
    """Set which organizations the user belongs to."""
    prefs = _load_prefs()
    prefs["user_organizations"] = req.organizations
    _save_prefs(prefs)
    return {"success": True, "organizations": req.organizations}


class TranslationLanguageRequest(BaseModel):
    language: str


@router.post("/prefs/translation-language")
async def set_translation_language(req: TranslationLanguageRequest):
    prefs = _load_prefs()
    prefs["translation_language"] = req.language
    _save_prefs(prefs)
    return {"success": True, "language": req.language}


# ---------------------------------------------------------------------------
# Scam detection settings
# ---------------------------------------------------------------------------

class ScamDetectionSettingsRequest(BaseModel):
    mode: str  # 'local_llm' | 'external'
    api_url: str = ""
    api_key: str = ""
    max_tokens: int = 200


def get_scam_detection_settings() -> dict:
    """Return scam detection settings from prefs."""
    prefs = _load_prefs()
    return {
        "mode": prefs.get("scam_detection_mode", "local_llm"),
        "api_url": prefs.get("scam_detection_api_url", ""),
        "api_key": prefs.get("scam_detection_api_key", ""),
        "max_tokens": prefs.get("scam_detection_max_tokens", 200),
    }


@router.get("/prefs/scam-detection")
async def get_scam_detection():
    """Get current scam detection configuration."""
    settings = get_scam_detection_settings()
    # Mask API key in response
    if settings["api_key"]:
        settings["api_key_set"] = True
        settings["api_key"] = ""
    else:
        settings["api_key_set"] = False
    return settings


@router.post("/prefs/scam-detection")
async def set_scam_detection(req: ScamDetectionSettingsRequest):
    """Update scam detection configuration and reconfigure the service."""
    from fastapi import HTTPException

    if req.mode not in ("local_llm", "external"):
        raise HTTPException(status_code=400, detail="Mode must be 'local_llm' or 'external'")

    if req.mode == "external" and not req.api_url.strip():
        raise HTTPException(status_code=400, detail="API URL is required for external mode")

    prefs = _load_prefs()
    prefs["scam_detection_mode"] = req.mode
    prefs["scam_detection_api_url"] = req.api_url.strip()
    prefs["scam_detection_max_tokens"] = max(50, min(2000, req.max_tokens))
    # Only update API key if provided (non-empty), otherwise keep existing
    if req.api_key:
        prefs["scam_detection_api_key"] = req.api_key
    elif req.mode == "local_llm":
        # Clear API key when switching to local mode
        prefs.pop("scam_detection_api_key", None)
    _save_prefs(prefs)

    # Reconfigure the live scam service
    _reconfigure_scam_service(prefs)

    return {"success": True, "mode": req.mode}


class ShieldCloudSettingsRequest(BaseModel):
    cloud_url: str = ""
    api_key: str = ""


@router.get("/prefs/shield-cloud")
async def get_shield_cloud():
    """Get Shield cloud sync configuration."""
    prefs = _load_prefs()
    api_key = prefs.get("shield_api_key", "")
    return {
        "cloud_url": prefs.get("shield_cloud_url", ""),
        "api_key_set": bool(api_key),
        "last_synced_at": prefs.get("shield_last_synced_at"),
    }


@router.post("/prefs/shield-cloud")
async def set_shield_cloud(req: ShieldCloudSettingsRequest):
    """Update Shield cloud sync configuration."""
    prefs = _load_prefs()
    prefs["shield_cloud_url"] = req.cloud_url.strip()
    if req.api_key:
        prefs["shield_api_key"] = req.api_key
    _save_prefs(prefs)

    # Reconfigure the sync manager
    from backend.scam.cloud_sync import get_cloud_sync_manager
    mgr = get_cloud_sync_manager()
    if mgr:
        mgr.configure(
            base_url=req.cloud_url.strip(),
            api_key=req.api_key or prefs.get("shield_api_key", ""),
        )

    return {"success": True}


def _reconfigure_scam_service(prefs: dict) -> None:
    """Swap the scam detection client based on current preferences."""
    from backend.scam.client import ExternalScamAPIClient, LocalPipelineClient
    from backend.scam.pipeline import get_pipeline_manager
    from backend.scam.service import get_scam_service

    svc = get_scam_service()
    if svc is None:
        return

    mode = prefs.get("scam_detection_mode", "local_llm")
    if mode == "external":
        api_url = prefs.get("scam_detection_api_url", "")
        api_key = prefs.get("scam_detection_api_key", "")
        svc._client = ExternalScamAPIClient(base_url=api_url, api_key=api_key)
    else:
        max_tokens = prefs.get("scam_detection_max_tokens", 200)
        mgr = get_pipeline_manager()
        if not mgr:
            from backend.scam.pipeline import init_pipeline_manager
            mgr = init_pipeline_manager()
        svc._client = LocalPipelineClient(mgr.get_config(), max_tokens=max_tokens)
