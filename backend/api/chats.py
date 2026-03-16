import json
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import utils
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter

from backend.config import DATA_DIR
from backend.db.database import get_db
from backend.db import crud
from backend.telegram.client import get_client, ensure_connected
from backend.telegram.sync import sync_messages, bulk_sync, _chat_type

router = APIRouter()


class MonitorRequest(BaseModel):
    monitored: bool


class HideRequest(BaseModel):
    hidden: bool


class BriefingRequest(BaseModel):
    briefing: bool


class TrackRequest(BaseModel):
    tracked: bool


@router.get("/folders")
async def list_folders():
    client = get_client()
    if not client:
        return []
    response = await client(GetDialogFiltersRequest())
    filters = response.filters if hasattr(response, 'filters') else response
    result = []
    for f in filters:
        if not isinstance(f, DialogFilter):
            continue  # skip DialogFilterDefault / DialogFilterChatlist
        chat_ids = []
        for peer in f.include_peers:
            try:
                chat_ids.append(utils.get_peer_id(peer))
            except Exception:
                pass
        title = f.title.text if hasattr(f.title, 'text') else str(f.title)
        result.append({
            "id": f.id,
            "title": title,
            "chat_ids": chat_ids,
            "include_contacts": f.contacts,
            "include_non_contacts": f.non_contacts,
            "include_groups": f.groups,
            "include_channels": f.broadcasts,
            "include_bots": f.bots,
        })
    return result


def _serialize_msg(row) -> dict:
    m, reply_sender, reply_text, reply_media_type = row
    reply_preview = None
    if m.reply_to_telegram_id is not None:
        reply_preview = {
            "sender_name": reply_sender,
            "text": reply_text,
            "media_type": reply_media_type,
        }
    return {
        "id": m.id,
        "telegram_id": m.telegram_id,
        "chat_id": m.chat_id,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "text": m.text,
        "date": m.date,
        "is_outgoing": m.is_outgoing,
        "media_type": m.media_type,
        "media_url": f"/api/media/{m.media_path}" if m.media_path else None,
        "text_html": m.text_html,
        "reactions": json.loads(m.reactions_json) if m.reactions_json else None,
        "importance_score": m.importance_score,
        "user_flagged": m.user_flagged,
        "reply_to_telegram_id": m.reply_to_telegram_id,
        "reply_to_preview": reply_preview,
        "my_reaction": m.my_reaction,
    }


def _chat_dict(c, last_text=None, last_sender=None, last_date=None) -> dict:
    # Use username as display name when name is just a numeric telegram ID
    name = c.name
    if name and name.isdigit() and c.username:
        name = c.username
    d = {
        "id": c.id,
        "telegram_id": c.telegram_id,
        "name": name,
        "type": c.type,
        "username": c.username,
        "is_monitored": c.is_monitored,
        "is_hidden": c.is_hidden,
        "is_briefing": c.is_briefing,
        "is_tracked": c.is_tracked,
        "unread_count": c.unread_count or 0,
        "last_synced_at": c.last_synced_at,
    }
    if last_text is not None or last_sender is not None or last_date is not None:
        d["last_message"] = {
            "text": (last_text[:100] if last_text and len(last_text) > 100 else last_text) or "",
            "sender_name": last_sender,
            "date": last_date.isoformat() if last_date else None,
        }
    else:
        d["last_message"] = None
    return d


@router.post("/bulk-sync")
async def bulk_sync_chats():
    synced = await bulk_sync(n_chats=50, messages_per_chat=30)
    return {"synced": synced}


@router.post("/{telegram_id}/ensure")
async def ensure_chat(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Ensure a chat exists locally and has synced messages.

    If the chat isn't in the DB, fetch it from Telegram and upsert it.
    If messages haven't been synced yet, sync them.
    Returns the chat dict.
    """
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)

    if not chat:
        # Chat not in local DB — fetch from Telegram and create it
        client = get_client()
        if not client:
            raise HTTPException(status_code=503, detail="Telegram client not available")
        await ensure_connected()
        try:
            entity = await client.get_entity(telegram_id)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Could not resolve Telegram entity: {e}")

        chat = await crud.upsert_chat(
            db,
            telegram_id=telegram_id,
            name=getattr(entity, "title", None)
                or f"{getattr(entity, 'first_name', '') or ''} {getattr(entity, 'last_name', '') or ''}".strip()
                or str(telegram_id),
            chat_type=_chat_type(entity),
            username=getattr(entity, "username", None),
        )

    # Sync messages if not synced yet
    if chat.last_synced_at is None:
        try:
            await sync_messages(telegram_id, limit=50)
        except ValueError:
            pass  # Entity not resolvable; return chat without messages

    # Re-fetch to get updated last_synced_at
    await db.refresh(chat)
    return _chat_dict(chat)


@router.get("")
async def list_chats(limit: int = 50, offset: int = 0, sort_by: str = "recent", db: AsyncSession = Depends(get_db)):
    rows = await crud.get_chats(db, limit=limit, offset=offset, sort_by=sort_by)
    return [_chat_dict(chat, last_text, last_sender, last_date) for chat, last_text, last_sender, last_date in rows]


@router.get("/hidden")
async def list_hidden_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_hidden_chats(db)]


@router.get("/tracked")
async def list_tracked_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_tracked_chats(db)]


@router.get("/briefing")
async def list_briefing_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_briefing_chats(db)]


@router.post("/{telegram_id}/monitor")
async def toggle_monitor(
    telegram_id: int,
    req: MonitorRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_monitored(db, telegram_id, req.monitored)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if req.monitored:
        try:
            await sync_messages(telegram_id)
        except ValueError:
            pass  # Entity not resolvable; monitoring is still toggled
    return {"telegram_id": telegram_id, "is_monitored": chat.is_monitored}


@router.post("/{telegram_id}/hide")
async def toggle_hide(
    telegram_id: int,
    req: HideRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_hidden(db, telegram_id, req.hidden)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"telegram_id": telegram_id, "is_hidden": chat.is_hidden}


@router.post("/{telegram_id}/briefing")
async def toggle_briefing(
    telegram_id: int,
    req: BriefingRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_briefing(db, telegram_id, req.briefing)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"telegram_id": telegram_id, "is_briefing": chat.is_briefing}


@router.post("/{telegram_id}/track")
async def toggle_track(
    telegram_id: int,
    req: TrackRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_tracked(db, telegram_id, req.tracked)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"telegram_id": telegram_id, "is_tracked": chat.is_tracked}


@router.post("/{telegram_id}/mark-read")
async def mark_chat_read(telegram_id: int, db: AsyncSession = Depends(get_db)):
    chat = await crud.mark_chat_read(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"success": True}


@router.get("/{telegram_id}/messages")
async def get_messages(
    telegram_id: int,
    limit: int = 100,
    offset: int = 0,
    anchor_telegram_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    # When anchoring to a specific message, compute offset so it lands near the middle
    if anchor_telegram_id is not None:
        offset = await crud.get_offset_for_anchor(db, telegram_id, anchor_telegram_id)

    # Auto-sync on first open (skip hidden chats)
    if offset == 0:
        chat = await crud.get_chat_by_telegram_id(db, telegram_id)
        if chat and chat.last_synced_at is None and not chat.is_hidden and not chat.is_briefing:
            try:
                await sync_messages(telegram_id, limit=100)
            except ValueError:
                pass  # Entity not resolvable; return whatever messages are in DB

    rows = await crud.get_messages(db, telegram_id, limit=limit, offset=offset)
    return [_serialize_msg(row) for row in reversed(rows)]


@router.get("/{telegram_id}/messages/search")
async def search_messages(
    telegram_id: int,
    q: str = "",
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    if not q.strip():
        return []
    rows = await crud.search_messages_in_chat(db, telegram_id, q.strip(), limit=limit)
    return [_serialize_msg(row) for row in reversed(rows)]


@router.get("/{telegram_id}/messages/date-offset")
async def get_date_offset(
    telegram_id: int,
    date: str,
    db: AsyncSession = Depends(get_db),
):
    target = datetime.fromisoformat(date)
    offset = await crud.get_date_offset(db, telegram_id, target)
    return {"offset": offset}


class ReactRequest(BaseModel):
    emoji: Optional[str] = None


@router.post("/{telegram_id}/messages/{msg_telegram_id}/react")
async def react_to_message(
    telegram_id: int,
    msg_telegram_id: int,
    req: ReactRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send or remove the current user's emoji reaction on a message via Telethon."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram not connected")

    try:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        reaction_list = [ReactionEmoji(emoticon=req.emoji)] if req.emoji else []
        await client(SendReactionRequest(
            peer=telegram_id,
            msg_id=msg_telegram_id,
            reaction=reaction_list,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send reaction: {e}")

    # Persist to local DB
    msg = await crud.set_my_reaction(db, telegram_id, msg_telegram_id, req.emoji)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    return {"success": True, "my_reaction": msg.my_reaction}


@router.post("/{telegram_id}/sync")
async def sync_chat(telegram_id: int, limit: int = 100, db: AsyncSession = Depends(get_db)):
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    try:
        await sync_messages(telegram_id, limit=min(limit, 200))
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Could not resolve chat entity. The user or chat may have been deleted. ({e})",
        )
    return {"status": "ok"}


@router.get("/{telegram_id}/members")
async def get_chat_members(telegram_id: int, limit: int = 100, db: AsyncSession = Depends(get_db)):
    """Fetch members of a group or channel via Telethon."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if chat.type == "private":
        return []

    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    from telethon.tl.types import User

    members = []
    try:
        async for user in client.iter_participants(telegram_id, limit=limit):
            if not isinstance(user, User):
                continue
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() or str(user.id)
            # Determine role from participant type
            role = "member"
            participant = getattr(user, "participant", None)
            if participant:
                cls_name = type(participant).__name__
                if "Creator" in cls_name:
                    role = "creator"
                elif "Admin" in cls_name:
                    role = "admin"

            members.append({
                "id": user.id,
                "name": name,
                "username": user.username,
                "is_bot": user.bot,
                "role": role,
            })
    except Exception as e:
        # Some chats don't allow participant listing
        raise HTTPException(status_code=403, detail=f"Cannot fetch members: {e}")

    # Sort: creator first, then admins, then members
    role_order = {"creator": 0, "admin": 1, "member": 2}
    members.sort(key=lambda m: (role_order.get(m["role"], 2), m["name"].lower()))
    return members
