import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import utils
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter

logger = logging.getLogger(__name__)

from backend.config import DATA_DIR
from backend.db.database import get_db
from backend.db import crud
from backend.telegram.client import get_client, ensure_connected
from backend.telegram.sync import sync_messages, bulk_sync, _chat_type, _extract_user_status, _is_deactivated

router = APIRouter()


class HideRequest(BaseModel):
    hidden: bool


class BriefingRequest(BaseModel):
    briefing: bool


class TrackRequest(BaseModel):
    tracked: bool


class PinRequest(BaseModel):
    pinned: bool


class ArchiveRequest(BaseModel):
    archived: bool


class MuteRequest(BaseModel):
    muted: bool
    duration: Optional[str] = None  # "1h", "8h", "1d", "forever", or None


class MarkUnreadRequest(BaseModel):
    marked_unread: bool


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
    if m.reply_to_tg_message_id is not None:
        reply_preview = {
            "sender_name": reply_sender,
            "text": reply_text,
            "media_type": reply_media_type,
        }
    return {
        "id": m.id,
        "tg_message_id": m.tg_message_id,
        "chat_id": m.chat_id,
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "text": m.text,
        "date": m.date.isoformat() + "Z" if m.date else None,
        "is_outgoing": m.is_outgoing,
        "media_type": m.media_type,
        "media_url": f"/api/media/{m.media_path}" if m.media_path else None,
        "text_html": m.text_html,
        "reactions": json.loads(m.reactions_json) if m.reactions_json else None,
        "importance_score": m.importance_score,
        "user_flagged": m.user_flagged,
        "reply_to_tg_message_id": m.reply_to_tg_message_id,
        "reply_to_preview": reply_preview,
        "my_reaction": m.my_reaction,
        "topic_id": m.topic_id if hasattr(m, 'topic_id') else None,
        "is_pinned": m.is_pinned if hasattr(m, 'is_pinned') else False,
        "media_filename": m.media_filename,
        "media_size": m.media_size,
        "grouped_id": m.grouped_id,
        "delivery_state": m.delivery_state or "sent",
        "is_edited": m.is_edited,
        "scam_score": getattr(m, 'scam_score', None),
        "scam_reason": getattr(m, 'scam_reason', None),
        "scam_manual_verdict": getattr(m, 'scam_manual_verdict', None),
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
        "is_forum": c.is_forum if hasattr(c, 'is_forum') else False,
        "slow_mode_seconds": c.slow_mode_seconds if hasattr(c, 'slow_mode_seconds') else 0,
        "is_pinned": c.is_pinned,
        "is_archived": c.is_archived,
        "is_muted": c.is_muted,
        "muted_until": c.muted_until.isoformat() if c.muted_until else None,
        "is_marked_unread": c.is_marked_unread,
        "unread_count": c.unread_count or 0,
        "last_synced_at": c.last_synced_at,
        "last_activity_at": getattr(c, 'last_activity_at', None),
    }
    if last_text is not None or last_sender is not None or last_date is not None:
        d["last_message"] = {
            "text": (last_text[:100] if last_text and len(last_text) > 100 else last_text) or "",
            "sender_name": last_sender,
            "date": last_date.isoformat() + "Z" if last_date else None,
        }
    else:
        d["last_message"] = None
    # Ensure last_activity_at is serialized as ISO string
    if d.get("last_activity_at") and hasattr(d["last_activity_at"], "isoformat"):
        d["last_activity_at"] = d["last_activity_at"].isoformat() + "Z"
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
            is_deactivated=_is_deactivated(entity),
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
async def list_chats(
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "recent",
    tag_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    rows = await crud.get_chats(db, limit=limit, offset=offset, sort_by=sort_by, tag_id=tag_id, workspace_id=workspace_id)
    return [_chat_dict(chat, last_text, last_sender, last_date) for chat, last_text, last_sender, last_date in rows]


class ChatsByIdsRequest(BaseModel):
    ids: List[int]


@router.post("/by-ids")
async def get_chats_by_ids(req: ChatsByIdsRequest, db: AsyncSession = Depends(get_db)):
    """Fetch chats by telegram_id — used to populate folder views whose members
    fall outside the paginated recent-chats window. Archived chats are included
    (Telegram folders legitimately contain them); hidden/briefing/deactivated are not.
    """
    ids = req.ids[:500]
    if not ids:
        return []
    from sqlalchemy import select as sa_sel
    from backend.db.models import Chat
    result = await db.execute(
        sa_sel(Chat).where(
            Chat.telegram_id.in_(ids),
            Chat.is_hidden == False,  # noqa: E712
            Chat.is_briefing == False,  # noqa: E712
            Chat.is_deactivated == False,  # noqa: E712
        )
    )
    return [_chat_dict(c) for c in result.scalars().all()]


@router.get("/search")
async def search_chats(q: str, db: AsyncSession = Depends(get_db)):
    """Search chats by name in local DB."""
    if not q.strip():
        return {"local": []}
    from sqlalchemy import select, desc
    from backend.db.models import Chat
    result = await db.execute(
        select(Chat)
        .where(
            Chat.name.ilike(f"%{q}%"),
            Chat.is_hidden == False,  # noqa: E712
            Chat.is_deactivated == False,  # noqa: E712
        )
        .order_by(desc(Chat.last_activity_at))
        .limit(20)
    )
    local = [_chat_dict(c) for c in result.scalars().all()]
    return {"local": local}


@router.get("/search/telegram")
async def search_chats_telegram(q: str, db: AsyncSession = Depends(get_db)):
    """Search chats via Telegram API (finds chats not yet in local DB)."""
    if not q.strip():
        return []
    client = get_client()
    if not client:
        # Fallback to local DB search if client unavailable
        from sqlalchemy import select as sa_sel
        from backend.db.models import Chat
        result = await db.execute(
            sa_sel(Chat)
            .where(Chat.name.ilike(f"%{q}%"))
            .order_by(Chat.last_activity_at.desc().nullslast())
            .limit(20)
        )
        return [_chat_dict(c) for c in result.scalars().all()]

    try:
        from telethon.tl.functions.contacts import SearchRequest
        chats_found = []
        seen_ids = set()

        # 1. Search messages globally — finds chats including contact requests
        #    that don't appear in regular dialog lists
        try:
            from telethon.tl.functions.messages import SearchGlobalRequest
            from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty, PeerUser, PeerChat, PeerChannel
            global_result = await client(SearchGlobalRequest(
                q=q,
                filter=InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None,
                offset_rate=0,
                offset_peer=InputPeerEmpty(),
                offset_id=0,
                limit=20,
            ))
            user_map = {u.id: u for u in getattr(global_result, 'users', [])}
            chat_map = {c.id: c for c in getattr(global_result, 'chats', [])}
            for msg in getattr(global_result, 'messages', []):
                peer_id = getattr(msg, 'peer_id', None)
                if not peer_id:
                    continue
                if isinstance(peer_id, PeerUser):
                    tid = peer_id.user_id
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    user = user_map.get(tid)
                    name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip() if user else str(tid)
                    chats_found.append({
                        "id": 0, "telegram_id": tid, "name": name, "type": "private",
                        "username": getattr(user, "username", None) if user else None,
                        "is_monitored": False, "is_hidden": False, "is_briefing": False,
                        "is_tracked": False, "is_pinned": False, "is_archived": False,
                        "is_muted": False, "muted_until": None, "is_marked_unread": False,
                        "last_synced_at": None, "last_activity_at": None, "last_message": None,
                    })
                elif isinstance(peer_id, (PeerChat, PeerChannel)):
                    raw = peer_id.chat_id if isinstance(peer_id, PeerChat) else peer_id.channel_id
                    tid = utils.get_peer_id(peer_id)  # marked id: -X (group) / -100X (channel)
                    if tid in seen_ids:
                        continue
                    seen_ids.add(tid)
                    chat_ent = chat_map.get(raw)
                    ctype = "channel" if getattr(chat_ent, "broadcast", False) else "group"
                    chats_found.append({
                        "id": 0, "telegram_id": tid,
                        "name": getattr(chat_ent, "title", str(tid)) if chat_ent else str(tid),
                        "type": ctype,
                        "username": getattr(chat_ent, "username", None) if chat_ent else None,
                        "is_monitored": False, "is_hidden": False, "is_briefing": False,
                        "is_tracked": False, "is_pinned": False, "is_archived": False,
                        "is_muted": False, "muted_until": None, "is_marked_unread": False,
                        "last_synced_at": None, "last_activity_at": None, "last_message": None,
                    })
        except Exception as e:
            logger.warning("Global message search failed: %s", e)

        # 2. Also search dialogs across all folders (main, archived, custom filters)
        from backend.telegram.sync import _chat_type
        q_lower = q.lower()

        folder_ids = [None, 1]  # None = main, 1 = archived/contact requests

        for folder in folder_ids:
            try:
                async for dialog in client.iter_dialogs(limit=200, folder=folder):
                    if q_lower in (dialog.name or "").lower():
                        if dialog.id in seen_ids:
                            continue
                        seen_ids.add(dialog.id)
                        entity = dialog.entity
                        chats_found.append({
                            "id": 0, "telegram_id": dialog.id,
                            "name": dialog.name or str(dialog.id),
                            "type": _chat_type(entity),
                            "username": getattr(entity, "username", None),
                            "is_monitored": False, "is_hidden": False, "is_briefing": False,
                            "is_tracked": False, "is_pinned": False, "is_archived": False,
                            "is_muted": False, "muted_until": None, "is_marked_unread": False,
                            "unread_count": dialog.unread_count,
                            "last_synced_at": None,
                            "last_activity_at": getattr(dialog, "date", None).isoformat() if getattr(dialog, "date", None) else None,
                            "last_message": None,
                        })
                        if len(chats_found) >= 20:
                            break
            except Exception as e:
                logger.warning("Dialog search folder %s failed: %s", folder, e)

        # 2. Also search Telegram's global contacts/channels directory
        result = await client(SearchRequest(q=q, limit=20))
        # Process users
        for user in result.users:
            if user.id in seen_ids:
                continue
            seen_ids.add(user.id)
            name = f"{getattr(user, 'first_name', '') or ''} {getattr(user, 'last_name', '') or ''}".strip() or str(user.id)
            chats_found.append({
                "id": 0,
                "telegram_id": user.id,
                "name": name,
                "type": "private",
                "username": getattr(user, "username", None),
                "is_monitored": False,
                "is_hidden": False,
                "is_briefing": False,
                "is_tracked": False,
                "is_pinned": False,
                "is_archived": False,
                "is_muted": False,
                "muted_until": None,
                "is_marked_unread": False,
                "last_synced_at": None,
                "last_activity_at": None,
                "last_message": None,
            })
        # Process chats/channels
        for chat in result.chats:
            tid = utils.get_peer_id(chat)  # marked id, matches DB convention
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            chat_type = "channel" if getattr(chat, "broadcast", False) else "group"
            chats_found.append({
                "id": 0,
                "telegram_id": tid,
                "name": getattr(chat, "title", str(chat.id)),
                "type": chat_type,
                "username": getattr(chat, "username", None),
                "is_monitored": False,
                "is_hidden": False,
                "is_briefing": False,
                "is_tracked": False,
                "is_pinned": False,
                "is_archived": False,
                "is_muted": False,
                "muted_until": None,
                "is_marked_unread": False,
                "last_synced_at": None,
                "last_activity_at": None,
                "last_message": None,
            })
        return chats_found
    except Exception as e:
        logger.warning("Telegram search failed: %s", e, exc_info=True)
        # Fallback to local DB search
        from sqlalchemy import select as sa_sel
        from backend.db.models import Chat
        result = await db.execute(
            sa_sel(Chat)
            .where(Chat.name.ilike(f"%{q}%"))
            .order_by(Chat.last_activity_at.desc().nullslast())
            .limit(20)
        )
        return [_chat_dict(c) for c in result.scalars().all()]


@router.get("/search/messages")
async def search_messages_global(q: str, db: AsyncSession = Depends(get_db)):
    """Search message content globally via Telegram SearchGlobalRequest."""
    if not q.strip():
        return []
    client = get_client()
    if not client:
        return []
    try:
        from telethon.tl.functions.messages import SearchGlobalRequest
        from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

        result = await client(SearchGlobalRequest(
            q=q,
            filter=InputMessagesFilterEmpty(),
            min_date=None,
            max_date=None,
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=0,
            limit=30,
        ))

        # Group results by chat
        chat_messages: dict[int, list] = {}
        chats_map: dict[int, dict] = {}

        for c in getattr(result, "chats", []):
            chats_map[c.id] = {"name": getattr(c, "title", str(c.id)), "type": "channel" if hasattr(c, "broadcast") else "group"}
        for u in getattr(result, "users", []):
            name = f"{getattr(u, 'first_name', '') or ''} {getattr(u, 'last_name', '') or ''}".strip() or str(u.id)
            chats_map[u.id] = {"name": name, "type": "private"}

        for msg in getattr(result, "messages", []):
            peer = msg.peer_id
            if not peer:
                continue
            tg_id = utils.get_peer_id(peer)

            if tg_id not in chat_messages:
                chat_messages[tg_id] = []
            if len(chat_messages[tg_id]) < 3:  # max 3 messages per chat
                sender_name = None
                if msg.from_id and hasattr(msg.from_id, "user_id"):
                    u = chats_map.get(msg.from_id.user_id)
                    sender_name = u["name"] if u else None
                chat_messages[tg_id].append({
                    "tg_message_id": msg.id,
                    "text": (msg.message or "")[:120],
                    "sender_name": sender_name,
                    "date": msg.date.replace(tzinfo=None).isoformat() if msg.date else None,
                })

        results = []
        for tg_id, messages in chat_messages.items():
            info = chats_map.get(utils.resolve_id(tg_id)[0], {})
            # Try local DB for richer chat info
            local_chat = await crud.get_chat_by_telegram_id(db, tg_id)
            results.append({
                "telegram_id": tg_id,
                "name": local_chat.name if local_chat else info.get("name", str(tg_id)),
                "type": local_chat.type if local_chat else info.get("type", "unknown"),
                "messages": messages,
            })
        return results
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Global message search failed: %s", e)
        return []


@router.get("/hidden")
async def list_hidden_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_hidden_chats(db)]


@router.get("/tracked")
async def list_tracked_chats(db: AsyncSession = Depends(get_db)):
    rows = await crud.get_tracked_chats(db)
    return [_chat_dict(chat, last_text, last_sender, last_date) for chat, last_text, last_sender, last_date in rows]


@router.get("/briefing")
async def list_briefing_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_briefing_chats(db)]


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


@router.post("/{telegram_id}/mark-unread")
async def mark_chat_unread(
    telegram_id: int,
    req: MarkUnreadRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_marked_unread(db, telegram_id, req.marked_unread)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    return {"telegram_id": telegram_id, "is_marked_unread": chat.is_marked_unread}


@router.post("/{telegram_id}/pin")
async def toggle_pin(
    telegram_id: int,
    req: PinRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_pinned(db, telegram_id, req.pinned)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Sync to Telegram (best-effort)
    client = get_client()
    if client:
        try:
            await ensure_connected()
            from telethon.tl.functions.messages import ToggleDialogPinRequest
            from telethon.tl.types import InputDialogPeer, InputPeerUser, InputPeerChat, InputPeerChannel
            entity = await client.get_input_entity(telegram_id)
            await client(ToggleDialogPinRequest(
                peer=InputDialogPeer(peer=entity),
                pinned=req.pinned,
            ))
        except Exception as e:
            logger.warning("Failed to sync pin to Telegram: %s", e)

    return {"telegram_id": telegram_id, "is_pinned": chat.is_pinned}


@router.post("/{telegram_id}/archive")
async def toggle_archive(
    telegram_id: int,
    req: ArchiveRequest,
    db: AsyncSession = Depends(get_db),
):
    chat = await crud.set_archived(db, telegram_id, req.archived)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Sync to Telegram (best-effort)
    client = get_client()
    if client:
        try:
            await ensure_connected()
            from telethon.tl.functions.folders import EditPeerFoldersRequest
            from telethon.tl.types import InputFolderPeer
            entity = await client.get_input_entity(telegram_id)
            folder_id = 1 if req.archived else 0  # 1 = Archive folder
            await client(EditPeerFoldersRequest(
                folder_peers=[InputFolderPeer(peer=entity, folder_id=folder_id)]
            ))
        except Exception as e:
            logger.warning("Failed to sync archive to Telegram: %s", e)

    return {"telegram_id": telegram_id, "is_archived": chat.is_archived}


@router.get("/archived")
async def list_archived_chats(db: AsyncSession = Depends(get_db)):
    return [_chat_dict(c) for c in await crud.get_archived_chats(db)]


@router.post("/{telegram_id}/mute")
async def toggle_mute(
    telegram_id: int,
    req: MuteRequest,
    db: AsyncSession = Depends(get_db),
):
    muted_until = None
    if req.muted and req.duration:
        duration_map = {
            "1h": timedelta(hours=1),
            "8h": timedelta(hours=8),
            "1d": timedelta(days=1),
        }
        delta = duration_map.get(req.duration)
        if delta:
            muted_until = datetime.utcnow() + delta
        # "forever" leaves muted_until as None (muted indefinitely)

    chat = await crud.set_muted(db, telegram_id, req.muted, muted_until)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    # Sync to Telegram (best-effort)
    client = get_client()
    if client:
        try:
            await ensure_connected()
            from telethon.tl.functions.account import UpdateNotifySettingsRequest
            from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
            entity = await client.get_input_entity(telegram_id)
            if req.muted:
                # Mute: set mute_until to a far future date or the specific time
                mute_until_ts = int((muted_until or datetime(2038, 1, 1)).timestamp())
                settings = InputPeerNotifySettings(mute_until=mute_until_ts)
            else:
                settings = InputPeerNotifySettings(mute_until=0)
            await client(UpdateNotifySettingsRequest(
                peer=InputNotifyPeer(peer=entity),
                settings=settings,
            ))
        except Exception as e:
            logger.warning("Failed to sync mute to Telegram: %s", e)

    return {
        "telegram_id": telegram_id,
        "is_muted": chat.is_muted,
        "muted_until": chat.muted_until.isoformat() if chat.muted_until else None,
    }


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

    # Auto-sync on first open — non-blocking (skip hidden chats)
    if offset == 0:
        chat = await crud.get_chat_by_telegram_id(db, telegram_id)
        if chat and chat.last_synced_at is None and not chat.is_hidden and not chat.is_briefing:
            import asyncio

            async def _bg_sync():
                from backend.ws.manager import ws_manager
                try:
                    await sync_messages(telegram_id, limit=100)
                    # If very few messages were saved (chat full of service messages),
                    # try a deeper fetch
                    from sqlalchemy import func as sa_func, select as sa_select
                    async with AsyncSessionLocal() as db2:
                        count = (await db2.execute(
                            sa_select(sa_func.count()).select_from(Message).where(Message.chat_id == telegram_id)
                        )).scalar() or 0
                    if count < 5:
                        await sync_messages(telegram_id, limit=500)
                        async with AsyncSessionLocal() as db3:
                            count = (await db3.execute(
                                sa_select(sa_func.count()).select_from(Message).where(Message.chat_id == telegram_id)
                            )).scalar() or 0
                    if count == 0:
                        # Nothing retrievable (migrated/inaccessible chat) — tell the UI
                        await ws_manager.broadcast({"type": "chat_sync_empty", "chat_id": telegram_id})
                    else:
                        await ws_manager.broadcast({"type": "chat_synced", "chat_id": telegram_id})
                except Exception:
                    logging.getLogger(__name__).warning("auto-sync failed for chat %s", telegram_id, exc_info=True)
                    try:
                        await ws_manager.broadcast({"type": "chat_sync_empty", "chat_id": telegram_id})
                    except Exception:
                        pass

            asyncio.create_task(_bg_sync())

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


@router.post("/{telegram_id}/messages/{msg_tg_id}/react")
async def react_to_message(
    telegram_id: int,
    msg_tg_id: int,
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
            msg_id=msg_tg_id,
            reaction=reaction_list,
        ))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send reaction: {e}")

    # Persist to local DB
    msg = await crud.set_my_reaction(db, telegram_id, msg_tg_id, req.emoji)
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
    from backend.ws.manager import ws_manager
    await ws_manager.broadcast({"type": "chat_synced", "chat_id": telegram_id})
    return {"status": "ok"}


@router.get("/{telegram_id}/shared-media")
async def get_shared_media(
    telegram_id: int,
    media_filter: str = "media",
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List shared media items for a chat.

    media_filter values:
    - 'media': photos, videos, GIFs
    - 'files': documents
    - 'links': messages containing URLs
    """
    from backend.db.models import Message
    from sqlalchemy import select, desc, or_

    query = select(Message).where(Message.chat_id == telegram_id)

    if media_filter == "media":
        query = query.where(
            or_(
                Message.media_type == "photo",
                Message.media_type == "video",
                Message.media_type == "gif",
            )
        )
    elif media_filter == "files":
        query = query.where(Message.media_type == "document")
    elif media_filter == "links":
        query = query.where(
            Message.text.isnot(None),
            or_(
                Message.text.contains("http://"),
                Message.text.contains("https://"),
            ),
        )
    else:
        return []

    query = query.order_by(desc(Message.date)).limit(limit).offset(offset)
    result = await db.execute(query)
    messages = result.scalars().all()

    items = []
    for m in messages:
        items.append({
            "id": m.id,
            "tg_message_id": m.tg_message_id,
            "date": m.date.isoformat() if m.date else None,
            "sender_name": m.sender_name,
            "media_type": m.media_type,
            "media_url": f"/api/media/{m.media_path}" if m.media_path else None,
            "media_filename": m.media_filename,
            "media_size": m.media_size,
            "text": m.text,
        })
    return items


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


@router.get("/{telegram_id}/presence")
async def get_user_presence(telegram_id: int):
    """Get online/last-seen status for a private chat user via Telethon."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types import (
            UserStatusOnline,
            UserStatusOffline,
            UserStatusRecently,
            UserStatusLastWeek,
            UserStatusLastMonth,
        )

        full = await client(GetFullUserRequest(telegram_id))
        user = full.users[0] if full.users else None
        if not user:
            return {"status": "unknown"}

        user_status = getattr(user, "status", None)
        if isinstance(user_status, UserStatusOnline):
            return {"status": "online"}
        if isinstance(user_status, UserStatusOffline):
            last_seen = user_status.was_online.isoformat() if user_status.was_online else None
            return {"status": "offline", "last_seen": last_seen}
        if isinstance(user_status, UserStatusRecently):
            return {"status": "recently"}
        if isinstance(user_status, UserStatusLastWeek):
            return {"status": "last_week"}
        if isinstance(user_status, UserStatusLastMonth):
            return {"status": "last_month"}
        return {"status": "unknown"}
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


@router.post("/{telegram_id}/typing")
async def send_typing_action(telegram_id: int):
    """Send a typing indicator to the given chat via Telethon."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        from telethon.tl.functions.messages import SetTypingRequest
        from telethon.tl.types import SendMessageTypingAction

        await client(SetTypingRequest(
            peer=telegram_id,
            action=SendMessageTypingAction(),
        ))
        return {"success": True}
    except Exception as e:
        # Non-critical — don't fail the request
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Forum topics
# ---------------------------------------------------------------------------

@router.get("/{telegram_id}/topics")
async def list_topics(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """List forum topics for a supergroup with forum mode enabled."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        from telethon.tl.functions.channels import GetForumTopicsRequest
        result = await client(GetForumTopicsRequest(
            channel=telegram_id,
            offset_date=0,
            offset_id=0,
            offset_topic=0,
            limit=100,
            q=None,
        ))
        topics = []
        for topic in result.topics:
            topics.append({
                "id": topic.id,
                "title": topic.title,
                "icon_color": getattr(topic, "icon_color", None),
                "icon_emoji_id": getattr(topic, "icon_emoji_id", None),
                "date": topic.date.replace(tzinfo=None).isoformat() if topic.date else None,
                "is_closed": getattr(topic, "closed", False) or False,
                "is_pinned": getattr(topic, "pinned", False) or False,
            })
        return topics
    except (ImportError, Exception) as exc:
        # GetForumTopicsRequest not available or call failed;
        # fall back to deriving topics from synced messages
        if not isinstance(exc, ImportError):
            logger.warning("GetForumTopicsRequest failed, using fallback: %s", exc)
        from sqlalchemy import select as sa_sel, distinct
        from backend.db.models import Message
        rows = (await db.execute(
            sa_sel(distinct(Message.topic_id))
            .where(Message.chat_id == telegram_id, Message.topic_id.isnot(None))
        )).fetchall()
        topics = []
        for (tid,) in rows:
            first_msg = (await db.execute(
                sa_sel(Message)
                .where(Message.chat_id == telegram_id, Message.topic_id == tid)
                .order_by(Message.date)
                .limit(1)
            )).scalars().first()
            topics.append({
                "id": tid,
                "title": f"Topic {tid}",
                "icon_color": None,
                "icon_emoji_id": None,
                "date": first_msg.date.isoformat() if first_msg and first_msg.date else None,
                "is_closed": False,
                "is_pinned": False,
            })
        return topics


class CreateTopicRequest(BaseModel):
    title: str


@router.post("/{telegram_id}/topics")
async def create_topic(telegram_id: int, req: CreateTopicRequest, db: AsyncSession = Depends(get_db)):
    """Create a new forum topic in a supergroup."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        from telethon.tl.functions.channels import CreateForumTopicRequest
        result = await client(CreateForumTopicRequest(
            channel=telegram_id,
            title=req.title,
            random_id=int(datetime.utcnow().timestamp() * 1000),
        ))
        # The result contains the created topic updates
        updates = getattr(result, "updates", [])
        topic_id = None
        for upd in updates:
            msg = getattr(upd, "message", None)
            if msg:
                topic_id = getattr(msg, "id", None)
                break
        return {"success": True, "topic_id": topic_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot create topic: {e}")


class TopicStateRequest(BaseModel):
    closed: bool


@router.post("/{telegram_id}/topics/{topic_id}/state")
async def set_topic_state(telegram_id: int, topic_id: int, req: TopicStateRequest, db: AsyncSession = Depends(get_db)):
    """Close or reopen a forum topic."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        from telethon.tl.functions.channels import EditForumTopicRequest
        await client(EditForumTopicRequest(
            channel=telegram_id,
            topic_id=topic_id,
            closed=req.closed,
        ))
        return {"success": True, "topic_id": topic_id, "closed": req.closed}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot update topic state: {e}")


@router.get("/{telegram_id}/topics/{topic_id}/messages")
async def get_topic_messages(
    telegram_id: int,
    topic_id: int,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Get messages for a specific forum topic."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    rows = await crud.get_topic_messages(db, telegram_id, topic_id, limit=limit, offset=offset)
    return [_serialize_msg(row) for row in reversed(rows)]


# ---------------------------------------------------------------------------
# Slow mode
# ---------------------------------------------------------------------------

class SlowModeRequest(BaseModel):
    seconds: int  # 0 to disable, valid values: 0, 10, 30, 60, 300, 900, 3600


@router.post("/{telegram_id}/slow-mode")
async def set_slow_mode(telegram_id: int, req: SlowModeRequest, db: AsyncSession = Depends(get_db)):
    """Set slow mode delay for a group/supergroup."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        from telethon.tl.functions.channels import ToggleSlowModeRequest
        await client(ToggleSlowModeRequest(
            channel=telegram_id,
            seconds=req.seconds,
        ))
        # Update local DB
        chat.slow_mode_seconds = req.seconds
        await db.commit()
        return {"success": True, "slow_mode_seconds": req.seconds}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot set slow mode: {e}")


# ---------------------------------------------------------------------------
# Pin/unpin messages
# ---------------------------------------------------------------------------

class MessagePinRequest(BaseModel):
    pinned: bool
    notify: bool = False


@router.post("/{telegram_id}/messages/{msg_tg_id}/pin")
async def pin_message(
    telegram_id: int,
    msg_tg_id: int,
    req: MessagePinRequest,
    db: AsyncSession = Depends(get_db),
):
    """Pin or unpin a message in a chat."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        if req.pinned:
            await client.pin_message(telegram_id, msg_tg_id, notify=req.notify)
        else:
            await client.unpin_message(telegram_id, msg_tg_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot {'pin' if req.pinned else 'unpin'} message: {e}")

    # Update local DB
    msg = await crud.set_message_pinned(db, telegram_id, msg_tg_id, req.pinned)
    return {"success": True, "is_pinned": req.pinned}


@router.get("/{telegram_id}/pinned")
async def get_pinned_messages(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Get all pinned messages in a chat."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")

    rows = await crud.get_pinned_messages(db, telegram_id)
    return [_serialize_msg(row) for row in rows]


# ---------------------------------------------------------------------------
# Channel post stats
# ---------------------------------------------------------------------------

@router.get("/{telegram_id}/post-stats")
async def get_post_stats(telegram_id: int, limit: int = 20, db: AsyncSession = Depends(get_db)):
    """Get stats for recent channel posts (views, forwards, replies)."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if chat.type != "channel":
        raise HTTPException(status_code=400, detail="Post stats are only available for channels")

    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    posts = []
    try:
        async for msg in client.iter_messages(telegram_id, limit=limit):
            if not msg.text and not msg.media:
                continue
            posts.append({
                "telegram_id": msg.id,
                "text": (msg.text or "")[:200],
                "date": msg.date.replace(tzinfo=None).isoformat() if msg.date else None,
                "views": getattr(msg, "views", None) or 0,
                "forwards": getattr(msg, "forwards", None) or 0,
                "replies_count": getattr(msg.replies, "replies", 0) if getattr(msg, "replies", None) else 0,
            })
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot fetch post stats: {e}")

    return posts


# ---------------------------------------------------------------------------
# Create new chats / groups / channels and join via invite link
# ---------------------------------------------------------------------------


class StartPrivateChatRequest(BaseModel):
    telegram_id: int


class CreateGroupRequest(BaseModel):
    name: str
    member_ids: List[int]


class CreateChannelRequest(BaseModel):
    name: str
    about: str = ""
    is_public: bool = False


class JoinInviteLinkRequest(BaseModel):
    link: str


@router.post("/create/private")
async def start_private_chat(
    req: StartPrivateChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """Start or open a private chat with a user by their Telegram ID."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        entity = await client.get_entity(req.telegram_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Could not find user: {e}")

    telegram_id = utils.get_peer_id(entity)
    name = (
        getattr(entity, "title", None)
        or f"{getattr(entity, 'first_name', '') or ''} {getattr(entity, 'last_name', '') or ''}".strip()
        or str(telegram_id)
    )
    chat = await crud.upsert_chat(
        db,
        telegram_id=telegram_id,
        name=name,
        chat_type=_chat_type(entity),
        username=getattr(entity, "username", None),
    )
    return _chat_dict(chat)


@router.post("/create/group")
async def create_group(
    req: CreateGroupRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a new Telegram group with the given members."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    if not req.member_ids:
        raise HTTPException(status_code=400, detail="At least one member is required")

    try:
        from telethon.tl.functions.messages import CreateChatRequest

        users = []
        for uid in req.member_ids:
            try:
                user = await client.get_entity(uid)
                users.append(user)
            except Exception:
                pass  # skip unresolvable users
        if not users:
            raise HTTPException(status_code=400, detail="No valid users could be resolved")

        result = await client(CreateChatRequest(
            users=users,
            title=req.name,
        ))

        # Extract the created chat from the result
        created_chat = result.chats[0]
        telegram_id = utils.get_peer_id(created_chat)
        chat = await crud.upsert_chat(
            db,
            telegram_id=telegram_id,
            name=req.name,
            chat_type="group",
        )
        return _chat_dict(chat)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create group: {e}")


@router.post("/create/channel")
async def create_channel(
    req: CreateChannelRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create a new Telegram channel."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        from telethon.tl.functions.channels import CreateChannelRequest as TLCreateChannel

        result = await client(TLCreateChannel(
            title=req.name,
            about=req.about,
            megagroup=False,
            broadcast=True,
        ))

        created_channel = result.chats[0]
        telegram_id = utils.get_peer_id(created_channel)
        chat = await crud.upsert_chat(
            db,
            telegram_id=telegram_id,
            name=req.name,
            chat_type="channel",
        )
        return _chat_dict(chat)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create channel: {e}")


@router.post("/join")
async def join_via_invite_link(
    req: JoinInviteLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    """Join a chat/group/channel via an invite link (t.me/joinchat/... or t.me/+...)."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    link = req.link.strip()
    if not link:
        raise HTTPException(status_code=400, detail="Link is required")

    try:
        from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest

        # Extract the hash from the invite link
        invite_hash = None
        for prefix in ["https://t.me/joinchat/", "https://t.me/+", "t.me/joinchat/", "t.me/+"]:
            if link.startswith(prefix):
                invite_hash = link[len(prefix):]
                break

        if invite_hash:
            # Private invite link — join via hash
            result = await client(ImportChatInviteRequest(hash=invite_hash))
            joined_chat = result.chats[0]
        else:
            # Try as a public username (e.g. https://t.me/channel_name or @channel_name)
            username = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").lstrip("@")
            if not username:
                raise HTTPException(status_code=400, detail="Invalid invite link format")
            from telethon.tl.functions.channels import JoinChannelRequest
            entity = await client.get_entity(username)
            result = await client(JoinChannelRequest(entity))
            joined_chat = result.chats[0] if result.chats else entity

        telegram_id = utils.get_peer_id(joined_chat)
        name = getattr(joined_chat, "title", None) or str(telegram_id)
        chat = await crud.upsert_chat(
            db,
            telegram_id=telegram_id,
            name=name,
            chat_type=_chat_type(joined_chat),
            username=getattr(joined_chat, "username", None),
            is_deactivated=_is_deactivated(joined_chat),
        )
        return _chat_dict(chat)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to join: {e}")
