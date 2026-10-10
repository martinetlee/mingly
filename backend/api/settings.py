import logging
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend import secrets as vault
from backend.features import require_shield
from backend.db.database import get_db
from backend.llm.briefing_prompts import (
    DEFAULT_BRIEFING_CHAT_PROMPT,
    DEFAULT_TRACKED_CHAT_PROMPT,
)
from backend import prefs as _prefs
from backend.briefing.settings import (
    get_briefing_prompt_override,
    get_briefing_prompt_overrides,
    parse_briefing_time,
)
from backend.prefs import NOTIF_PREFS
from backend.scam.settings import get_scam_detection_settings, reconfigure_scam_service
from backend.secret_prefs import clear_secret_pref, get_shield_api_key, migrate_plaintext_secrets
from backend.sessions import get_idle_timeout_minutes

logger = logging.getLogger(__name__)

router = APIRouter()


# Prefs the UI may read back via GET /prefs. Everything else (API keys, sync
# bookkeeping, prompt overrides with their own endpoints) stays server-side.
PUBLIC_PREF_KEYS = (
    "notif_pref",
    "notif_pref_channels",
    "email_remote_images_senders",
    "gmail_backfill_days",
    "sms_backfill_days",
    "briefing_schedule",
    "briefing_time",
    "briefing_day",
    "briefing_max_tokens",
    "briefing_last_run",
    "catchup_summary_interval_minutes",
    "translation_language",
    "session_idle_timeout_minutes",
)

def _store_secret_pref(key: str, value: str) -> None:
    if vault.vault_status() != "unlocked":
        raise HTTPException(status_code=409, detail="Vault must be unlocked to store an API key")
    vault.save_secret(key, value)



def on_vault_unlocked() -> None:
    """Unlock listener: migrate legacy keys and hand vault-held keys to the
    services that were configured at startup while the vault was locked."""
    migrate_plaintext_secrets()
    prefs = _prefs.load_prefs()
    if prefs.get("scam_detection_mode") == "external":
        reconfigure_scam_service()
    try:
        from backend.scam.cloud_sync import get_cloud_sync_manager

        mgr = get_cloud_sync_manager()
        if mgr:
            mgr.configure(base_url=prefs.get("shield_cloud_url", ""), api_key=get_shield_api_key())
    except Exception:
        logger.exception("Failed to reconfigure Shield cloud sync after unlock")




class NotifPrefRequest(BaseModel):
    pref: str  # 'all' | 'tracked' | 'off'
    channel: Optional[str] = None  # None = the global setting


class RemoteImagesRequest(BaseModel):
    sender: str  # email address
    allowed: bool


class BriefingScheduleRequest(BaseModel):
    schedule: str  # 'daily' | 'weekly'


class BriefingTimeRequest(BaseModel):
    time: str  # "HH:MM" in 24h format, e.g. "09:00"



@router.get("/prefs")
async def get_prefs():
    """Whitelisted view of prefs.json (never includes secrets)."""
    prefs = _prefs.load_prefs()
    return {k: prefs[k] for k in PUBLIC_PREF_KEYS if k in prefs}


@router.post("/prefs/notifications")
async def set_notif_pref(req: NotifPrefRequest):
    if req.pref not in NOTIF_PREFS:
        raise HTTPException(status_code=400, detail="Invalid preference")
    prefs = _prefs.load_prefs()
    if req.channel:
        per_channel = prefs.get("notif_pref_channels")
        per_channel = dict(per_channel) if isinstance(per_channel, dict) else {}
        per_channel[req.channel] = req.pref
        prefs["notif_pref_channels"] = per_channel
    else:
        prefs["notif_pref"] = req.pref
    _prefs.save_prefs(prefs)
    return {"success": True, "pref": req.pref, "channel": req.channel}


@router.post("/prefs/email-remote-images")
async def set_email_remote_images(req: RemoteImagesRequest):
    """Remember (or forget) that remote images load for one email sender."""
    sender = req.sender.strip().lower()
    if "@" not in sender:
        raise HTTPException(status_code=400, detail="Invalid sender address")
    prefs = _prefs.load_prefs()
    current = prefs.get("email_remote_images_senders")
    senders = [s for s in current if isinstance(s, str)] if isinstance(current, list) else []
    senders = [s for s in senders if s != sender] + ([sender] if req.allowed else [])
    prefs["email_remote_images_senders"] = sorted(senders)
    _prefs.save_prefs(prefs)
    return {"success": True, "senders": prefs["email_remote_images_senders"]}


@router.post("/prefs/briefing-schedule")
async def set_briefing_schedule(req: BriefingScheduleRequest):
    if req.schedule not in ("daily", "weekly"):
        raise HTTPException(status_code=400, detail="Invalid schedule")
    prefs = _prefs.load_prefs()
    prefs["briefing_schedule"] = req.schedule
    _prefs.save_prefs(prefs)
    return {"success": True, "schedule": req.schedule}


@router.post("/prefs/briefing-time")
async def set_briefing_time(req: BriefingTimeRequest):
    """Set the hour/minute at which the briefing auto-runs (local time, 24h HH:MM)."""
    if parse_briefing_time(req.time) is None:
        raise HTTPException(status_code=400, detail="Invalid time. Use 24-hour HH:MM (00:00-23:59).")
    prefs = _prefs.load_prefs()
    prefs["briefing_time"] = req.time
    _prefs.save_prefs(prefs)
    return {"success": True, "time": req.time}


class BriefingDayRequest(BaseModel):
    day: str  # 'monday' | 'tuesday' | ... | 'sunday'


@router.post("/prefs/briefing-day")
async def set_briefing_day(req: BriefingDayRequest):
    """Set the day of week on which the weekly briefing auto-runs."""
    valid = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    if req.day not in valid:
        raise HTTPException(status_code=400, detail="Invalid day")
    prefs = _prefs.load_prefs()
    prefs["briefing_day"] = req.day
    _prefs.save_prefs(prefs)
    return {"success": True, "day": req.day}


class BriefingMaxTokensRequest(BaseModel):
    max_tokens: int


class BriefingPromptRequest(BaseModel):
    prompt: str


class BriefingPromptsRequest(BaseModel):
    tracked_prompt: str
    briefing_prompt: str



@router.post("/prefs/briefing-max-tokens")
async def set_briefing_max_tokens(req: BriefingMaxTokensRequest):
    """Set max_tokens used when calling the LLM for each briefing summary."""
    if not (256 <= req.max_tokens <= 32000):
        raise HTTPException(status_code=400, detail="max_tokens must be between 256 and 32000")
    prefs = _prefs.load_prefs()
    prefs["briefing_max_tokens"] = req.max_tokens
    _prefs.save_prefs(prefs)
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
    prefs = _prefs.load_prefs()
    prompt = (req.prompt or "").strip()
    if prompt:
        prefs["briefing_prompt_override"] = prompt
    else:
        prefs.pop("briefing_prompt_override", None)
    _prefs.save_prefs(prefs)
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
    prefs = _prefs.load_prefs()
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

    _prefs.save_prefs(prefs)
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
    if not (5 <= req.interval_minutes <= 1440):
        raise HTTPException(status_code=400, detail="Interval must be between 5 and 1440 minutes")
    prefs = _prefs.load_prefs()
    prefs["catchup_summary_interval_minutes"] = req.interval_minutes
    _prefs.save_prefs(prefs)
    return {"success": True, "interval_minutes": req.interval_minutes}


@router.post("/prefs/briefing-last-run")
async def set_briefing_last_run():
    """Mark that the briefing was just run (stores current UTC ISO timestamp)."""
    from datetime import datetime
    prefs = _prefs.load_prefs()
    prefs["briefing_last_run"] = datetime.now().isoformat()
    _prefs.save_prefs(prefs)
    return {"success": True, "briefing_last_run": prefs["briefing_last_run"]}


# ---------------------------------------------------------------------------
# User organizations
# ---------------------------------------------------------------------------

class UserOrganizationsRequest(BaseModel):
    # Org ids (preferred) or names; unmatched names are kept as name-only entries.
    organizations: List[Union[int, str]]


def _user_orgs_response(entries: list) -> dict:
    return {
        "organizations": [e["name"] for e in entries],
        "entries": entries,
    }


@router.get("/prefs/user-organizations")
async def get_user_organizations(db: AsyncSession = Depends(get_db)):
    """The organizations the user belongs to: names (legacy shape) plus
    ``entries`` [{id, name}] where id is null for names matching no org."""
    from backend.user_orgs import resolve_user_orgs

    return _user_orgs_response(await resolve_user_orgs(db))


@router.put("/prefs/user-organizations")
async def set_user_organizations(req: UserOrganizationsRequest, db: AsyncSession = Depends(get_db)):
    """Set which organizations the user belongs to (ids or names)."""
    from backend.user_orgs import set_user_orgs

    return {"success": True, **_user_orgs_response(await set_user_orgs(db, req.organizations))}


class TranslationLanguageRequest(BaseModel):
    language: str


@router.post("/prefs/translation-language")
async def set_translation_language(req: TranslationLanguageRequest):
    prefs = _prefs.load_prefs()
    prefs["translation_language"] = req.language
    _prefs.save_prefs(prefs)
    return {"success": True, "language": req.language}


# ---------------------------------------------------------------------------
# Scam detection settings
# ---------------------------------------------------------------------------

class ScamDetectionSettingsRequest(BaseModel):
    mode: str  # 'local_llm' | 'external'
    api_url: str = ""
    api_key: str = ""
    max_tokens: int = 200



@router.get("/prefs/scam-detection", dependencies=[Depends(require_shield)])
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


@router.post("/prefs/scam-detection", dependencies=[Depends(require_shield)])
async def set_scam_detection(req: ScamDetectionSettingsRequest):
    """Update scam detection configuration and reconfigure the service."""

    if req.mode not in ("local_llm", "external"):
        raise HTTPException(status_code=400, detail="Mode must be 'local_llm' or 'external'")

    if req.mode == "external" and not req.api_url.strip():
        raise HTTPException(status_code=400, detail="API URL is required for external mode")

    prefs = _prefs.load_prefs()
    prefs["scam_detection_mode"] = req.mode
    prefs["scam_detection_api_url"] = req.api_url.strip()
    prefs["scam_detection_max_tokens"] = max(50, min(2000, req.max_tokens))
    # Only update API key if provided (non-empty), otherwise keep existing
    if req.api_key:
        _store_secret_pref("scam_detection_api_key", req.api_key)
    elif req.mode == "local_llm":
        # Clear API key when switching to local mode
        clear_secret_pref("scam_detection_api_key")
        prefs.pop("scam_detection_api_key", None)
    _prefs.save_prefs(prefs)

    # Reconfigure the live scam service
    reconfigure_scam_service()

    return {"success": True, "mode": req.mode}


class ShieldCloudSettingsRequest(BaseModel):
    cloud_url: str = ""
    api_key: str = ""
    sync_dismissals: Optional[bool] = None


@router.get("/prefs/shield-cloud", dependencies=[Depends(require_shield)])
async def get_shield_cloud():
    """Get Shield cloud sync configuration."""
    prefs = _prefs.load_prefs()
    api_key = get_shield_api_key()
    return {
        "cloud_url": prefs.get("shield_cloud_url", ""),
        "api_key_set": bool(api_key),
        "last_synced_at": prefs.get("shield_last_synced_at"),
        "sync_dismissals": prefs.get("shield_sync_dismissals", False),
    }


@router.post("/prefs/shield-cloud", dependencies=[Depends(require_shield)])
async def set_shield_cloud(req: ShieldCloudSettingsRequest):
    """Update Shield cloud sync configuration."""
    if req.api_key:
        _store_secret_pref("shield_api_key", req.api_key)
    prefs = _prefs.load_prefs()
    prefs["shield_cloud_url"] = req.cloud_url.strip()
    if req.sync_dismissals is not None:
        prefs["shield_sync_dismissals"] = req.sync_dismissals
    _prefs.save_prefs(prefs)

    # Reconfigure the sync manager
    from backend.scam.cloud_sync import get_cloud_sync_manager
    mgr = get_cloud_sync_manager()
    if mgr:
        mgr.configure(
            base_url=req.cloud_url.strip(),
            api_key=get_shield_api_key(),
        )

    return {"success": True}


# ---------------------------------------------------------------------------
# Session idle timeout
# ---------------------------------------------------------------------------

class SessionTimeoutRequest(BaseModel):
    minutes: int  # 0 = never expire


@router.get("/prefs/session-timeout")
async def get_session_timeout():
    return {"minutes": get_idle_timeout_minutes()}


@router.post("/prefs/session-timeout")
async def set_session_timeout(req: SessionTimeoutRequest):
    """Idle minutes after which a window's session token expires (0 = never, else 5-10080)."""
    if req.minutes != 0 and not (5 <= req.minutes <= 10080):
        raise HTTPException(status_code=400, detail="Timeout must be 0 (never) or between 5 and 10080 minutes")
    prefs = _prefs.load_prefs()
    prefs["session_idle_timeout_minutes"] = req.minutes
    _prefs.save_prefs(prefs)
    return {"success": True, "minutes": req.minutes}


# ---------------------------------------------------------------------------
# MCP server settings
# ---------------------------------------------------------------------------

def get_mcp_enabled() -> bool:
    """Whether the MCP endpoint should accept requests (default off)."""
    return bool(_prefs.load_prefs().get("mcp_enabled", False))


def get_mcp_write_enabled() -> bool:
    """Whether MCP write tools (notes, person fields, tags, review marks) may run (default off)."""
    return bool(_prefs.load_prefs().get("mcp_write_enabled", False))


class McpSettingsRequest(BaseModel):
    enabled: bool


class McpWriteRequest(BaseModel):
    enabled: bool


@router.get("/prefs/mcp")
async def get_mcp_settings():
    """MCP server status: enabled flag, write access, whether a key exists, and the live URL."""
    from backend.config import API_PORT
    from backend.secrets import get_secret, vault_status

    return {
        "enabled": get_mcp_enabled(),
        "write_enabled": get_mcp_write_enabled(),
        "key_set": get_secret("mcp_api_key") is not None,
        "url": f"http://127.0.0.1:{API_PORT}/mcp",
        "vault_unlocked": vault_status() == "unlocked",
    }


@router.post("/prefs/mcp")
async def set_mcp_settings(req: McpSettingsRequest):
    from backend.secrets import get_secret

    prefs = _prefs.load_prefs()
    prefs["mcp_enabled"] = req.enabled
    _prefs.save_prefs(prefs)
    return {
        "success": True,
        "enabled": req.enabled,
        "key_set": get_secret("mcp_api_key") is not None,
    }


@router.post("/prefs/mcp/write")
async def set_mcp_write(req: McpWriteRequest):
    """Allow or forbid the MCP write tools. Read tools are unaffected."""
    prefs = _prefs.load_prefs()
    prefs["mcp_write_enabled"] = req.enabled
    _prefs.save_prefs(prefs)
    return {"write_enabled": req.enabled}


@router.post("/prefs/mcp/generate-key")
async def generate_mcp_key():
    """Create (or rotate) the MCP bearer key. Any connected clients are cut off."""
    import secrets as pysecrets

    from backend.secrets import save_secret, vault_status

    if vault_status() != "unlocked":
        raise HTTPException(status_code=409, detail="Vault must be unlocked to manage the MCP key")
    key = pysecrets.token_urlsafe(32)
    save_secret("mcp_api_key", key)
    return {"key": key}


@router.post("/prefs/mcp/reveal-key")
async def reveal_mcp_key():
    from backend.secrets import get_secret, vault_status

    if vault_status() != "unlocked":
        raise HTTPException(status_code=409, detail="Vault must be unlocked to reveal the MCP key")
    key = get_secret("mcp_api_key")
    if key is None:
        raise HTTPException(status_code=404, detail="No MCP key has been generated")
    return {"key": key}


