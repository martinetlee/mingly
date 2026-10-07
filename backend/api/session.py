"""Unauthenticated bootstrap endpoints: session validation, health/readiness,
and single-instance focus. All are protected by the app-wide Host/Origin
middleware; none reveals data or changes state beyond showing the window."""

from typing import Optional

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from backend import instance
from backend.api.dependencies import get_idle_timeout_minutes, is_valid_session_token
from backend.secrets import vault_status

router = APIRouter()
health_router = APIRouter()


@router.get("/validate")
async def validate_session(x_session_token: Optional[str] = Header(None)):
    """Tell the frontend at boot whether its stored token is still usable.

    ``valid`` false with ``vault_status`` 'unlocked' means "re-enter the
    password to get a token for this window"; the vault itself stays unlocked.
    A valid check counts as activity for the idle timer.
    """
    return {
        "valid": is_valid_session_token(x_session_token, touch=True),
        "vault_status": vault_status(),
        "idle_timeout_minutes": get_idle_timeout_minutes(),
    }


@health_router.get("/health")
async def health():
    return {"app": "mingly", "ready": instance.is_ready()}


@health_router.get("/health/ready")
async def health_ready():
    ready = instance.is_ready()
    return JSONResponse({"ready": ready}, status_code=200 if ready else 503)


@health_router.post("/instance/focus")
async def focus_instance():
    """Called by a second launch of the app so this instance shows its window."""
    return {"focused": instance.request_focus()}
