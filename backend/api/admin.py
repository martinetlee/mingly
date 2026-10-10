"""
Admin management endpoints for groups and channels.

Provides promote/demote, ban/restrict/approve, invite link management,
group settings, and admin action log. The channel work is the
conversation's connector's ``GroupAdmin`` role.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.lookups import channel_call, connector_with, conversation_or_404
from backend.connectors.roles import GroupAdmin
from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Conversation
from backend.serialization import iso_utc

router = APIRouter()


# ── Request models ────────────────────────────────────────────────────────

class AdminPermissions(BaseModel):
    change_info: bool = False
    post_messages: bool = False
    edit_messages: bool = False
    delete_messages: bool = False
    ban_users: bool = False
    invite_users: bool = False
    pin_messages: bool = False
    manage_call: bool = False
    anonymous: bool = False
    add_admins: bool = False


class PromoteRequest(BaseModel):
    identity_id: int
    permissions: AdminPermissions


class DemoteRequest(BaseModel):
    identity_id: int


class BanRequest(BaseModel):
    identity_id: int


class RestrictRequest(BaseModel):
    identity_id: int
    send_messages: bool = False
    send_media: bool = False
    send_stickers: bool = False
    send_gifs: bool = False
    send_games: bool = False
    send_inline: bool = False
    embed_links: bool = False
    send_polls: bool = False
    change_info: bool = False
    invite_users: bool = False
    pin_messages: bool = False
    until_date: Optional[int] = None  # Unix timestamp, 0 = forever


class ApproveRequest(BaseModel):
    identity_id: int


class CreateInviteLinkRequest(BaseModel):
    expire_date: Optional[int] = None  # Unix timestamp
    usage_limit: Optional[int] = None
    title: Optional[str] = None
    request_needed: bool = False


class RevokeInviteLinkRequest(BaseModel):
    link: str


class GroupSettingsRequest(BaseModel):
    title: Optional[str] = None
    about: Optional[str] = None


# ── Helper ────────────────────────────────────────────────────────────────

async def _require_group(conversation_id: int, db: AsyncSession) -> tuple[Conversation, GroupAdmin]:
    """The group/channel conversation and its connector (404 / 409 / 400 otherwise)."""
    conv = await conversation_or_404(db, conversation_id)
    connector = connector_with(conv, GroupAdmin, "Admin actions")
    if conv.kind == "direct":
        raise HTTPException(status_code=400, detail="Admin actions are only available for groups and channels")
    return conv, connector


# ── Promote / Demote ─────────────────────────────────────────────────────

@router.post("/{conversation_id}/promote")
async def promote_admin(
    conversation_id: int,
    req: PromoteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Promote a user to admin with granular permissions."""
    conv, connector = await _require_group(conversation_id, db)
    await channel_call(conv, connector.promote(conv, req.identity_id, req.permissions.model_dump()),
                       "Failed to promote user", status=500)
    await crud.log_admin_action(db, conversation_id, "promote", req.identity_id)
    return {"success": True}


@router.post("/{conversation_id}/demote")
async def demote_admin(
    conversation_id: int,
    req: DemoteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Demote an admin back to regular member."""
    conv, connector = await _require_group(conversation_id, db)
    await channel_call(conv, connector.demote(conv, req.identity_id), "Failed to demote user", status=500)
    await crud.log_admin_action(db, conversation_id, "demote", req.identity_id)
    return {"success": True}


# ── Ban / Restrict / Approve ─────────────────────────────────────────────

@router.post("/{conversation_id}/ban")
async def ban_member(
    conversation_id: int,
    req: BanRequest,
    db: AsyncSession = Depends(get_db),
):
    """Ban a user from the group/channel."""
    conv, connector = await _require_group(conversation_id, db)
    await channel_call(conv, connector.ban(conv, req.identity_id), "Failed to ban user", status=500)
    await crud.log_admin_action(db, conversation_id, "ban", req.identity_id)
    return {"success": True}


@router.post("/{conversation_id}/restrict")
async def restrict_member(
    conversation_id: int,
    req: RestrictRequest,
    db: AsyncSession = Depends(get_db),
):
    """Restrict a member's permissions."""
    conv, connector = await _require_group(conversation_id, db)
    allowed = req.model_dump(exclude={"identity_id", "until_date"})
    until = datetime.fromtimestamp(req.until_date) if req.until_date else None
    await channel_call(conv, connector.restrict(conv, req.identity_id, allowed, until),
                       "Failed to restrict user", status=500)
    await crud.log_admin_action(db, conversation_id, "restrict", req.identity_id)
    return {"success": True}


@router.post("/{conversation_id}/approve")
async def approve_member(
    conversation_id: int,
    req: ApproveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Remove all restrictions from a user (unban/unrestrict)."""
    conv, connector = await _require_group(conversation_id, db)
    await channel_call(conv, connector.approve(conv, req.identity_id), "Failed to approve user", status=500)
    await crud.log_admin_action(db, conversation_id, "approve", req.identity_id)
    return {"success": True}


# ── Invite Links ──────────────────────────────────────────────────────────

@router.get("/{conversation_id}/invite-links")
async def get_invite_links(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get all invite links for a group/channel."""
    conv, connector = await _require_group(conversation_id, db)
    return await channel_call(conv, connector.list_invite_links(conv), "Failed to get invite links", status=500)


@router.post("/{conversation_id}/invite-links")
async def create_invite_link(
    conversation_id: int,
    req: CreateInviteLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a new invite link."""
    conv, connector = await _require_group(conversation_id, db)
    link_data = await channel_call(conv, connector.create_invite_link(
        conv,
        expire=datetime.fromtimestamp(req.expire_date) if req.expire_date else None,
        usage_limit=req.usage_limit, title=req.title, request_needed=req.request_needed,
    ), "Failed to create invite link", status=500)
    await crud.log_admin_action(db, conversation_id, "create_invite_link", details=link_data["link"])
    return link_data


@router.post("/{conversation_id}/invite-links/revoke")
async def revoke_invite_link(
    conversation_id: int,
    req: RevokeInviteLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke an existing invite link."""
    conv, connector = await _require_group(conversation_id, db)
    await channel_call(conv, connector.revoke_invite_link(conv, req.link), "Failed to revoke invite link", status=500)
    await crud.log_admin_action(db, conversation_id, "revoke_invite_link", details=req.link)
    return {"success": True}


# ── Group/Channel Settings ───────────────────────────────────────────────

@router.get("/{conversation_id}/settings")
async def get_group_settings(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the current title and about/description for a group or channel."""
    conv, connector = await _require_group(conversation_id, db)
    return await channel_call(conv, connector.get_group_settings(conv), "Failed to get settings", status=500)


@router.put("/{conversation_id}/settings")
async def update_group_settings(
    conversation_id: int,
    req: GroupSettingsRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update group/channel title and/or description."""
    conv, connector = await _require_group(conversation_id, db)
    changes = await channel_call(conv, connector.update_group_settings(conv, title=req.title, about=req.about),
                                 "Failed to update settings", status=500)
    await crud.log_admin_action(db, conversation_id, "update_settings", details=", ".join(changes))
    return {"success": True, "updated": changes}


# ── Admin Action Log ─────────────────────────────────────────────────────

@router.get("/{conversation_id}/action-log")
async def get_admin_action_log(
    conversation_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Recent admin actions taken from the app in a group/channel."""
    await _require_group(conversation_id, db)
    return [
        {
            "id": log.id,
            "conversation_id": log.conversation_id,
            "action": log.action,
            "target_identity_id": log.target_identity_id,
            "target_person_id": person_id,
            "details": log.details,
            "created_at": iso_utc(log.created_at),
        }
        for log, person_id in await crud.admin_action_log(db, conversation_id, limit)
    ]
