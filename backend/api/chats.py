import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import GetDialogFiltersRequest
from telethon.tl.types import DialogFilter

logger = logging.getLogger(__name__)

from backend.db.database import get_db, AsyncSessionLocal
from backend.db.models import Message
from backend.db import crud
from backend.jobs import get_running, run_single
from backend.scam.verdict import scam_fields
from backend.serialization import iso_utc
from backend.telegram.runtime import runtime
from backend.telegram.coordinator import coordinator
from backend.telegram.entities import chat_type as _chat_type, display_name, is_deactivated as _is_deactivated
from backend.telegram.sync import bulk_sync
from backend.ws.manager import ws_manager

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
    client = await runtime.client(require=False)
    if not client:
        return []
    try:
        response = await client(GetDialogFiltersRequest())
    except Exception as e:
        logger.warning("Failed to fetch dialog folders: %s", e)
        return []
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


def _parse_meta(raw: Optional[str]):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


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
        "date": iso_utc(m.date),
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
        "kind": getattr(m, "kind", None) or "message",
        "meta": _parse_meta(getattr(m, "meta_json", None)),
        "fwd_from_name": getattr(m, "fwd_from_name", None),
        **scam_fields(m),
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
        "is_bot": bool(getattr(c, "is_bot", False)),
        "is_deleted_account": bool(getattr(c, "is_deleted_account", False)),
        "history_complete": bool(getattr(c, "history_complete", False)),
        "is_muted": c.is_muted,
        "muted_until": iso_utc(c.muted_until),
        "is_marked_unread": c.is_marked_unread,
        "unread_count": c.unread_count or 0,
        "last_synced_at": iso_utc(c.last_synced_at),
        "last_activity_at": iso_utc(getattr(c, "last_activity_at", None)),
    }
    if last_text is not None or last_sender is not None or last_date is not None:
        d["last_message"] = {
            "text": (last_text[:100] if last_text and len(last_text) > 100 else last_text) or "",
            "sender_name": last_sender,
            "date": iso_utc(last_date),
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
        client = await runtime.client()
        try:
            entity = await client.get_entity(telegram_id)
        except Exception as e:
            raise HTTPException(status_code=404, detail=f"Could not resolve Telegram entity: {e}")

        chat = await crud.upsert_chat(
            db,
            telegram_id=telegram_id,
            name=display_name(entity, telegram_id),
            chat_type=_chat_type(entity),
            username=getattr(entity, "username", None),
            is_forum=bool(getattr(entity, "forum", False)),
            is_deactivated=_is_deactivated(entity),
        )

    # No synchronous history sync here — it made chat-open block for many
    # seconds. Returning immediately lets the pane open now; the first
    # GET /messages triggers the background auto-sync (with retry/FloodWait
    # handling) and the pane shows its "Syncing message history…" state.
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


def _remote_chat_dict(telegram_id: int, name: str, ctype: str, username: Optional[str]) -> dict:
    """Chat-shaped dict for a Telegram search hit that isn't stored locally."""
    return {
        "id": 0, "telegram_id": telegram_id, "name": name, "type": ctype, "username": username,
        "is_monitored": False, "is_hidden": False, "is_briefing": False,
        "is_tracked": False, "is_pinned": False, "is_archived": False,
        "is_muted": False, "muted_until": None, "is_marked_unread": False,
        "unread_count": 0, "last_synced_at": None, "last_activity_at": None, "last_message": None,
    }


@router.get("/search/telegram")
async def search_chats_telegram(q: str, db: AsyncSession = Depends(get_db)):
    """Find chats by name: the user's own dialogs are searched locally (every
    dialog is stored by dialog sync, including archived ones); Telegram is
    queried only for the global directory (people/groups/channels not yet in
    the dialog list). One RPC per query instead of four."""
    q = q.strip()
    if not q:
        return []
    from sqlalchemy import select as sa_sel, or_ as sa_or
    from backend.db.models import Chat

    pattern = f"%{q}%"
    result = await db.execute(
        sa_sel(Chat)
        .where(
            sa_or(Chat.name.ilike(pattern), Chat.username.ilike(pattern)),
            Chat.is_deactivated == False,  # noqa: E712
        )
        .order_by(Chat.last_activity_at.desc().nullslast())
        .limit(20)
    )
    found = [_chat_dict(c) for c in result.scalars().all()]
    seen = {c["telegram_id"] for c in found}

    if len(q) < 2:
        return found
    client = await runtime.client(require=False)
    # Typeahead must never queue behind a FloodWait: skip the directory lookup.
    if not client or not await coordinator.limiter.acquire(1, max_wait=2):
        return found
    try:
        from telethon.tl.functions.contacts import SearchRequest
        res = await client(SearchRequest(q=q, limit=20))
    except FloodWaitError as e:
        coordinator.limiter.flood(e.seconds)
        logger.info("Telegram directory search rate-limited (%ss)", e.seconds)
        return found
    except Exception as e:
        logger.warning("Telegram directory search failed: %s", e)
        return found
    for user in res.users:
        if user.id not in seen:
            seen.add(user.id)
            found.append(_remote_chat_dict(user.id, display_name(user, user.id), "private", getattr(user, "username", None)))
    for chat in res.chats:
        tid = utils.get_peer_id(chat)  # marked id, matches DB convention
        if tid not in seen:
            seen.add(tid)
            found.append(_remote_chat_dict(tid, display_name(chat, tid), _chat_type(chat), getattr(chat, "username", None)))
    return found


@router.get("/search/messages")
async def search_messages_global(q: str, db: AsyncSession = Depends(get_db)):
    """Search message content globally via Telegram SearchGlobalRequest."""
    if not q.strip():
        return []
    client = await runtime.client(require=False)
    if not client or not await coordinator.limiter.acquire(1, max_wait=2):
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

        # Keyed by marked peer id: raw user/chat/channel ids can collide.
        for c in getattr(result, "chats", []):
            chats_map[utils.get_peer_id(c)] = {"name": display_name(c, c.id), "type": _chat_type(c)}
        for u in getattr(result, "users", []):
            chats_map[u.id] = {"name": display_name(u, u.id), "type": "private"}

        for msg in getattr(result, "messages", []):
            peer = msg.peer_id
            if not peer:
                continue
            tg_id = utils.get_peer_id(peer)

            if tg_id not in chat_messages:
                chat_messages[tg_id] = []
            if len(chat_messages[tg_id]) < 3:  # max 3 messages per chat
                sender_name = None
                if msg.from_id:
                    u = chats_map.get(utils.get_peer_id(msg.from_id))
                    sender_name = u["name"] if u else None
                chat_messages[tg_id].append({
                    "tg_message_id": msg.id,
                    "text": (msg.message or "")[:120],
                    "sender_name": sender_name,
                    "date": iso_utc(msg.date),
                })

        results = []
        for tg_id, messages in chat_messages.items():
            info = chats_map.get(tg_id, {})
            # Try local DB for richer chat info
            local_chat = await crud.get_chat_by_telegram_id(db, tg_id)
            results.append({
                "telegram_id": tg_id,
                "name": local_chat.name if local_chat else info.get("name", str(tg_id)),
                "type": local_chat.type if local_chat else info.get("type", "unknown"),
                "messages": messages,
            })
        return results
    except FloodWaitError as e:
        coordinator.limiter.flood(e.seconds)
        logger.info("Global message search rate-limited (%ss)", e.seconds)
        return []
    except Exception as e:
        logger.warning("Global message search failed: %s", e)
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

    # Sync to Telegram (best-effort). Without this, the periodic dialog sync
    # restores Telegram's unread count and the badge resurrects within ~2 min.
    client = await runtime.client(require=False)
    if client:
        try:
            entity = await client.get_input_entity(telegram_id)
            await client.send_read_acknowledge(entity)
        except Exception as e:
            logger.warning("Failed to sync read state to Telegram: %s", e)

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
    client = await runtime.client(require=False)
    if client:
        try:
            from telethon.tl.functions.messages import ToggleDialogPinRequest
            from telethon.tl.types import InputDialogPeer
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
    client = await runtime.client(require=False)
    if client:
        try:
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


@router.post("/{telegram_id}/archive-history")
async def archive_chat_history(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Queue a full-history walk (or top-up) for one chat — DM, group or
    channel — ahead of the background DM archive. Distinct from
    ``/archive`` (the Telegram Archive folder toggle)."""
    from backend.telegram import archive

    if await crud.get_chat_by_telegram_id(db, telegram_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    await archive.request_chat(telegram_id, priority=True)
    cov = (await crud.get_coverage(db, [telegram_id])).get(telegram_id)
    return {
        "queued": True,
        "coverage": {
            "history_complete": bool(cov and cov.history_complete),
            "message_count": await crud.count_chat_messages(db, telegram_id),
            "oldest_tg_id": cov.oldest_tg_id if cov else None,
            "newest_contiguous_tg_id": cov.newest_contiguous_tg_id if cov else None,
        },
    }


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
    client = await runtime.client(require=False)
    if client:
        try:
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
        "muted_until": iso_utc(chat.muted_until),
    }


async def _local_message_count(chat_id: int) -> int:
    from sqlalchemy import func as sa_func, select as sa_select
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            sa_select(sa_func.count()).select_from(Message).where(Message.chat_id == chat_id)
        )).scalar() or 0


async def _auto_sync(telegram_id: int, first_sync: bool) -> None:
    """Background sync on chat open, with retry through transient failures.

    Emits exactly one of: ``chat_synced`` (something changed, or first sync
    finished with history), ``chat_sync_empty`` (a *completed* fetch found no
    retrievable history), ``chat_sync_failed`` (gave up / chat unavailable).
    """
    limit = 100 if first_sync else 50
    last = None
    for attempt in range(1, 5):
        res = await coordinator.sync_chat(telegram_id, limit)
        last = res
        changed = res.changed
        if res.completed and first_sync and not res.exhausted:
            if await _local_message_count(telegram_id) < 5:
                # Chat may be full of service messages — try a deeper fetch.
                # Only its own completion can prove the chat has no history.
                deep = await coordinator.sync_chat(telegram_id, 500)
                changed = changed or deep.changed
                res = last = deep
        if res.completed:
            if first_sync:
                count = await _local_message_count(telegram_id)
                if count == 0:
                    await ws_manager.broadcast({"type": "chat_sync_empty", "chat_id": telegram_id})
                    return
            if changed or first_sync:
                await ws_manager.broadcast({"type": "chat_synced", "chat_id": telegram_id})
            return
        if res.flood_wait:
            logger.info("auto-sync chat %s: FloodWait %ss (attempt %d)", telegram_id, res.flood_wait, attempt)
            await asyncio.sleep(min(res.flood_wait + 2, 600))
        elif res.status == "unavailable" and await runtime.client(require=False) is not None:
            break  # the chat itself can't be fetched (deleted, private, banned)
        else:
            await asyncio.sleep(3 * attempt)
    logger.warning("auto-sync chat %s gave up: %s", telegram_id, last.error if last else None)
    await ws_manager.broadcast({
        "type": "chat_sync_failed",
        "chat_id": telegram_id,
        "status": last.status if last else "error",
        "error": last.error if last else None,
    })


def _maybe_auto_sync(chat, latest: bool = True) -> bool:
    """Start a background sync when the chat was never synced (deep fetch,
    any view) or — for the latest view — its last sync is older than 15 min.
    Returns True when a sync is running."""
    if chat is None or chat.is_hidden or chat.is_deactivated:
        return False
    key = f"autosync:{chat.telegram_id}"
    if get_running(key) is not None:
        return True
    first_sync = chat.last_synced_at is None
    stale = not first_sync and chat.last_synced_at < datetime.utcnow() - timedelta(minutes=15)
    if first_sync or (stale and latest):
        run_single(key, lambda: _auto_sync(chat.telegram_id, first_sync))
        return True
    return coordinator.is_syncing(chat.telegram_id)


def _parse_cursor(value: Optional[str]):
    """Cursor = "<message.date as returned by the API>~<tg_message_id>"."""
    if value is None:
        return None
    try:
        date_part, id_part = value.rsplit("~", 1)
        dt = datetime.fromisoformat(date_part.rstrip("Z").replace(" ", "T"))
        return (dt.replace(tzinfo=None), int(id_part))
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid cursor: {value!r}")


def _cursor(msg: dict) -> str:
    return f"{msg['date']}~{msg['tg_message_id']}"


@router.get("/{telegram_id}/messages/window")
async def get_message_window(
    telegram_id: int,
    limit: int = 50,
    before: Optional[str] = None,
    after: Optional[str] = None,
    anchor: Optional[int] = None,
    date: Optional[str] = None,
    topic_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """Keyset message window (see crud.get_message_window).

    Exactly one positioning parameter: ``before``/``after`` (cursor strings),
    ``anchor`` (tg_message_id to centre on), ``date`` (ISO; centre on the first
    message at/after it) — or none for the newest messages, which also starts
    a background sync when the chat is stale.
    """
    around_date = None
    if date is not None:
        try:
            around_date = datetime.fromisoformat(date.rstrip("Z"))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date: {date!r}")
        if around_date.tzinfo is not None:
            from datetime import timezone
            around_date = around_date.astimezone(timezone.utc).replace(tzinfo=None)
    window = await crud.get_message_window(
        db, telegram_id,
        limit=limit,
        before=_parse_cursor(before),
        after=_parse_cursor(after),
        anchor_tg_id=anchor,
        around_date=around_date,
        topic_id=topic_id,
    )
    latest = before is None and after is None and anchor is None and date is None
    syncing = _maybe_auto_sync(await crud.get_chat_by_telegram_id(db, telegram_id), latest=latest)

    # Jump target not stored locally (or the date is older than local
    # history): fetch it plus a window around it from Telegram, then re-read.
    anchor_reason = None
    if anchor is not None or around_date is not None:
        rows = window["rows"]
        before_local_history = (
            around_date is not None and rows and not window["has_older"] and rows[0][0].date > around_date
        )
        if not window["anchor_found"] or before_local_history:
            res = await coordinator.fetch_around(
                telegram_id, anchor_id=anchor, date=around_date if anchor is None else None, limit=limit,
            )
            if res.completed:
                window = await crud.get_message_window(
                    db, telegram_id, limit=limit, anchor_tg_id=anchor, around_date=around_date, topic_id=topic_id,
                )
            if not window["anchor_found"]:
                if res.status == "not_found" or res.completed:
                    anchor_reason = "not_found"
                elif res.flood_wait or res.status == "skipped":
                    anchor_reason = "rate_limited"
                elif res.status == "unavailable":
                    anchor_reason = "unavailable"
                else:
                    anchor_reason = "error"
    messages = [_serialize_msg(row) for row in window["rows"]]
    return {
        "messages": messages,
        "has_older": window["has_older"],
        "has_newer": window["has_newer"],
        "older_cursor": _cursor(messages[0]) if messages else None,
        "newer_cursor": _cursor(messages[-1]) if messages else None,
        "anchor_found": window["anchor_found"],
        "anchor_reason": anchor_reason,
        "syncing": syncing,
    }


@router.post("/{telegram_id}/backfill")
async def backfill_history(
    telegram_id: int,
    limit: int = 100,
    topic_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """Fetch history older than the oldest locally stored message from
    Telegram. ``exhausted`` = Telegram has nothing older."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    oldest = await crud.get_oldest_message_id(db, telegram_id, topic_id)
    res = await coordinator.sync_chat(
        telegram_id, max(1, min(limit, 200)), offset_id=oldest or 0, topic_id=topic_id,
    )
    if res.flood_wait:
        raise HTTPException(
            status_code=429,
            detail=f"Telegram rate limit — retry in {res.flood_wait}s",
            headers={"Retry-After": str(res.flood_wait)},
        )
    if res.status == "unavailable":
        await runtime.client()  # 503 when Telegram itself is unreachable
        raise HTTPException(status_code=422, detail=res.error or "Chat unavailable")
    if res.status == "error":
        raise HTTPException(status_code=502, detail=f"Backfill failed: {res.error}")
    return {"added": res.added, "exhausted": res.exhausted, "status": res.status}


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
    client = await runtime.client()

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
    """Refresh the newest messages of a chat from Telegram.

    Returns the typed sync result: {status, fetched, added, updated, deleted,
    exhausted, flood_wait, error, chat_id}. ``chat_synced`` is broadcast only
    when something changed."""
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    res = await coordinator.sync_chat(telegram_id, max(1, min(limit, 200)))
    if res.flood_wait:
        raise HTTPException(
            status_code=429,
            detail=f"Telegram rate limit — retry in {res.flood_wait}s",
            headers={"Retry-After": str(res.flood_wait)},
        )
    if res.status == "unavailable":
        await runtime.client()  # 503 when Telegram itself is unreachable
        raise HTTPException(
            status_code=422,
            detail=f"Could not resolve chat entity. The user or chat may have been deleted. ({res.error})",
        )
    if res.status == "error":
        raise HTTPException(status_code=502, detail=f"Sync failed: {res.error}")
    if res.changed:
        await ws_manager.broadcast({"type": "chat_synced", "chat_id": telegram_id})
    return res.to_dict()


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
            "date": iso_utc(m.date),
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

    client = await runtime.client()

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
    client = await runtime.client()

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
            last_seen = iso_utc(user_status.was_online)
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
    client = await runtime.client()

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

    client = await runtime.client()

    from telethon.tl.functions.messages import GetForumTopicsRequest
    from telethon.tl.types import ForumTopic
    try:
        result = await client(GetForumTopicsRequest(
            peer=telegram_id,
            offset_date=None,
            offset_id=0,
            offset_topic=0,
            limit=100,
        ))
    except Exception as exc:
        # Telegram unreachable or the chat is not a forum — derive topics from synced messages
        logger.warning("GetForumTopicsRequest failed, using fallback: %s", exc)
        from sqlalchemy import select as sa_sel, func as sa_func
        from backend.db.models import Message
        rows = (await db.execute(
            sa_sel(Message.topic_id, sa_func.min(Message.date))
            .where(Message.chat_id == telegram_id, Message.topic_id.isnot(None))
            .group_by(Message.topic_id)
        )).all()
        return [
            {
                "id": tid,
                "title": f"Topic {tid}",
                "icon_color": None,
                "icon_emoji_id": None,
                "date": iso_utc(first_date),
                "is_closed": False,
                "is_pinned": False,
            }
            for tid, first_date in rows
        ]

    topics = []
    for topic in result.topics:
        if not isinstance(topic, ForumTopic):
            continue  # ForumTopicDeleted
        topics.append({
            "id": topic.id,
            "title": topic.title,
            "icon_color": getattr(topic, "icon_color", None),
            "icon_emoji_id": getattr(topic, "icon_emoji_id", None),
            "date": iso_utc(topic.date),
            "is_closed": bool(getattr(topic, "closed", False)),
            "is_pinned": bool(getattr(topic, "pinned", False)),
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

    client = await runtime.client()

    try:
        from telethon.tl.functions.messages import CreateForumTopicRequest
        result = await client(CreateForumTopicRequest(
            peer=telegram_id,
            title=req.title,
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

    client = await runtime.client()

    try:
        from telethon.tl.functions.messages import EditForumTopicRequest
        await client(EditForumTopicRequest(
            peer=telegram_id,
            topic_id=topic_id,
            closed=req.closed,
        ))
        return {"success": True, "topic_id": topic_id, "closed": req.closed}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot update topic state: {e}")


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

    client = await runtime.client()

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
    client = await runtime.client()

    try:
        if req.pinned:
            await client.pin_message(telegram_id, msg_tg_id, notify=req.notify)
        else:
            await client.unpin_message(telegram_id, msg_tg_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot {'pin' if req.pinned else 'unpin'} message: {e}")

    # Update local DB
    await crud.set_message_pinned(db, telegram_id, msg_tg_id, req.pinned)
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

    client = await runtime.client()
    limit = max(1, min(limit, 100))
    if not await coordinator.limiter.acquire(1, max_wait=30):
        raise HTTPException(
            status_code=429,
            detail=f"Telegram rate limit — retry in {coordinator.limiter.flood_remaining}s",
            headers={"Retry-After": str(coordinator.limiter.flood_remaining)},
        )

    posts = []
    try:
        async for msg in client.iter_messages(telegram_id, limit=limit):
            if not msg.message and not msg.media:
                continue
            posts.append({
                "telegram_id": msg.id,
                "text": (msg.message or "")[:200],
                "date": iso_utc(msg.date),
                "views": getattr(msg, "views", None) or 0,
                "forwards": getattr(msg, "forwards", None) or 0,
                "replies_count": getattr(msg.replies, "replies", 0) if getattr(msg, "replies", None) else 0,
            })
    except FloodWaitError as e:
        coordinator.limiter.flood(e.seconds)
        raise HTTPException(status_code=429, detail=f"Telegram rate limit — retry in {e.seconds}s",
                            headers={"Retry-After": str(e.seconds)})
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
    client = await runtime.client()

    try:
        entity = await client.get_entity(req.telegram_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Could not find user: {e}")

    telegram_id = utils.get_peer_id(entity)
    name = display_name(entity, telegram_id)
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
    client = await runtime.client()

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
    client = await runtime.client()

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
    client = await runtime.client()

    link = req.link.strip()
    if not link:
        raise HTTPException(status_code=400, detail="Link is required")

    try:
        from telethon.tl.functions.messages import ImportChatInviteRequest

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
        name = display_name(joined_chat, telegram_id)
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
