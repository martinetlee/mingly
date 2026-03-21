"""
REST endpoints for cloud account management (connect / disconnect / status).
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr

from backend.cloud import auth as cloud_auth
from backend.cloud.client import CloudAPIError
from backend.cloud.session import cloud_session
from backend.ws.manager import ws_manager

router = APIRouter()
log = logging.getLogger(__name__)

# In-memory state for an active connect (magic-link) flow
_active_connect: Optional[asyncio.Task] = None


# ── Request / response models ─────────────────────────────────────────────

class ConnectRequest(BaseModel):
    email: EmailStr


class ConnectResponse(BaseModel):
    status: str  # "polling" | "error"
    message: str


class CloudStatusResponse(BaseModel):
    status: str  # "connected" | "disconnected" | "connecting" | "offline" | "expired"
    email: Optional[str] = None


class DisconnectResponse(BaseModel):
    status: str
    message: str


# ── Helpers ────────────────────────────────────────────────────────────────

async def _broadcast_cloud_status() -> None:
    """Push the current cloud status to all connected WebSocket clients."""
    status = cloud_session.status
    email = cloud_session.email
    await ws_manager.broadcast({
        "type": "cloud_status",
        "status": status,
        "email": email,
    })


async def _connect_background(email: str) -> None:
    """Run the full magic-link connect flow in the background.

    Broadcasts status updates via WebSocket so the frontend can react.
    """
    global _active_connect
    try:
        # Step 1: request magic link
        result = await cloud_auth.connect(email)
        request_id = result["request_id"]
        claim_secret = result["claim_secret"]

        # Broadcast "connecting" status
        await ws_manager.broadcast({
            "type": "cloud_status",
            "status": "connecting",
            "email": email,
        })

        # Step 2: poll for completion
        claim = await cloud_auth.poll_for_completion(request_id, claim_secret)

        # Step 3: activate the session
        await cloud_session.on_connect(claim["access_token"])

        # Broadcast success
        await _broadcast_cloud_status()

    except TimeoutError as exc:
        log.info("Cloud connect timed out: %s", exc)
        await ws_manager.broadcast({
            "type": "cloud_status",
            "status": "timeout",
            "email": email,
            "error": str(exc),
        })
    except CloudAPIError as exc:
        log.warning("Cloud connect API error: %s", exc)
        error_msg = exc.detail
        if exc.status_code == 429:
            error_msg = "Too many attempts. Please wait before trying again."
        await ws_manager.broadcast({
            "type": "cloud_status",
            "status": "error",
            "email": email,
            "error": error_msg,
        })
    except asyncio.CancelledError:
        log.info("Cloud connect cancelled")
    except Exception as exc:
        log.exception("Cloud connect unexpected error: %s", exc)
        await ws_manager.broadcast({
            "type": "cloud_status",
            "status": "error",
            "email": email,
            "error": "An unexpected error occurred. Please try again.",
        })
    finally:
        _active_connect = None


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/connect", response_model=ConnectResponse)
async def cloud_connect(req: ConnectRequest):
    """Start the magic-link connect flow for the given email.

    The actual polling happens in a background task; the frontend receives
    status updates via WebSocket (``cloud_status`` events).
    """
    global _active_connect

    # Cancel any in-progress connect
    if _active_connect and not _active_connect.done():
        _active_connect.cancel()

    _active_connect = asyncio.create_task(_connect_background(req.email))

    return ConnectResponse(
        status="polling",
        message="Magic link requested. Check your email.",
    )


@router.get("/status", response_model=CloudStatusResponse)
async def cloud_status():
    """Return the current cloud connection status."""
    global _active_connect

    if _active_connect and not _active_connect.done():
        return CloudStatusResponse(status="connecting", email=None)

    status = cloud_session.status
    email = cloud_session.email
    return CloudStatusResponse(status=status, email=email)


@router.post("/disconnect", response_model=DisconnectResponse)
async def cloud_disconnect():
    """Disconnect from the cloud (revoke + clear credentials)."""
    global _active_connect

    # Cancel any in-progress connect
    if _active_connect and not _active_connect.done():
        _active_connect.cancel()
        _active_connect = None

    try:
        await cloud_auth.disconnect()
        await cloud_session.on_disconnect()
        await _broadcast_cloud_status()
        return DisconnectResponse(status="disconnected", message="Cloud account disconnected.")
    except Exception as exc:
        log.exception("Cloud disconnect error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to disconnect. Please try again.")


@router.post("/cancel-connect")
async def cloud_cancel_connect():
    """Cancel an in-progress connect flow."""
    global _active_connect
    if _active_connect and not _active_connect.done():
        _active_connect.cancel()
        _active_connect = None
    await _broadcast_cloud_status()
    return {"status": "cancelled"}
