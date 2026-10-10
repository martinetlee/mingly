"""Conversations (/api/conversations) and single-message actions
(/api/messages). Keyed by internal ids; operations that need the channel go
through the conversation's connector and its roles (``connectors.roles``;
409 ``unsupported_channel`` when it doesn't have them)."""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.lookups import (
    channel_call, connector_for, connector_with, conversation_or_404, message_or_404,
)
from backend.api.views import conversation_json
from backend.connectors import autosync
from backend.connectors.base import Capability, Connector
from backend.connectors.manager import manager
from backend.connectors.roles import (
    DialogActions, ForumTopics, GlobalSearch, GroupAdmin, GroupMembers, HistorySync, MessageActions,
)
from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Conversation, Message, MessageAttachment
from backend.errors import DomainError
from backend.serialization import iso_utc
from backend.serializers import attachment_out, messages_out
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
    synced = 0
    for connector in manager.with_role(HistorySync):
        synced += await connector.sync_recent(n_conversations=50, per_conversation=30)
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
    return {"local": [conversation_json(c) for c in await crud.search_conversations(db, q, 20)]}


@router.get("/search/messages")
async def search_messages_global(q: str, db: AsyncSession = Depends(get_db)):
    """Server-side message search of every channel that has one (best
    effort). Each hit's conversation is stored locally (so it can be opened);
    messages are referenced by external id."""
    if not q.strip():
        return []
    results = []
    for connector in manager.with_role(GlobalSearch):
        results += await connector.search_messages(q)
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


async def _mirror(conv: Conversation, what: str, call) -> None:
    """Mirror a local change to the conversation's channel when it supports
    it. Failures are logged: the local state already changed."""
    connector = connector_for(conv)
    if not isinstance(connector, DialogActions):
        return
    try:
        await call(connector)
    except Exception as e:
        logger.warning("Failed to sync %s to %s: %s", what, connector.display_name, e)


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
    # Read receipts, best effort (Telegram: without it the periodic dialog
    # sync restores the unread count and the badge resurrects).
    connector = connector_for(conv)
    if connector is not None:
        try:
            await connector.mark_read(conv)
        except Exception as e:
            logger.warning("Failed to send read state to %s: %s", connector.display_name, e)
    return {"success": True}


@router.post("/{conversation_id}/mark-unread")
async def mark_unread(conversation_id: int, req: MarkUnreadRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_marked_unread=req.marked_unread)
    return {"conversation_id": conversation_id, "is_marked_unread": conv.is_marked_unread}


@router.post("/{conversation_id}/pin")
async def toggle_pin(conversation_id: int, req: PinRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_pinned=req.pinned)
    await _mirror(conv, "pin", lambda c: c.set_dialog_pinned(conv, req.pinned))
    return {"conversation_id": conversation_id, "is_pinned": conv.is_pinned}


@router.post("/{conversation_id}/archive")
async def toggle_archive(conversation_id: int, req: ArchiveRequest, db: AsyncSession = Depends(get_db)):
    conv = await _set_flags(db, conversation_id, is_archived=req.archived)
    await _mirror(conv, "archive", lambda c: c.set_dialog_archived(conv, req.archived))
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
    await _mirror(conv, "mute", lambda c: c.set_dialog_muted(conv, req.muted, muted_until))
    return {"conversation_id": conversation_id, "is_muted": conv.is_muted, "muted_until": iso_utc(conv.muted_until)}


@router.post("/{conversation_id}/archive-history")
async def archive_history(conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Queue a full-history walk (or top-up) for one conversation ahead of the
    background DM archive. Distinct from ``/archive`` (the archive folder)."""
    conv = await conversation_or_404(db, conversation_id)
    connector = connector_with(conv, HistorySync, "History archiving")
    await connector.request_full_history(conv)
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


def _parse_date(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.rstrip("Z"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail=f"Invalid date: {value!r}")
    return dt


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
    connector = connector_for(conv)
    history = connector if isinstance(connector, HistorySync) else None
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
    syncing = autosync.maybe_auto_sync(history, conv, latest=latest)

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
        if history is None or not history.can_fetch_around(conv, around_external_id):
            if not window["anchor_found"]:
                anchor_reason = "not_found"
        elif not window["anchor_found"] or before_local_history:
            res = await history.fetch_around(
                conv,
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
    connector = connector_with(conv, HistorySync, "Loading older history")
    oldest = await crud.get_oldest_message(db, conversation_id, topic_id)
    res = await connector.sync_history(
        conv, max(1, min(limit, 200)), offset_seq=oldest.seq if oldest else 0, topic_id=topic_id,
    )
    _raise_for_sync(res, "Backfill")
    if res.status == "unavailable":
        await connector.ensure_reachable()  # 503 when the channel itself is unreachable
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
    connector = connector_with(conv, HistorySync, "Syncing")
    res = await connector.sync_history(conv, max(1, min(limit, 200)))
    _raise_for_sync(res, "Sync")
    if res.status == "unavailable":
        await connector.ensure_reachable()  # 503 when the channel itself is unreachable
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
    connector = connector_with(conv, GroupMembers, "Listing members", Capability.MEMBERS)
    # Some chats don't allow participant listing
    return await channel_call(conv, connector.list_members(conv, limit), "Cannot fetch members", status=403)


@router.get("/{conversation_id}/presence")
async def get_presence(conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Online / last-seen status of the other party of a direct conversation."""
    conv = await conversation_or_404(db, conversation_id)
    connector = connector_with(conv, GroupMembers, "Presence")
    return await channel_call(conv, connector.presence(conv))


@router.post("/{conversation_id}/typing")
async def send_typing(conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Send a typing indicator. Non-critical: failures don't fail the request."""
    conv = await conversation_or_404(db, conversation_id)
    connector = connector_with(conv, GroupMembers, "Typing indicators")
    try:
        await connector.send_typing(conv)
        return {"success": True}
    except (HTTPException, DomainError):
        raise
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── forum topics ─────────────────────────────────────────────────────────────

@router.get("/{conversation_id}/topics")
async def list_topics(conversation_id: int, db: AsyncSession = Depends(get_db)):
    """Forum topics of a supergroup with forum mode enabled."""
    conv = await conversation_or_404(db, conversation_id)
    connector = connector_with(conv, ForumTopics, "Forum topics")
    try:
        return await connector.list_topics(conv)
    except (HTTPException, DomainError):
        raise
    except Exception as exc:
        # The channel can't list them (or it's not a forum) — derive topics
        # from stored messages
        logger.warning("Listing forum topics failed, using fallback: %s", exc)
    return [
        {
            "id": tid, "title": f"Topic {tid}", "icon_color": None, "icon_emoji_id": None,
            "date": iso_utc(first_date), "is_closed": False, "is_pinned": False,
        }
        for tid, first_date in await crud.stored_topics(db, conversation_id)
    ]


class CreateTopicRequest(BaseModel):
    title: str


@router.post("/{conversation_id}/topics")
async def create_topic(conversation_id: int, req: CreateTopicRequest, db: AsyncSession = Depends(get_db)):
    conv = await conversation_or_404(db, conversation_id)
    connector = connector_with(conv, ForumTopics, "Forum topics")
    topic_id = await channel_call(conv, connector.create_topic(conv, req.title), "Cannot create topic")
    return {"success": True, "topic_id": topic_id}


class TopicStateRequest(BaseModel):
    closed: bool


@router.post("/{conversation_id}/topics/{topic_id}/state")
async def set_topic_state(
    conversation_id: int, topic_id: int, req: TopicStateRequest, db: AsyncSession = Depends(get_db),
):
    """Close or reopen a forum topic."""
    conv = await conversation_or_404(db, conversation_id)
    connector = connector_with(conv, ForumTopics, "Forum topics")
    await channel_call(conv, connector.set_topic_closed(conv, topic_id, req.closed), "Cannot update topic state")
    return {"success": True, "topic_id": topic_id, "closed": req.closed}


class SlowModeRequest(BaseModel):
    seconds: int  # 0 to disable, valid values: 0, 10, 30, 60, 300, 900, 3600


@router.post("/{conversation_id}/slow-mode")
async def set_slow_mode(conversation_id: int, req: SlowModeRequest, db: AsyncSession = Depends(get_db)):
    conv = await conversation_or_404(db, conversation_id)
    connector = connector_with(conv, GroupAdmin, "Slow mode")
    await channel_call(conv, connector.set_slow_mode(conv, req.seconds), "Cannot set slow mode")
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
    connector = connector_with(conv, GroupAdmin, "Post stats")
    if conv.kind != "channel":
        raise HTTPException(status_code=400, detail="Post stats are only available for channels")
    posts = await channel_call(conv, connector.post_stats(conv, max(1, min(limit, 100))), "Cannot fetch post stats")
    stored = await crud.message_ids_by_external(db, conversation_id, [p["external_id"] for p in posts])
    for p in posts:
        p["message_id"] = stored.get(p["external_id"])
    return posts


# ── single messages (/api/messages) ──────────────────────────────────────────

class ReactRequest(BaseModel):
    emoji: Optional[str] = None


async def _react_via_connector(conv: Conversation, msg: Message, emoji: Optional[str]) -> None:
    connector = connector_with(conv, Connector, "Reactions", Capability.REACT)
    await channel_call(conv, connector.react(conv, msg, emoji), "Cannot send reaction")


@messages_router.post("/{message_id}/react")
async def react_to_message(message_id: int, req: ReactRequest, db: AsyncSession = Depends(get_db)):
    """Send or remove the current user's emoji reaction on a message."""
    msg = await message_or_404(db, message_id)
    conv = await conversation_or_404(db, msg.conversation_id)
    await _react_via_connector(conv, msg, req.emoji)
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
    connector = connector_with(conv, MessageActions, "Pinning messages", Capability.PIN)
    await channel_call(
        conv, connector.pin_message(conv, msg, pinned=req.pinned, notify=req.notify),
        f"Cannot {'pin' if req.pinned else 'unpin'} message",
    )
    await crud.set_message_pinned(db, message_id, req.pinned)
    return {"success": True, "is_pinned": req.pinned}
