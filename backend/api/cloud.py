"""
REST endpoints for cloud account management (connect / disconnect / status),
email management, and org management.
"""

import asyncio
import functools
import logging
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr

from backend.cloud import auth as cloud_auth
from backend.cloud.client import CloudAPIClient, CloudAPIError
from backend.cloud.session import cloud_session
from backend.ws.manager import ws_manager

router = APIRouter()
log = logging.getLogger(__name__)


def _cloud_api(fn):
    """Decorator that converts ``CloudAPIError`` into ``HTTPException``.

    Eliminates the repetitive try/except CloudAPIError pattern.
    """
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except CloudAPIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    return wrapper

# In-memory state for an active connect (magic-link) flow
_active_connect: Optional[asyncio.Task] = None


# ── Helpers ───────────────────────────────────────────────────────────────

def _require_connected() -> str:
    """Return the current access token or raise 401."""
    token = cloud_session.access_token
    if not token or cloud_session.status != "connected":
        raise HTTPException(status_code=401, detail="Not connected to cloud")
    return token


def _get_client() -> CloudAPIClient:
    """Return the shared CloudAPIClient from the cloud session singleton."""
    return cloud_session.client


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
    status: Optional[Literal["active", "revoked"]] = None
    role: Optional[Literal["admin", "member"]] = None


class ShareContactRequest(BaseModel):
    telegram_id: int
    name: str
    telegram_handle: Optional[str] = None


class CreateSharedOrgRequest(BaseModel):
    name: str
    domain: Optional[str] = None
    website: Optional[str] = None


class CreateContactLinkRequest(BaseModel):
    shared_org_id: str
    position: Optional[str] = None
    relationship_context: Optional[str] = None


class UpdateSharedContactRequest(BaseModel):
    name: Optional[str] = None
    telegram_handle: Optional[str] = None


class UpdateContactLinkRequest(BaseModel):
    position: Optional[str] = None
    relationship_context: Optional[str] = None


class BackupEnableRequest(BaseModel):
    master_password: str


class BackupDisableRequest(BaseModel):
    delete_remote: bool = False


class BackupRestoreRequest(BaseModel):
    master_password: str



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
    current_task = asyncio.current_task()
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
        if _active_connect is current_task:
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
@_cloud_api
async def cloud_profile():
    """Get user profile with verified emails."""
    token = _require_connected()
    return await _get_client().get_profile(token)


@router.post("/emails")
@_cloud_api
async def cloud_add_email(req: AddEmailRequest):
    """Add a new email address (triggers verification magic link).

    Returns request_id for polling verification status.
    """
    token = _require_connected()
    result = await _get_client().add_email(token, req.email)
    # Return the request_id and claim_secret so frontend can poll
    return {
        "status": "verification_sent",
        "request_id": result.get("request_id"),
        "claim_secret": result.get("claim_secret"),
        "message": result.get("message", "Verification email sent. Check your inbox."),
    }


@router.get("/emails/verify-status/{request_id}")
@_cloud_api
async def cloud_email_verify_status(request_id: str, request: Request):
    """Poll verification status for an email being verified."""
    claim_secret = request.headers.get("X-Claim-Secret")
    if not claim_secret:
        raise HTTPException(status_code=400, detail="Missing X-Claim-Secret header")
    token = _require_connected()
    return await _get_client().poll_email_verification(
        token, request_id, claim_secret
    )


@router.delete("/emails/{email_id}")
@_cloud_api
async def cloud_remove_email(email_id: str, confirm: bool = False):
    """Remove a verified email. Pass confirm=true to cascade org membership revocations."""
    token = _require_connected()
    return await _get_client().remove_email(token, email_id, confirm=confirm)


# ── Org management endpoints ─────────────────────────────────────────────

@router.get("/orgs")
@_cloud_api
async def cloud_list_orgs():
    """List orgs the user belongs to."""
    token = _require_connected()
    return await _get_client().list_orgs(token)


@router.post("/orgs")
@_cloud_api
async def cloud_create_org(req: CreateOrgRequest):
    """Create a new org (requires verified email on that domain)."""
    token = _require_connected()
    return await _get_client().create_org(token, req.name, req.domain)


@router.get("/orgs/{org_id}")
@_cloud_api
async def cloud_get_org(org_id: str):
    """Get org details."""
    token = _require_connected()
    return await _get_client().get_org(token, org_id)


@router.post("/orgs/{org_id}/join")
@_cloud_api
async def cloud_join_org(org_id: str):
    """Join or request to join an org."""
    token = _require_connected()
    return await _get_client().join_org(token, org_id)


@router.get("/orgs/{org_id}/members")
@_cloud_api
async def cloud_list_members(org_id: str):
    """List org members."""
    token = _require_connected()
    return await _get_client().list_members(token, org_id)


@router.get("/orgs/{org_id}/members/pending")
@_cloud_api
async def cloud_list_pending(org_id: str):
    """List pending join requests (admin only)."""
    token = _require_connected()
    return await _get_client().list_pending(token, org_id)


@router.patch("/orgs/{org_id}/members/{user_id}")
@_cloud_api
async def cloud_update_member(org_id: str, user_id: str, req: UpdateMemberRequest):
    """Update member role or status (admin only)."""
    token = _require_connected()
    data = {}
    if req.status is not None:
        data["status"] = req.status
    if req.role is not None:
        data["role"] = req.role
    return await _get_client().update_member(token, org_id, user_id, data)


@router.delete("/orgs/{org_id}/members/{user_id}")
@_cloud_api
async def cloud_remove_member(org_id: str, user_id: str):
    """Remove a member from the org (admin or self)."""
    token = _require_connected()
    return await _get_client().remove_member(token, org_id, user_id)


# ── Shared contacts endpoints ────────────────────────────────────────────

@router.post("/orgs/{org_id}/share-contact")
@_cloud_api
async def cloud_share_contact(org_id: str, req: ShareContactRequest):
    """Share a local contact to the org's shared pool.

    Creates a SharedContact via the cloud API.  Returns the created contact,
    or a 409 with the existing contact if the telegram_id is already shared.
    """
    token = _require_connected()
    data: dict = {
        "telegram_id": req.telegram_id,
        "name": req.name,
    }
    if req.telegram_handle:
        data["telegram_handle"] = req.telegram_handle
    return await _get_client().create_shared_contact(token, org_id, data)


@router.get("/orgs/{org_id}/contacts")
@_cloud_api
async def cloud_list_shared_contacts(org_id: str):
    """List all shared contacts for the org."""
    token = _require_connected()
    return await _get_client().list_shared_contacts(token, org_id)


@router.get("/orgs/{org_id}/contacts/{contact_id}/links")
@_cloud_api
async def cloud_list_contact_links(org_id: str, contact_id: str):
    """List org links for a shared contact."""
    token = _require_connected()
    return await _get_client().list_contact_links(token, org_id, contact_id)


@router.post("/orgs/{org_id}/contacts/{contact_id}/links")
@_cloud_api
async def cloud_create_contact_link(org_id: str, contact_id: str, req: CreateContactLinkRequest):
    """Link a shared contact to an external org."""
    token = _require_connected()
    data = req.model_dump(exclude_none=True)
    return await _get_client().create_contact_link(token, org_id, contact_id, data)

@router.get("/orgs/{org_id}/contacts/{contact_id}")
async def cloud_get_shared_contact(org_id: str, contact_id: str):
    """Get shared contact detail."""
    token = _require_connected()
    try:
        return await _get_client().get_shared_contact(token, org_id, contact_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.patch("/orgs/{org_id}/contacts/{contact_id}")
@_cloud_api
async def cloud_update_shared_contact(org_id: str, contact_id: str, req: UpdateSharedContactRequest):
    """Update shared contact info (name, telegram_handle)."""
    token = _require_connected()
    data = req.model_dump(exclude_none=True)
    return await _get_client().update_shared_contact(token, org_id, contact_id, data)


@router.patch("/orgs/{org_id}/contacts/{contact_id}/links/{link_id}")
@_cloud_api
async def cloud_update_contact_link(org_id: str, contact_id: str, link_id: str, req: UpdateContactLinkRequest):
    """Update a contact org link (position, relationship_context)."""
    token = _require_connected()
    data = req.model_dump(exclude_none=True)
    return await _get_client().update_contact_link(token, org_id, contact_id, link_id, data)


@router.delete("/orgs/{org_id}/contacts/{contact_id}/links/{link_id}")
async def cloud_delete_contact_link(org_id: str, contact_id: str, link_id: str):
    """Soft-delete a contact org link."""
    token = _require_connected()
    try:
        return await _get_client().delete_contact_link(token, org_id, contact_id, link_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}/contacts/{contact_id}/group-chats")
async def cloud_list_group_chats(org_id: str, contact_id: str):
    """List group chats a shared contact is in."""
    token = _require_connected()
    try:
        return await _get_client().list_group_chats(token, org_id, contact_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}/directory/{shared_org_id}")
async def cloud_get_shared_org_detail(org_id: str, shared_org_id: str):
    """Get shared org details + linked contacts."""
    token = _require_connected()
    try:
        return await _get_client().get_shared_org_detail(token, org_id, shared_org_id)
    except CloudAPIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/orgs/{org_id}/directory")
@_cloud_api
async def cloud_list_directory(org_id: str):
    """List shared external organizations for the org."""
    token = _require_connected()
    return await _get_client().list_directory(token, org_id)


@router.post("/orgs/{org_id}/directory")
@_cloud_api
async def cloud_create_shared_org(org_id: str, req: CreateSharedOrgRequest):
    """Create a shared (external) organization in the org's directory."""
    token = _require_connected()
    data: dict = {"name": req.name}
    if req.domain:
        data["domain"] = req.domain
    if req.website:
        data["website"] = req.website
    return await _get_client().create_shared_org(token, org_id, data)


# ── Backup endpoints ─────────────────────────────────────────────────────

@router.post("/backup/enable")
@_cloud_api
async def cloud_backup_enable(req: BackupEnableRequest):
    """Enable encrypted backup sync.

    Derives the backup key from the master password, encrypts all contacts,
    and uploads the encrypted payload to the cloud.
    """
    _require_connected()
    from backend.cloud.backup import backup_manager
    try:
        return await backup_manager.enable_backup(req.master_password)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("Backup enable failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to enable backup")


@router.post("/backup/disable")
@_cloud_api
async def cloud_backup_disable(req: BackupDisableRequest):
    """Disable backup sync. Optionally delete the remote backup."""
    _require_connected()
    from backend.cloud.backup import backup_manager
    try:
        return await backup_manager.disable_backup(delete_remote=req.delete_remote)
    except Exception as exc:
        log.exception("Backup disable failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to disable backup")


@router.post("/backup/restore")
@_cloud_api
async def cloud_backup_restore(req: BackupRestoreRequest):
    """Restore contacts from an encrypted cloud backup.

    Returns the decrypted contact data without importing it.
    """
    _require_connected()
    from backend.cloud.backup import backup_manager
    try:
        return await backup_manager.restore_backup(req.master_password)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        log.exception("Backup restore failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to restore backup")


@router.post("/backup/sync")
@_cloud_api
async def cloud_backup_sync():
    """Manually trigger a backup sync."""
    _require_connected()
    from backend.cloud.backup import backup_manager
    try:
        return await backup_manager.sync_backup()
    except Exception as exc:
        log.exception("Backup sync failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to sync backup")


@router.get("/backup/status")
async def cloud_backup_status():
    """Get backup status: enabled state, last sync time, dirty flag."""
    from backend.cloud.backup import backup_manager
    return backup_manager.get_status()
