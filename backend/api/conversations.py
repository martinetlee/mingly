"""Conversations (/api/conversations) and single-message actions
(/api/messages). Keyed by internal ids; operations that need a channel's
network client say so through ``lookups.telegram_peer`` (409 for others)."""

import asyncio
import calendar
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import utils
from telethon.errors import FloodWaitError

from backend.api.lookups import conversation_or_404, message_or_404, telegram_peer
from backend.db import crud
from backend.db.database import AsyncSessionLocal, get_db
from backend.db.models import Conversation, Message, MessageAttachment
from backend.jobs import get_running, run_single
from backend.serialization import iso_utc
from backend.serializers import attachment_out, messages_out
from backend.api.views import conversation_json
from backend.telegram import ids
from backend.telegram.coordinator import coordinator
from backend.telegram.entities import display_name
from backend.telegram.runtime import runtime
from backend.telegram.sync import bulk_sync
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()
messages_router = APIRouter()


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


# ── lists ────────────────────────────────────────────────────────────────────

@router.post("/bulk-sync")
async def bulk_sync_conversations():
    synced = await bulk_sync(n_conversations=50, messages_per_conversation=30)
    return {"synced": synced}


@router.get("")
async def list_conversations(
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "recent",
    tag_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
    channel: Optional[str] = None,
    label: Optional[str] = None,
    kind: Optional[str] = None,
    exclude_kind: Optional[str] = None,
    account_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """The inbox (not archived), optionally narrowed by tag, workspace,
    channel, ``account_id`` or ``kind`` / ``exclude_kind``. ``label`` (Gmail)
    lists a label's conversations, archived ones included."""
    member_ids = None
    if workspace_id is not None:
        member_ids = crud.workspace_member_ids(workspace_id)
    convs = await crud.list_conversations(
        db, limit=max(1, min(limit, 500)), offset=max(0, offset), sort_by=sort_by, tag_id=tag_id, member_ids=member_ids, channel=channel,
        label=label, kind=kind, exclude_kind=exclude_kind, account_id=account_id,
        archived=None if label is not None else False,
    )
    return [conversation_json(c) for c in convs]


class ConversationsByIdsRequest(BaseModel):
    ids: List[int]


@router.post("/by-ids")
async def get_conversations_by_ids(req: ConversationsByIdsRequest, db: AsyncSession = Depends(get_db)):
    """Conversations by id — used to populate folder views whose members fall
    outside the paginated recent window. Archived ones are included (folders
    legitimately contain them); hidden/briefing/deactivated are not."""
    convs = await crud.get_conversations(db, req.ids[:500])
    return [
        conversation_json(c) for c in convs.values()
        if not (c.is_hidden or c.is_briefing or c.is_deactivated)
    ]


@router.get("/search")
async def search_conversations(q: str, db: AsyncSession = Depends(get_db)):
    """Search conversations by title or handle in the local DB (every channel)."""
    q = q.strip()
    if not q:
        return {"local": []}
    pattern = f"%{q}%"
    result = await db.execute(
        select(Conversation)
        .where(
            or_(Conversation.title.ilike(pattern), Conversation.username.ilike(pattern)),
            Conversation.is_hidden == False,  # noqa: E712
            Conversation.is_deactivated == False,  # noqa: E712
        )
        .order_by(desc(Conversation.last_activity_at))
        .limit(20)
    )
    return {"local": [conversation_json(c) for c in result.scalars().all()]}


@router.get("/search/messages")
async def search_messages_global(q: str, db: AsyncSession = Depends(get_db)):
    """Telegram global message search. Each hit's conversation is stored
    locally (so it can be opened); messages are referenced by external id."""
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
    except FloodWaitError as e:
        coordinator.limiter.flood(e.seconds)
        logger.info("Global message search rate-limited (%ss)", e.seconds)
        return []
    except Exception as e:
        logger.warning("Global message search failed: %s", e)
        return []

    # Keyed by marked peer id: raw user/chat/channel ids can collide.
    entities: dict[int, object] = {}
    for c in getattr(result, "chats", []):
        entities[utils.get_peer_id(c)] = c
    for u in getattr(result, "users", []):
        entities[u.id] = u

    by_peer: dict[int, list] = {}
    for msg in getattr(result, "messages", []):
        if not msg.peer_id:
            continue
        peer = utils.get_peer_id(msg.peer_id)
        hits = by_peer.setdefault(peer, [])
        if len(hits) >= 3:  # max 3 messages per conversation
            continue
        sender = entities.get(utils.get_peer_id(msg.from_id)) if msg.from_id else None
        hits.append({
            "external_id": ids.message_key(msg.id),
            "text": (msg.message or "")[:120],
            "sender_name": display_name(sender, getattr(sender, "id", None)) if sender is not None else None,
            "date": iso_utc(msg.date),
        })

    results = []
    for peer, messages in by_peer.items():
        conv = await ids.conversation_for(db, peer)
        if conv is None:
            entity = entities.get(peer)
            if entity is None:
                continue
            conv = await ids.upsert_dialog(db, peer, entity)
        results.append({
            "conversation_id": conv.id,
            "conversation_title": conv.title,
            "conversation_kind": conv.kind,
            "messages": messages,
        })
    return results


@router.get("/hidden")
async def list_hidden_conversations(db: AsyncSession = Depends(get_db)):
    return [conversation_json(c) for c in await crud.get_hidden_conversations(db)]


@router.get("/tracked")
async def list_tracked_conversations(db: AsyncSession = Depends(get_db)):
    return [conversation_json(c) for c in await crud.get_tracked_conversations(db)]


@router.get("/briefing")
async def list_briefing_conversations(db: AsyncSession = Depends(get_db)):
    return [conversation_json(c) for c in await crud.get_briefing_conversations(db)]


@router.get("/archived")
async def list_archived_conversations(db: AsyncSession = Depends(get_db)):
    return [conversation_json(c) for c in await crud.get_archived_conversations(db)]


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: int, db: AsyncSession = Depends(get_db)):
    return conversation_json(await conversation_or_404(db, conversation_id))


# ── flags ────────────────────────────────────────────────────────────────────

async def _set_flags(db, conversation_id: int, **flags) -> Conversation:
    conv = await crud.set_conversation_flags(db, conversation_id, **flags)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


async def _telegram_best_effort(conv: Conversation, what: str, call) -> None:
    """Mirror a local change to Telegram when the conversation is there.
    Failures are logged: the local state already changed."""
    if not ids.is_telegram(conv):
        return
    client = await runtime.client(require=False)
    if client is None:
        return
    try:
        await call(client, ids.peer_id_of(conv))
    except Exception as e:
        logger.warning("Failed to sync %s to Telegram: %s", what, e)


@router.post("/{conversation_id}/hide")
async def toggle_hide(conversation_id: int, req: HideRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_hidden=req.hidden)
    return {"conversation_id": conversation_id, "is_hidden": conv.is_hidden}


@router.post("/{conversation_id}/briefing")
async def toggle_briefing(conversation_id: int, req: BriefingRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_briefing=req.briefing)
    return {"conversation_id": conversation_id, "is_briefing": conv.is_briefing}


@router.post("/{conversation_id}/track")
async def toggle_track(conversation_id: int, req: TrackRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_tracked=req.tracked)
    return {"conversation_id": conversation_id, "is_tracked": conv.is_tracked}


@router.post("/{conversation_id}/mark-read")
async def mark_read(conversation_id: int, db: AsyncSession = Depends(get_db)):
    conv = await crud.mark_read(db, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Without this, the periodic dialog sync restores Telegram's unread count
    # and the badge resurrects within ~2 min.
    async def _ack(client, peer):
        await client.send_read_acknowledge(await client.get_input_entity(peer))

    await _telegram_best_effort(conv, "read state", _ack)
    if not ids.is_telegram(conv):
        connector = _connector_of(conv)
        if connector is not None:
            try:
                await connector.mark_read(conv)
            except Exception as e:  # best-effort, like Telegram's
                logger.warning("Failed to send read state to %s: %s", conv.account_id, e)
    return {"success": True}


@router.post("/{conversation_id}/mark-unread")
async def mark_unread(conversation_id: int, req: MarkUnreadRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_marked_unread=req.marked_unread)
    return {"conversation_id": conversation_id, "is_marked_unread": conv.is_marked_unread}


@router.post("/{conversation_id}/pin")
async def toggle_pin(conversation_id: int, req: PinRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_pinned=req.pinned)

    async def _pin(client, peer):
        from telethon.tl.functions.messages import ToggleDialogPinRequest
        from telethon.tl.types import InputDialogPeer
        await client(ToggleDialogPinRequest(peer=InputDialogPeer(peer=await client.get_input_entity(peer)), pinned=req.pinned))

    await _telegram_best_effort(conv, "pin", _pin)
    return {"conversation_id": conversation_id, "is_pinned": conv.is_pinned}


@router.post("/{conversation_id}/archive")
async def toggle_archive(conversation_id: int, req: ArchiveRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_archived=req.archived)

    async def _archive(client, peer):
        from telethon.tl.functions.folders import EditPeerFoldersRequest
        from telethon.tl.types import InputFolderPeer
        folder_id = 1 if req.archived else 0  # 1 = Archive folder
        await client(EditPeerFoldersRequest(
            folder_peers=[InputFolderPeer(peer=await client.get_input_entity(peer), folder_id=folder_id)]
        ))

    await _telegram_best_effort(conv, "archive", _archive)
    return {"conversation_id": conversation_id, "is_archived": conv.is_archived}


@router.post("/{conversation_id}/mute")
async def toggle_mute(conversation_id: int, req: MuteRequest, db: AsyncSession = Depends(get_db)):
    muted_until = None
    if req.muted and req.duration:
        delta = {"1h": timedelta(hours=1), "8h": timedelta(hours=8), "1d": timedelta(days=1)}.get(req.duration)
        if delta:
            muted_until = datetime.utcnow() + delta
        # "forever" leaves muted_until as None (muted indefinitely)
    conv = await _set_flags(db, conversation_id, is_muted=req.muted, muted_until=muted_until)

    async def _mute(client, peer):
        from telethon.tl.functions.account import UpdateNotifySettingsRequest
        from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings
        # muted_until is naive UTC: timegm, not .timestamp() (which reads it as local time)
        until = calendar.timegm((muted_until or datetime(2038, 1, 1)).timetuple()) if req.muted else 0
        await client(UpdateNotifySettingsRequest(
            peer=InputNotifyPeer(peer=await client.get_input_entity(peer)),
            settings=InputPeerNotifySettings(mute_until=until),
        ))

    await _telegram_best_effort(conv, "mute", _mute)
    return {"conversation_id": conversation_id, "is_muted": conv.is_muted, "muted_until": iso_utc(conv.muted_until)}


@router.post("/{conversation_id}/archive-history")
async def archive_history(conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Queue a full-history walk (or top-up) for one conversation ahead of the
    background DM archive. Distinct from ``/archive`` (the archive folder)."""
    from backend.telegram import archive

    conv = await conversation_or_404(db, conversation_id)
    telegram_peer(conv, "History archiving")
    await archive.request_conversation(conversation_id, priority=True)
    cov = (await crud.get_coverage(db, [conversation_id])).get(conversation_id)
    return {
        "queued": True,
        "coverage": {
            "history_complete": bool(cov and cov.history_complete),
            "message_count": await crud.count_messages(db, conversation_id),
            "oldest_seq": cov.oldest_seq if cov else None,
            "newest_contiguous_seq": cov.newest_contiguous_seq if cov else None,
        },
    }


# ── messages ─────────────────────────────────────────────────────────────────

async def _local_message_count(conversation_id: int) -> int:
    async with AsyncSessionLocal() as db:
        return await crud.count_messages(db, conversation_id)


async def _auto_sync(conversation_id: int, first_sync: bool) -> None:
    """Background sync on open, with retry through transient failures.

    Emits exactly one of: ``conversation_synced`` (something changed, or the
    first sync finished with history), ``conversation_sync_empty`` (a
    *completed* fetch found no retrievable history), ``conversation_sync_failed``
    (gave up / unavailable).
    """
    limit = 100 if first_sync else 50
    last = None
    for attempt in range(1, 5):
        res = await coordinator.sync_conversation(conversation_id, limit)
        last = res
        changed = res.changed
        if res.completed and first_sync and not res.exhausted:
            if await _local_message_count(conversation_id) < 5:
                # May be full of service messages — try a deeper fetch. Only
                # its own completion can prove there is no history.
                deep = await coordinator.sync_conversation(conversation_id, 500)
                changed = changed or deep.changed
                res = last = deep
        if res.completed:
            if first_sync and await _local_message_count(conversation_id) == 0:
                await ws_manager.broadcast({"type": "conversation_sync_empty", "conversation_id": conversation_id})
                return
            if changed or first_sync:
                await ws_manager.broadcast({"type": "conversation_synced", "conversation_id": conversation_id})
            return
        if res.flood_wait:
            logger.info("auto-sync %s: FloodWait %ss (attempt %d)", conversation_id, res.flood_wait, attempt)
            await asyncio.sleep(min(res.flood_wait + 2, 600))
        elif res.status in ("unavailable", "not_found") and await runtime.client(require=False) is not None:
            break  # the conversation itself can't be fetched (deleted, private, banned)
        else:
            await asyncio.sleep(3 * attempt)
    logger.warning("auto-sync %s gave up: %s", conversation_id, last.error if last else None)
    await ws_manager.broadcast({
        "type": "conversation_sync_failed",
        "conversation_id": conversation_id,
        "status": last.status if last else "error",
        "error": last.error if last else None,
    })


def _maybe_auto_sync(conv: Optional[Conversation], latest: bool = True) -> bool:
    """Start a background sync when the conversation was never synced (deep
    fetch, any view) or — for the latest view — its last sync is older than
    15 min. Returns True when a sync is running. Telegram only."""
    if conv is None or conv.is_hidden or conv.is_deactivated or not ids.is_telegram(conv):
        return False
    key = f"autosync:{conv.id}"
    if get_running(key) is not None:
        return True
    first_sync = conv.last_synced_at is None
    stale = not first_sync and conv.last_synced_at < datetime.utcnow() - timedelta(minutes=15)
    if first_sync or (stale and latest):
        conv_id = conv.id
        run_single(key, lambda: _auto_sync(conv_id, first_sync))
        return True
    return coordinator.is_syncing(conv.id)


def _parse_date(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.rstrip("Z"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail=f"Invalid date: {value!r}")
    return dt


def _can_fetch_around(conv: Conversation, external_id: Optional[str]) -> bool:
    """Whether the channel can fetch an unstored message (or date) window."""
    if not ids.is_telegram(conv):
        return False
    return external_id is None or (external_id.isascii() and external_id.isdigit())


@router.get("/{conversation_id}/messages/window")
async def get_message_window(
    conversation_id: int,
    limit: int = 50,
    before: Optional[int] = None,
    after: Optional[int] = None,
    anchor: Optional[int] = None,
    around_external_id: Optional[str] = None,
    around_date: Optional[str] = None,
    topic_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """Keyset message window (see crud.get_message_window).

    At most one positioning parameter: ``before``/``after``/``anchor``
    (message ids), ``around_external_id`` (a channel message id, e.g. a reply
    target that may not be stored yet), ``around_date`` (ISO) — or none for
    the newest messages, which also starts a background sync when stale.
    """
    conv = await conversation_or_404(db, conversation_id)
    date = _parse_date(around_date) if around_date is not None else None
    if around_external_id is not None:
        stored = await crud.get_message_by_external(db, conversation_id, around_external_id)
        anchor = stored.id if stored is not None else None

    async def _window():
        return await crud.get_message_window(
            db, conversation_id, limit=limit, before=before, after=after,
            anchor=anchor, around_date=date, topic_id=topic_id,
        )

    window = await _window()
    latest = before is None and after is None and anchor is None and date is None and around_external_id is None
    syncing = _maybe_auto_sync(conv, latest=latest)

    # Jump target not stored locally (or the date is older than local
    # history): fetch it plus a window around it from the channel, then re-read.
    anchor_reason = None
    wants_jump = around_external_id is not None or date is not None
    if anchor is None and around_external_id is not None:
        window = {"rows": [], "has_older": False, "has_newer": False, "anchor_found": False}
    if wants_jump:
        rows = window["rows"]
        before_local_history = (
            date is not None and rows and not window["has_older"] and rows[0][0].date > date
        )
        if not _can_fetch_around(conv, around_external_id):
            # Only Telegram fetches unstored messages, and its ids are integers
            if not window["anchor_found"]:
                anchor_reason = "not_found"
        elif not window["anchor_found"] or before_local_history:
            res = await coordinator.fetch_around(
                conversation_id,
                anchor_external_id=around_external_id,
                date=date if around_external_id is None else None,
                limit=limit,
            )
            if res.completed:
                if around_external_id is not None:
                    stored = await crud.get_message_by_external(db, conversation_id, around_external_id)
                    anchor = stored.id if stored is not None else None
                if anchor is not None or date is not None:
                    window = await _window()
            if not window["anchor_found"]:
                if res.status == "not_found" or res.completed:
                    anchor_reason = "not_found"
                elif res.flood_wait or res.status == "skipped":
                    anchor_reason = "rate_limited"
                elif res.status == "unavailable":
                    anchor_reason = "unavailable"
                else:
                    anchor_reason = "error"
    return {
        "messages": await messages_out(db, window["rows"]),
        "has_older": window["has_older"],
        "has_newer": window["has_newer"],
        "anchor_found": window["anchor_found"],
        "anchor_reason": anchor_reason,
        "syncing": syncing,
    }


def _raise_for_sync(res, what: str) -> None:
    if res.flood_wait:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited — retry in {res.flood_wait}s",
            headers={"Retry-After": str(res.flood_wait)},
        )
    if res.status == "error":
        raise HTTPException(status_code=502, detail=f"{what} failed: {res.error}")


@router.post("/{conversation_id}/backfill")
async def backfill_history(
    conversation_id: int,
    limit: int = 100,
    topic_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """Fetch history older than the oldest stored message. ``exhausted`` =
    the channel has nothing older."""
    conv = await conversation_or_404(db, conversation_id)
    telegram_peer(conv, "Loading older history")
    oldest = await crud.get_oldest_message(db, conversation_id, topic_id)
    res = await coordinator.sync_conversation(
        conversation_id, max(1, min(limit, 200)), offset_seq=oldest.seq if oldest else 0, topic_id=topic_id,
    )
    _raise_for_sync(res, "Backfill")
    if res.status == "unavailable":
        await runtime.client()  # 503 when Telegram itself is unreachable
        raise HTTPException(status_code=422, detail=res.error or "Conversation unavailable")
    return {"added": res.added, "exhausted": res.exhausted, "status": res.status}


@router.get("/{conversation_id}/messages/search")
async def search_messages(conversation_id: int, q: str = "", limit: int = 50, db: AsyncSession = Depends(get_db)):
    if not q.strip():
        return []
    await conversation_or_404(db, conversation_id)
    rows = await crud.search_messages_in_conversation(db, conversation_id, q.strip(), limit=max(1, min(limit, 200)))
    return await messages_out(db, list(reversed(rows)))


@router.post("/{conversation_id}/sync")
async def sync_conversation(conversation_id: int, limit: int = 100, db: AsyncSession = Depends(get_db)):
    """Refresh the newest messages from the channel. Returns the typed sync
    result; ``conversation_synced`` is broadcast only when something changed."""
    conv = await conversation_or_404(db, conversation_id)
    telegram_peer(conv, "Syncing")
    res = await coordinator.sync_conversation(conversation_id, max(1, min(limit, 200)))
    _raise_for_sync(res, "Sync")
    if res.status == "unavailable":
        await runtime.client()  # 503 when Telegram itself is unreachable
        raise HTTPException(
            status_code=422,
            detail=f"Could not resolve the conversation. The user or chat may have been deleted. ({res.error})",
        )
    if res.changed:
        await ws_manager.broadcast({"type": "conversation_synced", "conversation_id": conversation_id})
    return res.to_dict()


_SHARED_MEDIA_KINDS = {
    "media": ("photo", "video", "gif"),
    "files": ("document",),
}


@router.get("/{conversation_id}/shared-media")
async def get_shared_media(
    conversation_id: int,
    media_filter: str = "media",
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Shared items of a conversation: 'media' (photos, videos, GIFs),
    'files' (documents) or 'links' (messages containing URLs)."""
    await conversation_or_404(db, conversation_id)
    query = select(Message).where(Message.conversation_id == conversation_id)
    if media_filter in _SHARED_MEDIA_KINDS:
        query = query.where(Message.id.in_(
            select(MessageAttachment.message_id).where(MessageAttachment.kind.in_(_SHARED_MEDIA_KINDS[media_filter]))
        ))
    elif media_filter == "links":
        query = query.where(
            Message.text.isnot(None),
            or_(Message.text.contains("http://"), Message.text.contains("https://")),
        )
    else:
        return []
    rows = (await db.execute(
        query.order_by(desc(Message.date)).limit(max(1, min(limit, 200))).offset(max(0, offset))
    )).scalars().all()
    kinds = _SHARED_MEDIA_KINDS.get(media_filter)
    return [
        {
            "id": m.id,
            "message_id": m.id,
            "date": iso_utc(m.date),
            "sender_name": m.sender_name,
            "text": m.text,
            "attachments": [attachment_out(a) for a in m.attachments if kinds is None or a.kind in kinds],
        }
        for m in rows
    ]


@router.get("/{conversation_id}/members")
async def get_members(conversation_id: int, limit: int = 100, db: AsyncSession = Depends(get_db)):
    """Members of a group or channel (each recorded as an identity)."""
    conv = await conversation_or_404(db, conversation_id)
    if conv.kind == "direct":
        return []
    peer = telegram_peer(conv, "Listing members")
    client = await runtime.client()

    from telethon.tl.types import User

    members = []
    try:
        async for user in client.iter_participants(peer, limit=limit):
            if not isinstance(user, User):
                continue
            role = "member"
            participant = getattr(user, "participant", None)
            if participant:
                cls_name = type(participant).__name__
                if "Creator" in cls_name:
                    role = "creator"
                elif "Admin" in cls_name:
                    role = "admin"
            ident = await ids.ensure_user_identity(db, user)
            members.append({
                "identity_id": ident.id,
                "person_id": ident.person_id,
                "name": f"{user.first_name or ''} {user.last_name or ''}".strip() or str(user.id),
                "username": user.username,
                "is_bot": user.bot,
                "role": role,
            })
        await db.commit()
    except Exception as e:
        # Some chats don't allow participant listing
        raise HTTPException(status_code=403, detail=f"Cannot fetch members: {e}")

    role_order = {"creator": 0, "admin": 1, "member": 2}
    members.sort(key=lambda m: (role_order.get(m["role"], 2), m["name"].lower()))
    return members


@router.get("/{conversation_id}/presence")
async def get_presence(conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Online / last-seen status of the other party of a direct conversation."""
    conv = await conversation_or_404(db, conversation_id)
    peer = telegram_peer(conv, "Presence")
    if conv.kind != "direct":
        return {"status": "unknown"}
    client = await runtime.client()
    try:
        from telethon.tl.functions.users import GetFullUserRequest
        from backend.telegram.sync import status_info

        full = await client(GetFullUserRequest(peer))
        user = full.users[0] if full.users else None
        if not user:
            return {"status": "unknown"}
        return status_info(getattr(user, "status", None)) or {"status": "unknown"}
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


@router.post("/{conversation_id}/typing")
async def send_typing(conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Send a typing indicator. Non-critical: failures don't fail the request."""
    conv = await conversation_or_404(db, conversation_id)
    peer = telegram_peer(conv, "Typing indicators")
    client = await runtime.client()
    try:
        from telethon.tl.functions.messages import SetTypingRequest
        from telethon.tl.types import SendMessageTypingAction
        await client(SetTypingRequest(peer=peer, action=SendMessageTypingAction()))
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── forum topics ─────────────────────────────────────────────────────────────

@router.get("/{conversation_id}/topics")
async def list_topics(conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Forum topics of a supergroup with forum mode enabled."""
    conv = await conversation_or_404(db, conversation_id)
    peer = telegram_peer(conv, "Forum topics")
    client = await runtime.client()

    from telethon.tl.functions.messages import GetForumTopicsRequest
    from telethon.tl.types import ForumTopic
    try:
        result = await client(GetForumTopicsRequest(
            peer=peer, offset_date=None, offset_id=0, offset_topic=0, limit=100,
        ))
    except Exception as exc:
        # Telegram unreachable or not a forum — derive topics from stored messages
        logger.warning("GetForumTopicsRequest failed, using fallback: %s", exc)
        rows = (await db.execute(
            select(Message.topic_id, func.min(Message.date))
            .where(Message.conversation_id == conversation_id, Message.topic_id.isnot(None))
            .group_by(Message.topic_id)
        )).all()
        return [
            {
                "id": tid, "title": f"Topic {tid}", "icon_color": None, "icon_emoji_id": None,
                "date": iso_utc(first_date), "is_closed": False, "is_pinned": False,
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


@router.post("/{conversation_id}/topics")
async def create_topic(conversation_id: int, req: CreateTopicRequest, db: AsyncSession = Depends(get_db)):
    conv = await conversation_or_404(db, conversation_id)
    peer = telegram_peer(conv, "Forum topics")
    client = await runtime.client()
    try:
        from telethon.tl.functions.messages import CreateForumTopicRequest
        result = await client(CreateForumTopicRequest(peer=peer, title=req.title))
        topic_id = None
        for upd in getattr(result, "updates", []):
            msg = getattr(upd, "message", None)
            if msg:
                topic_id = getattr(msg, "id", None)
                break
        return {"success": True, "topic_id": topic_id}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot create topic: {e}")


class TopicStateRequest(BaseModel):
    closed: bool


@router.post("/{conversation_id}/topics/{topic_id}/state")
async def set_topic_state(
    conversation_id: int, topic_id: int, req: TopicStateRequest, db: AsyncSession = Depends(get_db),
):
    """Close or reopen a forum topic."""
    conv = await conversation_or_404(db, conversation_id)
    peer = telegram_peer(conv, "Forum topics")
    client = await runtime.client()
    try:
        from telethon.tl.functions.messages import EditForumTopicRequest
        await client(EditForumTopicRequest(peer=peer, topic_id=topic_id, closed=req.closed))
        return {"success": True, "topic_id": topic_id, "closed": req.closed}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot update topic state: {e}")


class SlowModeRequest(BaseModel):
    seconds: int  # 0 to disable, valid values: 0, 10, 30, 60, 300, 900, 3600


@router.post("/{conversation_id}/slow-mode")
async def set_slow_mode(conversation_id: int, req: SlowModeRequest, db: AsyncSession = Depends(get_db)):
    conv = await conversation_or_404(db, conversation_id)
    peer = telegram_peer(conv, "Slow mode")
    client = await runtime.client()
    try:
        from telethon.tl.functions.channels import ToggleSlowModeRequest
        await client(ToggleSlowModeRequest(channel=peer, seconds=req.seconds))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot set slow mode: {e}")
    conv.slow_mode_seconds = req.seconds
    await db.commit()
    return {"success": True, "slow_mode_seconds": req.seconds}


@router.get("/{conversation_id}/pinned")
async def get_pinned_messages(conversation_id: int, db: AsyncSession = Depends(get_db)):
    await conversation_or_404(db, conversation_id)
    return await messages_out(db, list(await crud.get_pinned_messages(db, conversation_id)))


@router.get("/{conversation_id}/post-stats")
async def get_post_stats(conversation_id: int, limit: int = 20, db: AsyncSession = Depends(get_db)):
    """Views / forwards / replies of recent channel posts."""
    conv = await conversation_or_404(db, conversation_id)
    peer = telegram_peer(conv, "Post stats")
    if conv.kind != "channel":
        raise HTTPException(status_code=400, detail="Post stats are only available for channels")
    client = await runtime.client()
    limit = max(1, min(limit, 100))
    if not await coordinator.limiter.acquire(1, max_wait=30):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited — retry in {coordinator.limiter.flood_remaining}s",
            headers={"Retry-After": str(coordinator.limiter.flood_remaining)},
        )
    posts = []
    try:
        async for msg in client.iter_messages(peer, limit=limit):
            if not msg.message and not msg.media:
                continue
            posts.append({
                "external_id": ids.message_key(msg.id),
                "text": (msg.message or "")[:200],
                "date": iso_utc(msg.date),
                "views": getattr(msg, "views", None) or 0,
                "forwards": getattr(msg, "forwards", None) or 0,
                "replies_count": getattr(msg.replies, "replies", 0) if getattr(msg, "replies", None) else 0,
            })
    except FloodWaitError as e:
        coordinator.limiter.flood(e.seconds)
        raise HTTPException(status_code=429, detail=f"Rate limited — retry in {e.seconds}s",
                            headers={"Retry-After": str(e.seconds)})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot fetch post stats: {e}")
    stored = await crud.message_ids_by_external(db, conversation_id, [p["external_id"] for p in posts])
    for p in posts:
        p["message_id"] = stored.get(p["external_id"])
    return posts


# ── single messages (/api/messages) ──────────────────────────────────────────

class ReactRequest(BaseModel):
    emoji: Optional[str] = None


def _connector_of(conv: Conversation):
    from backend.connectors.manager import manager
    from backend.serializers import channel_of
    return manager.for_account(channel_of(conv), conv.account_id)


async def _react_via_connector(conv: Conversation, msg: Message, emoji: Optional[str]) -> None:
    from backend.api.lookups import unsupported_channel
    from backend.connectors.base import Capability, UnsupportedOperation

    connector = _connector_of(conv)
    if connector is None or Capability.REACT not in connector.conversation_capabilities(conv):
        raise unsupported_channel(conv, "Reactions")
    try:
        await connector.react(conv, msg, emoji)
    except UnsupportedOperation as e:
        raise unsupported_channel(conv, str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot send reaction: {e}")


@messages_router.post("/{message_id}/react")
async def react_to_message(message_id: int, req: ReactRequest, db: AsyncSession = Depends(get_db)):
    """Send or remove the current user's emoji reaction on a message."""
    msg = await message_or_404(db, message_id)
    conv = await conversation_or_404(db, msg.conversation_id)
    if not ids.is_telegram(conv):
        await _react_via_connector(conv, msg, req.emoji)
        msg = await crud.set_my_reaction(db, message_id, req.emoji)
        return {"success": True, "my_reaction": msg.my_reaction if msg else req.emoji}
    peer = telegram_peer(conv, "Reactions")
    client = await runtime.client()
    try:
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji
        reaction = [ReactionEmoji(emoticon=req.emoji)] if req.emoji else []
        await client(SendReactionRequest(peer=peer, msg_id=ids.tg_message_id_of(msg), reaction=reaction))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot send reaction: {e}")
    msg = await crud.set_my_reaction(db, message_id, req.emoji)
    return {"success": True, "my_reaction": msg.my_reaction if msg else req.emoji}


class MessagePinRequest(BaseModel):
    pinned: bool
    notify: bool = False


@messages_router.post("/{message_id}/pin")
async def pin_message(message_id: int, req: MessagePinRequest, db: AsyncSession = Depends(get_db)):
    """Pin or unpin a message in its conversation."""
    msg = await message_or_404(db, message_id)
    conv = await conversation_or_404(db, msg.conversation_id)
    peer = telegram_peer(conv, "Pinning messages")
    client = await runtime.client()
    try:
        if req.pinned:
            await client.pin_message(peer, ids.tg_message_id_of(msg), notify=req.notify)
        else:
            await client.unpin_message(peer, ids.tg_message_id_of(msg))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot {'pin' if req.pinned else 'unpin'} message: {e}")
    await crud.set_message_pinned(db, message_id, req.pinned)
    return {"success": True, "is_pinned": req.pinned}
