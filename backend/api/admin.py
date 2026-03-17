"""
Admin management endpoints for groups and channels.

Provides promote/demote, ban/restrict/approve, invite link management,
group settings, and admin action log.
"""

from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db import crud
from backend.telegram.client import get_client, ensure_connected

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
    user_id: int
    permissions: AdminPermissions


class DemoteRequest(BaseModel):
    user_id: int


class BanRequest(BaseModel):
    user_id: int


class RestrictRequest(BaseModel):
    user_id: int
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
    user_id: int


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

async def _require_group_chat(telegram_id: int, db: AsyncSession):
    """Validate that chat exists and is a group/channel."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if chat.type == "private":
        raise HTTPException(status_code=400, detail="Admin actions are only available for groups and channels")
    return chat


async def _require_client():
    """Return the connected Telegram client or raise 503."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()
    return client


async def _log_admin_action(
    db: AsyncSession,
    chat_telegram_id: int,
    action: str,
    target_user_id: Optional[int] = None,
    details: Optional[str] = None,
):
    """Insert an admin action log entry."""
    from sqlalchemy import text
    await db.execute(
        text(
            "INSERT INTO admin_action_log (chat_telegram_id, action, target_user_id, details, created_at) "
            "VALUES (:chat_id, :action, :target, :details, :ts)"
        ),
        {
            "chat_id": chat_telegram_id,
            "action": action,
            "target": target_user_id,
            "details": details,
            "ts": datetime.utcnow(),
        },
    )
    await db.commit()


# ── Promote / Demote ─────────────────────────────────────────────────────

@router.post("/{telegram_id}/promote")
async def promote_admin(
    telegram_id: int,
    req: PromoteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Promote a user to admin with granular permissions."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

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
            channel=telegram_id,
            user_id=req.user_id,
            admin_rights=rights,
            rank="Admin",
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to promote user: {e}")

    await _log_admin_action(db, telegram_id, "promote", req.user_id)
    return {"success": True}


@router.post("/{telegram_id}/demote")
async def demote_admin(
    telegram_id: int,
    req: DemoteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Demote an admin back to regular member."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

    try:
        from telethon.tl.functions.channels import EditAdminRequest
        from telethon.tl.types import ChatAdminRights

        # All-false rights effectively demotes
        rights = ChatAdminRights()
        await client(EditAdminRequest(
            channel=telegram_id,
            user_id=req.user_id,
            admin_rights=rights,
            rank="",
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to demote user: {e}")

    await _log_admin_action(db, telegram_id, "demote", req.user_id)
    return {"success": True}


# ── Ban / Restrict / Approve ─────────────────────────────────────────────

@router.post("/{telegram_id}/ban")
async def ban_member(
    telegram_id: int,
    req: BanRequest,
    db: AsyncSession = Depends(get_db),
):
    """Ban a user from the group/channel."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

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
            channel=telegram_id,
            participant=req.user_id,
            banned_rights=rights,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to ban user: {e}")

    await _log_admin_action(db, telegram_id, "ban", req.user_id)
    return {"success": True}


@router.post("/{telegram_id}/restrict")
async def restrict_member(
    telegram_id: int,
    req: RestrictRequest,
    db: AsyncSession = Depends(get_db),
):
    """Restrict a member's permissions."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

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
            channel=telegram_id,
            participant=req.user_id,
            banned_rights=rights,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to restrict user: {e}")

    await _log_admin_action(db, telegram_id, "restrict", req.user_id)
    return {"success": True}


@router.post("/{telegram_id}/approve")
async def approve_member(
    telegram_id: int,
    req: ApproveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Remove all restrictions from a user (unban/unrestrict)."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

    try:
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import ChatBannedRights

        # All-false rights = no restrictions
        rights = ChatBannedRights(until_date=None)
        await client(EditBannedRequest(
            channel=telegram_id,
            participant=req.user_id,
            banned_rights=rights,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to approve user: {e}")

    await _log_admin_action(db, telegram_id, "approve", req.user_id)
    return {"success": True}


# ── Invite Links ──────────────────────────────────────────────────────────

@router.get("/{telegram_id}/invite-links")
async def get_invite_links(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get all invite links for a group/channel."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

    try:
        from telethon.tl.functions.messages import GetExportedChatInvitesRequest
        me = await client.get_me()
        result = await client(GetExportedChatInvitesRequest(
            peer=telegram_id,
            admin_id=me,
            limit=50,
            revoked=False,
        ))
        links = []
        for invite in result.invites:
            links.append({
                "link": invite.link,
                "title": getattr(invite, "title", None),
                "expire_date": invite.expire_date.isoformat() if invite.expire_date else None,
                "usage_limit": getattr(invite, "usage_limit", None),
                "usage": getattr(invite, "usage", 0) or 0,
                "request_needed": getattr(invite, "request_needed", False),
                "revoked": getattr(invite, "revoked", False),
            })
        return links
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get invite links: {e}")


@router.post("/{telegram_id}/invite-links")
async def create_invite_link(
    telegram_id: int,
    req: CreateInviteLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a new invite link."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

    try:
        from telethon.tl.functions.messages import ExportChatInviteRequest

        expire = datetime.fromtimestamp(req.expire_date) if req.expire_date else None
        result = await client(ExportChatInviteRequest(
            peer=telegram_id,
            expire_date=expire,
            usage_limit=req.usage_limit,
            title=req.title,
            request_needed=req.request_needed,
        ))
        link_data = {
            "link": result.link,
            "title": getattr(result, "title", None),
            "expire_date": result.expire_date.isoformat() if result.expire_date else None,
            "usage_limit": getattr(result, "usage_limit", None),
            "usage": getattr(result, "usage", 0) or 0,
            "request_needed": getattr(result, "request_needed", False),
            "revoked": False,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create invite link: {e}")

    await _log_admin_action(db, telegram_id, "create_invite_link", details=link_data["link"])
    return link_data


@router.post("/{telegram_id}/invite-links/revoke")
async def revoke_invite_link(
    telegram_id: int,
    req: RevokeInviteLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    """Revoke an existing invite link."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

    try:
        from telethon.tl.functions.messages import EditExportedChatInviteRequest
        await client(EditExportedChatInviteRequest(
            peer=telegram_id,
            link=req.link,
            revoked=True,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to revoke invite link: {e}")

    await _log_admin_action(db, telegram_id, "revoke_invite_link", details=req.link)
    return {"success": True}


# ── Group/Channel Settings ───────────────────────────────────────────────

@router.get("/{telegram_id}/settings")
async def get_group_settings(
    telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get the current title and about/description for a group or channel."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

    try:
        full = await client.get_entity(telegram_id)
        title = getattr(full, "title", "")
        # about requires full chat info
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest
        try:
            full_info = await client(GetFullChannelRequest(channel=telegram_id))
            about = full_info.full_chat.about or ""
        except Exception:
            try:
                full_info = await client(GetFullChatRequest(chat_id=telegram_id))
                about = full_info.full_chat.about or ""
            except Exception:
                about = ""

        return {
            "title": title,
            "about": about,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get settings: {e}")


@router.put("/{telegram_id}/settings")
async def update_group_settings(
    telegram_id: int,
    req: GroupSettingsRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update group/channel title and/or description."""
    await _require_group_chat(telegram_id, db)
    client = await _require_client()

    changes = []
    try:
        if req.title is not None:
            from telethon.tl.functions.channels import EditTitleRequest
            await client(EditTitleRequest(channel=telegram_id, title=req.title))
            changes.append("title")

        if req.about is not None:
            from telethon.tl.functions.channels import EditPhotoRequest
            from telethon.tl.functions.messages import EditChatAboutRequest
            await client(EditChatAboutRequest(peer=telegram_id, about=req.about))
            changes.append("about")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update settings: {e}")

    await _log_admin_action(db, telegram_id, "update_settings", details=", ".join(changes))
    return {"success": True, "updated": changes}


# ── Admin Action Log ─────────────────────────────────────────────────────

@router.get("/{telegram_id}/action-log")
async def get_admin_action_log(
    telegram_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    """Get recent admin actions for a group/channel."""
    await _require_group_chat(telegram_id, db)

    from sqlalchemy import text
    result = await db.execute(
        text(
            "SELECT id, chat_telegram_id, action, target_user_id, details, created_at "
            "FROM admin_action_log "
            "WHERE chat_telegram_id = :chat_id "
            "ORDER BY created_at DESC LIMIT :lim"
        ),
        {"chat_id": telegram_id, "lim": limit},
    )
    rows = result.fetchall()
    return [
        {
            "id": r[0],
            "chat_telegram_id": r[1],
            "action": r[2],
            "target_user_id": r[3],
            "details": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
        for r in rows
    ]
