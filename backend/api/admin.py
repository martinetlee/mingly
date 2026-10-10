"""
Admin management endpoints for groups and channels.

Provides promote/demote, ban/restrict/approve, invite link management,
group settings, and admin action log.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.lookups import conversation_or_404, telegram_peer
from backend.db.database import get_db
from backend.db.models import AdminActionLog, Identity
from backend.serialization import iso_utc
from backend.telegram import ids
from backend.telegram import client as tg_client

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

async def _require_group(conversation_id: int, db: AsyncSession) -> int:
    """The Telegram peer of a group/channel conversation (404 / 409 / 400 otherwise)."""
    conv = await conversation_or_404(db, conversation_id)
    peer = telegram_peer(conv, "Admin actions")
    if conv.kind == "direct":
        raise HTTPException(status_code=400, detail="Admin actions are only available for groups and channels")
    return peer


async def _user_id(db: AsyncSession, identity_id: int) -> int:
    """The Telegram user id of the identity an admin action targets."""
    user_ids = await ids.user_ids_for(db, [identity_id])
    if identity_id not in user_ids:
        raise HTTPException(status_code=404, detail="Telegram user not found")
    return user_ids[identity_id]


async def _log_admin_action(
    db: AsyncSession,
    conversation_id: int,
    action: str,
    target_identity_id: Optional[int] = None,
    details: Optional[str] = None,
):
    db.add(AdminActionLog(
        conversation_id=conversation_id, action=action, target_identity_id=target_identity_id,
        details=details, created_at=datetime.utcnow(),
    ))
    await db.commit()


# ── Promote / Demote ─────────────────────────────────────────────────────

@router.post("/{conversation_id}/promote")
async def promote_admin(
    conversation_id: int,
    req: PromoteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Promote a user to admin with granular permissions."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        from telethon.tl.functions.channels import EditAdminRequest
        from telethon.tl.types import ChatAdminRights

        rights = ChatAdminRights(
            change_info=req.permissions.change_info,
            post_messages=req.permissions.post_messages,
            edit_messages=req.permissions.edit_messages,
            delete_messages=req.permissions.delete_messages,
            ban_users=req.permissions.ban_users,
            invite_users=req.permissions.invite_users,
            pin_messages=req.permissions.pin_messages,
            manage_call=req.permissions.manage_call,
            anonymous=req.permissions.anonymous,
            add_admins=req.permissions.add_admins,
        )
        await client(EditAdminRequest(
            channel=peer,
            user_id=await _user_id(db, req.identity_id),
            admin_rights=rights,
            rank="Admin",
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to promote user: {e}")

    await _log_admin_action(db, conversation_id, "promote", req.identity_id)
    return {"success": True}


@router.post("/{conversation_id}/demote")
async def demote_admin(
    conversation_id: int,
    req: DemoteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Demote an admin back to regular member."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        from telethon.tl.functions.channels import EditAdminRequest
        from telethon.tl.types import ChatAdminRights

        # All-false rights effectively demotes
        rights = ChatAdminRights()
        await client(EditAdminRequest(
            channel=peer,
            user_id=await _user_id(db, req.identity_id),
            admin_rights=rights,
            rank="",
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to demote user: {e}")

    await _log_admin_action(db, conversation_id, "demote", req.identity_id)
    return {"success": True}


# ── Ban / Restrict / Approve ─────────────────────────────────────────────

@router.post("/{conversation_id}/ban")
async def ban_member(
    conversation_id: int,
    req: BanRequest,
    db: AsyncSession = Depends(get_db),
):
    """Ban a user from the group/channel."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights

        rights = ChatBannedRights(
            until_date=None,
            view_messages=True,
            send_messages=True,
            send_media=True,
            send_stickers=True,
            send_gifs=True,
            send_games=True,
            send_inline=True,
            embed_links=True,
        )
        await client(EditBannedRequest(
            channel=peer,
            participant=await _user_id(db, req.identity_id),
            banned_rights=rights,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to ban user: {e}")

    await _log_admin_action(db, conversation_id, "ban", req.identity_id)
    return {"success": True}


@router.post("/{conversation_id}/restrict")
async def restrict_member(
    conversation_id: int,
    req: RestrictRequest,
    db: AsyncSession = Depends(get_db),
):
    """Restrict a member's permissions."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights

        until = datetime.fromtimestamp(req.until_date) if req.until_date else None
        rights = ChatBannedRights(
            until_date=until,
            send_messages=not req.send_messages,
            send_media=not req.send_media,
            send_stickers=not req.send_stickers,
            send_gifs=not req.send_gifs,
            send_games=not req.send_games,
            send_inline=not req.send_inline,
            embed_links=not req.embed_links,
            send_polls=not req.send_polls,
            change_info=not req.change_info,
            invite_users=not req.invite_users,
            pin_messages=not req.pin_messages,
        )
        await client(EditBannedRequest(
            channel=peer,
            participant=await _user_id(db, req.identity_id),
            banned_rights=rights,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restrict user: {e}")

    await _log_admin_action(db, conversation_id, "restrict", req.identity_id)
    return {"success": True}


@router.post("/{conversation_id}/approve")
async def approve_member(
    conversation_id: int,
    req: ApproveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Remove all restrictions from a user (unban/unrestrict)."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights

        # All-false rights = no restrictions
        rights = ChatBannedRights(until_date=None)
        await client(EditBannedRequest(
            channel=peer,
            participant=await _user_id(db, req.identity_id),
            banned_rights=rights,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to approve user: {e}")

    await _log_admin_action(db, conversation_id, "approve", req.identity_id)
    return {"success": True}


# ── Invite Links ──────────────────────────────────────────────────────────

@router.get("/{conversation_id}/invite-links")
async def get_invite_links(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get all invite links for a group/channel."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        from telethon.tl.functions.messages import GetExportedChatInvitesRequest
        me = await client.get_me()
        result = await client(GetExportedChatInvitesRequest(
            peer=peer,
            admin_id=me,
            limit=50,
            revoked=False,
        ))
        links = []
        for invite in result.invites:
            links.append({
                "link": invite.link,
                "title": getattr(invite, "title", None),
                "expire_date": iso_utc(invite.expire_date),
                "usage_limit": getattr(invite, "usage_limit", None),
                "usage": getattr(invite, "usage", 0) or 0,
                "request_needed": getattr(invite, "request_needed", False),
                "revoked": getattr(invite, "revoked", False),
            })
        return links
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get invite links: {e}")


@router.post("/{conversation_id}/invite-links")
async def create_invite_link(
    conversation_id: int,
    req: CreateInviteLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a new invite link."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        from telethon.tl.functions.messages import ExportChatInviteRequest

        expire = datetime.fromtimestamp(req.expire_date) if req.expire_date else None
        result = await client(ExportChatInviteRequest(
            peer=peer,
            expire_date=expire,
            usage_limit=req.usage_limit,
            title=req.title,
            request_needed=req.request_needed,
        ))
        link_data = {
            "link": result.link,
            "title": getattr(result, "title", None),
            "expire_date": iso_utc(result.expire_date),
            "usage_limit": getattr(result, "usage_limit", None),
            "usage": getattr(result, "usage", 0) or 0,
            "request_needed": getattr(result, "request_needed", False),
            "revoked": False,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create invite link: {e}")

    await _log_admin_action(db, conversation_id, "create_invite_link", details=link_data["link"])
    return link_data


@router.post("/{conversation_id}/invite-links/revoke")
async def revoke_invite_link(
    conversation_id: int,
    req: RevokeInviteLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke an existing invite link."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        from telethon.tl.functions.messages import EditExportedChatInviteRequest
        await client(EditExportedChatInviteRequest(
            peer=peer,
            link=req.link,
            revoked=True,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to revoke invite link: {e}")

    await _log_admin_action(db, conversation_id, "revoke_invite_link", details=req.link)
    return {"success": True}


# ── Group/Channel Settings ───────────────────────────────────────────────

@router.get("/{conversation_id}/settings")
async def get_group_settings(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the current title and about/description for a group or channel."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    try:
        full = await client.get_entity(peer)
        title = getattr(full, "title", "")
        # about requires full chat info
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest
        try:
            full_info = await client(GetFullChannelRequest(channel=peer))
            about = full_info.full_chat.about or ""
        except Exception:
            try:
                full_info = await client(GetFullChatRequest(chat_id=peer))
                about = full_info.full_chat.about or ""
            except Exception:
                about = ""

        return {
            "title": title,
            "about": about,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get settings: {e}")


@router.put("/{conversation_id}/settings")
async def update_group_settings(
    conversation_id: int,
    req: GroupSettingsRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update group/channel title and/or description."""
    peer = await _require_group(conversation_id, db)
    client = await tg_client.connected_client()

    changes = []
    try:
        if req.title is not None:
            from telethon.tl.functions.channels import EditTitleRequest
            await client(EditTitleRequest(channel=peer, title=req.title))
            changes.append("title")

        if req.about is not None:
            from telethon.tl.functions.messages import EditChatAboutRequest
            await client(EditChatAboutRequest(peer=peer, about=req.about))
            changes.append("about")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update settings: {e}")

    await _log_admin_action(db, conversation_id, "update_settings", details=", ".join(changes))
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
    rows = (await db.execute(
        select(AdminActionLog, Identity.person_id)
        .outerjoin(Identity, Identity.id == AdminActionLog.target_identity_id)
        .where(AdminActionLog.conversation_id == conversation_id)
        .order_by(AdminActionLog.created_at.desc())
        .limit(limit)
    )).all()
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
        for log, person_id in rows
    ]
