"""
REST endpoints for cloud account management (connect / disconnect / status),
email management, and org management.
"""

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

from backend.cloud import auth as cloud_auth
from backend.cloud.client import CloudAPIClient, CloudAPIError
from backend.cloud.session import cloud_session
from backend.ws.manager import ws_manager

router = APIRouter()
log = logging.getLogger(__name__)

# In-memory state for an active connect (magic-link) flow
_active_connect: Optional[asyncio.Task] = None

# In-memory state for active email verification polling tasks
_email_verify_tasks: dict[str, asyncio.Task] = {}


# ── Helpers ───────────────────────────────────────────────────────────────

def _require_connected() -> str:
    """Return the current access token or raise 401."""
    token = cloud_session.access_token
    if not token or cloud_session.status != "connected":
        raise HTTPException(status_code=401, detail="Not connected to cloud")
    return token


def _get_client() -> CloudAPIClient:
    return CloudAPIClient()


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


class AddEmailRequest(BaseModel):
    email: EmailStr


class RemoveEmailRequest(BaseModel):
    confirm: bool = False


class CreateOrgRequest(BaseModel):
    name: str
    domain: str


class UpdateMemberRequest(BaseModel):
    status: Optional[str] = None
    role: Optional[str] = None


class ShareContactRequest(BaseModel):
    telegram_id: int
    name: str
    telegram_handle: Optional[str] = None


class CreateSharedOrgRequest(BaseModel):
    name: str
    domain: Optional[str] = None
    website: Optional[str] = None


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


# ── Email management endpoints ────────────────────────────────────────────

@router.get("/profile")
async def cloud_profile():
    """Get user profile with verified emails."""
    token = _require_connected()
    try:
        return await _get_client().get_profile(token)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/emails")
async def cloud_add_email(req: AddEmailRequest):
    """Add a new email address (triggers verification magic link).

    Returns request_id for polling verification status.
    """
    token = _require_connected()
    try:
        result = await _get_client().add_email(token, req.email)
        # Return the request_id and claim_secret so frontend can poll
        return {
            "status": "verification_sent",
            "request_id": result.get("request_id"),
            "claim_secret": result.get("claim_secret"),
            "message": result.get("message", "Verification email sent. Check your inbox."),
        }
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/emails/verify-status/{request_id}")
async def cloud_email_verify_status(request_id: str, claim_secret: str):
    """Poll verification status for an email being verified."""
    token = _require_connected()
    try:
        result = await _get_client().poll_email_verification(
            token, request_id, claim_secret
        )
        return result
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.delete("/emails/{email_id}")
async def cloud_remove_email(email_id: str, confirm: bool = False):
    """Remove a verified email. Pass confirm=true to cascade org membership revocations."""
    token = _require_connected()
    try:
        return await _get_client().remove_email(token, email_id, confirm=confirm)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


# ── Org management endpoints ─────────────────────────────────────────────

@router.get("/orgs")
async def cloud_list_orgs():
    """List orgs the user belongs to."""
    token = _require_connected()
    try:
        return await _get_client().list_orgs(token)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/orgs")
async def cloud_create_org(req: CreateOrgRequest):
    """Create a new org (requires verified email on that domain)."""
    token = _require_connected()
    try:
        return await _get_client().create_org(token, req.name, req.domain)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}")
async def cloud_get_org(org_id: str):
    """Get org details."""
    token = _require_connected()
    try:
        return await _get_client().get_org(token, org_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/orgs/{org_id}/join")
async def cloud_join_org(org_id: str):
    """Join or request to join an org."""
    token = _require_connected()
    try:
        return await _get_client().join_org(token, org_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}/members")
async def cloud_list_members(org_id: str):
    """List org members."""
    token = _require_connected()
    try:
        return await _get_client().list_members(token, org_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}/members/pending")
async def cloud_list_pending(org_id: str):
    """List pending join requests (admin only)."""
    token = _require_connected()
    try:
        return await _get_client().list_pending(token, org_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.patch("/orgs/{org_id}/members/{user_id}")
async def cloud_update_member(org_id: str, user_id: str, req: UpdateMemberRequest):
    """Update member role or status (admin only)."""
    token = _require_connected()
    data = {}
    if req.status is not None:
        data["status"] = req.status
    if req.role is not None:
        data["role"] = req.role
    try:
        return await _get_client().update_member(token, org_id, user_id, data)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.delete("/orgs/{org_id}/members/{user_id}")
async def cloud_remove_member(org_id: str, user_id: str):
    """Remove a member from the org (admin or self)."""
    token = _require_connected()
    try:
        return await _get_client().remove_member(token, org_id, user_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


# ── Shared contacts endpoints ────────────────────────────────────────────

@router.post("/orgs/{org_id}/share-contact")
async def cloud_share_contact(org_id: str, req: ShareContactRequest):
    """Share a local contact to the org's shared pool.

    Creates a SharedContact via the cloud API.  Returns the created contact,
    or a 409 with the existing contact if the telegram_id is already shared.
    """
    token = _require_connected()
    client = _get_client()
    data: dict = {
        "telegram_id": req.telegram_id,
        "name": req.name,
    }
    if req.telegram_handle:
        data["telegram_handle"] = req.telegram_handle
    try:
        return await client.create_shared_contact(token, org_id, data)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}/contacts")
async def cloud_list_shared_contacts(org_id: str):
    """List all shared contacts for the org."""
    token = _require_connected()
    try:
        return await _get_client().list_shared_contacts(token, org_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}/contacts/{contact_id}/links")
async def cloud_list_contact_links(org_id: str, contact_id: str):
    """List org links for a shared contact."""
    token = _require_connected()
    try:
        return await _get_client().list_contact_links(token, org_id, contact_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/orgs/{org_id}/contacts/{contact_id}/links")
async def cloud_create_contact_link(org_id: str, contact_id: str, request: Request):
    """Link a shared contact to an external org."""
    token = _require_connected()
    data = await request.json()
    try:
        return await _get_client().create_contact_link(token, org_id, contact_id, data)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}/directory")
async def cloud_list_directory(org_id: str):
    """List shared external organizations for the org."""
    token = _require_connected()
    try:
        return await _get_client().list_directory(token, org_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/orgs/{org_id}/directory")
async def cloud_create_shared_org(org_id: str, req: CreateSharedOrgRequest):
    """Create a shared (external) organization in the org's directory."""
    token = _require_connected()
    data: dict = {"name": req.name}
    if req.domain:
        data["domain"] = req.domain
    if req.website:
        data["website"] = req.website
    try:
        return await _get_client().create_shared_org(token, org_id, data)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
