"""Telegram dialog / history sync and live update handlers.

Everything stored goes through ``backend.telegram.ids`` (peer → conversation,
user → identity) and ``crud.save_messages``; live messages then fan out
through ``backend.ingest.fanout`` like every other channel's.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import BigInteger, cast, select, update
from telethon import events, utils as tl_utils
from telethon.tl.types import User

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Message
from backend.ingest import fanout
from backend.serialization import iso_utc
from backend.telegram import ids
from backend.telegram.runtime import runtime
from backend.telegram.convert import extract_reactions, is_storable, media_type, rows_for
from backend.telegram.coordinator import (
    broadcast_conversations_synced, coordinator, sync_conversations_since as _sync_since,
)
from backend.telegram.entities import display_name, is_deactivated, is_forum, sender_name
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

# Marked peer ids of channels/supergroups are -100xxxxxxxxxx, below this.
CHANNEL_PEER_FLOOR = -1000000000000

# When the last complete enumeration (every folder, no limit) finished.
last_full_dialog_sync: Optional[datetime] = None


async def sync_dialogs(limit: Optional[int] = None, include_archived: bool = False) -> list[int]:
    """Fetch dialogs and upsert them as conversations (one transaction).

    Args:
        limit: Max dialogs to fetch per folder. None = all dialogs.
        include_archived: Also scan the archived/contact-requests folder.

    Returns the ids of conversations seen for the first time.
    """
    from backend.telegram.media import download_avatar

    client = await runtime.client(require=False)
    if not client:
        return []

    # Telethon 1.45: folder=None asks Telegram without folder_id, documented
    # as "all dialogs (including those from folders)"; folder=1 is the
    # Archive. Scan both when asked and dedupe, so archived chats are found
    # whichever way the server answers. Dialog.folder_id == 1 marks archived.
    folders = [None, 1] if include_archived else [None]
    dialogs = []
    seen: dict[int, int] = {}
    for folder in folders:
        async for dialog in client.iter_dialogs(limit=limit, folder=folder):
            if dialog.id in seen:
                if folder == 1:
                    dialogs[seen[dialog.id]] = dialog  # carries folder_id for sure
                continue
            seen[dialog.id] = len(dialogs)
            dialogs.append(dialog)

    new_convs: list[tuple[int, str, int]] = []
    mention_convs: list[tuple[int, int]] = []
    avatar_targets: list[tuple] = []
    async with AsyncSessionLocal() as db:
        acc = await ids.account_id(db)
        known = await crud.existing_external_ids(db, acc, [ids.peer_key(d.id) for d in dialogs])
        for dialog in dialogs:
            entity = dialog.entity
            name = display_name(entity, dialog.id, dialog.name)
            last_msg_date = getattr(dialog, "date", None)
            if last_msg_date is not None:
                last_msg_date = last_msg_date.replace(tzinfo=None)
            conv = await ids.upsert_dialog(
                db, dialog.id, entity,
                title=name,
                last_message_at=last_msg_date,
                unread_count=dialog.unread_count,
                is_archived=getattr(dialog, "folder_id", None) == 1,
                commit=False,
            )
            if ids.peer_key(dialog.id) not in known:
                known.add(ids.peer_key(dialog.id))
                # Only a recent unread counts as "incoming" — a full dialog
                # sweep must not notify for months-old unread chats.
                fresh = last_msg_date is not None and last_msg_date > datetime.utcnow() - timedelta(minutes=10)
                new_convs.append((conv.id, name, (dialog.unread_count or 0) if fresh else 0))
            unread_mentions = getattr(dialog, "unread_mentions_count", 0) or 0
            if unread_mentions > 0:
                mention_convs.append((conv.id, unread_mentions))
            if getattr(entity, "photo", None):
                avatar_targets.append((entity, conv.id, conv.peer_identity_id))
        await db.commit()
    if limit is None and include_archived:
        global last_full_dialog_sync
        last_full_dialog_sync = datetime.utcnow()

    if new_convs:
        logger.info("Discovered %d new conversations during dialog sync", len(new_convs))
        await broadcast_conversations_synced(cid for cid, _, _ in new_convs)
        # A new conversation with unreads behaves like an incoming message (notification)
        for cid, name, unread in new_convs:
            if unread > 0:
                await ws_manager.broadcast({
                    "type": "new_message",
                    "conversation_id": cid,
                    "message_id": None,
                    "conversation_title": name,
                    "sender_name": name,
                    "text": "",
                    "is_tracked": False,
                    "is_outgoing": False,
                })

    # Deep-sync conversations with unread mentions so the mention messages are stored
    changed = []
    for cid, count in mention_convs:
        res = await coordinator.sync_conversation(cid, max(100, count * 5), max_wait=60)
        if res.flood_wait:
            logger.warning("Mention sync: FloodWait %ss — deferring remaining conversations", res.flood_wait)
            break
        if res.changed:
            changed.append(cid)
    await broadcast_conversations_synced(changed)

    for entity, conv_id, peer_identity_id in avatar_targets:
        if isinstance(entity, User) and peer_identity_id is not None:
            await download_avatar(client, entity, identity_id=peer_identity_id)
        else:
            await download_avatar(client, entity, conversation_id=conv_id)

    return [cid for cid, _, _ in new_convs]


async def resolve_numeric_names() -> int:
    """Re-resolve direct conversations still titled with a numeric id."""
    client = await runtime.client(require=False)
    if not client:
        return 0

    updated = 0
    async with AsyncSessionLocal() as db:
        acc = await ids.account_id(db)
        direct = (await db.execute(
            select(Conversation).where(Conversation.account_id == acc, Conversation.kind == "direct")
        )).scalars().all()
        for conv in [c for c in direct if c.title and c.title.isdigit()]:
            peer = ids.peer_id_of(conv)
            try:
                entity = await client.get_entity(peer)
            except Exception:
                continue  # deleted account etc.
            name = display_name(entity, peer)
            if name != conv.title:
                await ids.upsert_dialog(db, peer, entity, title=name, commit=False)
                updated += 1
        if updated:
            await db.commit()

    logger.info("Resolved %d numeric conversation names", updated)
    return updated


async def sync_global_mentions(days: int = 7) -> int:
    """Search Telegram globally for messages mentioning the current user and
    store them. Returns the number of mention messages saved."""
    client = await runtime.client(require=False)
    if not client:
        return 0
    try:
        me = await client.get_me()
    except Exception:
        return 0
    username = me.username if me else None
    if not username:
        return 0

    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

    try:
        result = await client(SearchGlobalRequest(
            q=f"@{username}",
            filter=InputMessagesFilterEmpty(),
            min_date=datetime.utcnow() - timedelta(days=days),
            max_date=None,
            offset_rate=0,
            offset_peer=InputPeerEmpty(),
            offset_id=0,
            limit=100,
        ))
    except Exception as e:
        logger.warning("Global mention search failed: %s", e)
        return 0

    entities = {}
    for c in getattr(result, "chats", []):
        entities[tl_utils.get_peer_id(c)] = c
    for u in getattr(result, "users", []):
        entities[u.id] = u

    by_peer: dict[int, list] = {}
    for msg in getattr(result, "messages", []):
        if not msg.peer_id or not is_storable(msg):
            continue
        peer = tl_utils.get_peer_id(msg.peer_id)
        if peer not in entities:
            continue  # no entity to create the conversation from
        from_id = getattr(msg, "from_id", None)
        sender = entities.get(tl_utils.get_peer_id(from_id)) if from_id else None
        if sender is not None:
            msg._sender = sender  # Telethon's msg.sender reads _sender
        by_peer.setdefault(peer, []).append(msg)

    stored = 0
    if by_peer:
        async with AsyncSessionLocal() as db:
            for peer, msgs in by_peer.items():
                entity = entities[peer]
                conv = await ids.conversation_for(db, peer) or await ids.upsert_dialog(db, peer, entity)
                rows = await rows_for(db, conv.id, msgs, forum=is_forum(entity))
                await crud.save_messages(db, rows)
                stored += len(rows)
    logger.info("Global mention search: stored %d messages for @%s", stored, username)
    return stored


# ---------------------------------------------------------------------------
# Bulk / tag sync
# ---------------------------------------------------------------------------

async def bulk_sync(n_conversations: int = 50, messages_per_conversation: int = 30) -> int:
    """Sync recent history for the top ``n_conversations`` dialogs ∪ tracked ∪
    briefing conversations. Broadcasts one ``conversations_synced`` for the
    ones that changed. Returns the number synced successfully."""
    client = await runtime.client(require=False)
    if not client:
        return 0

    peers: list[int] = []
    try:
        async for dialog in client.iter_dialogs(limit=n_conversations):
            peers.append(dialog.id)
    except Exception:
        logger.warning("bulk_sync: listing dialogs failed", exc_info=True)

    async with AsyncSessionLocal() as db:
        acc = await ids.account_id(db)
        by_peer = await ids.conversation_ids_for(db, peers)
        candidates = [by_peer[p] for p in peers if p in by_peer]
        flagged = (await db.execute(
            select(Conversation.id).where(
                Conversation.account_id == acc,
                (Conversation.is_tracked == True) | (Conversation.is_briefing == True),  # noqa: E712
            )
        )).scalars().all()
        for cid in flagged:
            if cid not in candidates:
                candidates.append(cid)
        skip = set((await db.execute(
            select(Conversation.id).where(
                Conversation.id.in_(candidates),
                (Conversation.is_hidden == True) | (Conversation.is_deactivated == True),  # noqa: E712
            )
        )).scalars().all())

    synced = 0
    changed: list[int] = []
    for cid in candidates:
        if cid in skip:
            continue
        res = await coordinator.sync_conversation(cid, messages_per_conversation, max_wait=60)
        if res.flood_wait:
            # Continuing after a long flood wait escalates the penalty.
            logger.warning("bulk_sync: FloodWait %ss — stopping early (%d synced)", res.flood_wait, synced)
            break
        if res.completed:
            synced += 1
            if res.changed:
                changed.append(cid)

    await broadcast_conversations_synced(changed)
    await ws_manager.broadcast({"type": "bulk_sync_complete", "synced": synced})
    return synced


_tag_sync_task: Optional["asyncio.Task"] = None
_tag_sync_state: dict = {}


def get_tag_sync_state() -> dict:
    """Current (or last finished) tag-sync run state; empty dict if never run."""
    return dict(_tag_sync_state)


async def _run_tag_sync(tag_name: str, conversation_ids: list[int], messages_per_conversation: int) -> None:
    global _tag_sync_task
    synced = 0
    changed: list[int] = []
    try:
        async with AsyncSessionLocal() as db:
            peers = await ids.peer_ids_for(db, conversation_ids)
        for cid in conversation_ids:
            peer = peers.get(cid)
            if peer is None:
                _tag_sync_state["skipped"] = _tag_sync_state.get("skipped", 0) + 1
                continue  # not a Telegram conversation
            client = await runtime.client(require=False)
            try:
                entity = await client.get_entity(peer) if client else None
            except Exception:
                entity = None  # resolution failure is reported by the sync below
            if entity is not None and is_deactivated(entity):
                async with AsyncSessionLocal() as db:
                    await db.execute(update(Conversation).where(Conversation.id == cid).values(is_deactivated=True))
                    await db.commit()
                _tag_sync_state["skipped"] = _tag_sync_state.get("skipped", 0) + 1
                continue

            for _attempt in range(4):
                res = await coordinator.sync_conversation(cid, messages_per_conversation)
                if not res.flood_wait:
                    break
                wait = min(res.flood_wait + 5, 3600)
                _tag_sync_state["waiting_until"] = (datetime.utcnow() + timedelta(seconds=wait)).isoformat() + "Z"
                logger.info("tag sync '%s': FloodWait %ss on %s, sleeping %ss", tag_name, res.flood_wait, cid, wait)
                await asyncio.sleep(wait)
                _tag_sync_state.pop("waiting_until", None)
            if res.completed:
                synced += 1
                _tag_sync_state["done"] = synced
                if res.changed:
                    changed.append(cid)
            else:
                logger.warning("tag sync '%s': conversation %s %s: %s", tag_name, cid, res.status, res.error)
                _tag_sync_state["failed"] = _tag_sync_state.get("failed", 0) + 1
    finally:
        _tag_sync_state["finished"] = True
        _tag_sync_state.pop("waiting_until", None)
        _tag_sync_task = None
        await broadcast_conversations_synced(changed)
        await ws_manager.broadcast({"type": "bulk_sync_complete", "synced": synced})


def start_tag_sync(tag_name: str, conversation_ids: list[int], messages_per_conversation: int = 50) -> dict:
    """Start a background sync of the given conversations unless one is
    already running. Returns the run state; check 'already_running'."""
    global _tag_sync_task, _tag_sync_state
    from backend.tasks import spawn

    if _tag_sync_task is not None and not _tag_sync_task.done():
        return {**_tag_sync_state, "already_running": True}
    _tag_sync_state = {
        "tag_name": tag_name, "total": len(conversation_ids), "done": 0, "failed": 0, "finished": False,
    }
    _tag_sync_task = spawn(_run_tag_sync(tag_name, conversation_ids, messages_per_conversation), name="tag_sync")
    return {**_tag_sync_state, "already_running": False}


def cancel_tag_sync() -> None:
    if _tag_sync_task is not None and not _tag_sync_task.done():
        _tag_sync_task.cancel()


async def sync_conversations_since(conversation_ids: list[int], since: datetime, max_per_conversation: int = 1000) -> dict:
    """Sync ``conversation_ids`` back to ``since`` (naive UTC) before a
    briefing. Returns {conversation_id: SyncResult}."""
    return await _sync_since(conversation_ids, since, max_per_conversation)


# ---------------------------------------------------------------------------
# Live update handlers
# ---------------------------------------------------------------------------

async def _conversation_for_event(db, event, msg) -> Conversation:
    """The conversation of an incoming event, created if new."""
    conv = await ids.conversation_for(db, event.chat_id)
    if conv is not None:
        return conv
    entity = await event.get_chat()
    conv = await ids.upsert_dialog(
        db, event.chat_id, entity,
        last_message_at=msg.date.replace(tzinfo=None) if msg.date else None,
    )
    logger.info("Auto-created conversation %s (%s) from incoming message", conv.title, conv.id)
    return conv


async def _sync_after_service_message(peer_id: int) -> None:
    async with AsyncSessionLocal() as db:
        conv = await ids.conversation_for(db, peer_id)
    if conv is None:
        return  # unknown chat: dialog sync will pick it up
    res = await coordinator.sync_conversation(conv.id, 20, max_wait=60)
    if res.changed:
        await broadcast_conversations_synced([conv.id])


def _schedule_service_sync(peer_id: int) -> None:
    from backend.jobs import run_single
    run_single(f"service_sync:{peer_id}", lambda: _sync_after_service_message(peer_id))


async def _store_event_message(db, conv: Conversation, msg) -> Optional[Message]:
    rows = await rows_for(db, conv.id, [msg], forum=conv.is_forum)
    if not rows:
        return None
    return await crud.save_message(db, **rows[0])


def register_event_handlers(client) -> list:
    """Register Telethon event handlers on ``client``.

    Returns ``[(callback, event_builder), …]`` so the runtime can remove
    exactly these handlers on stop. Only ``backend.telegram.runtime`` calls
    this, once per client.
    """
    from telethon.tl.types import (
        MessageService, PeerChannel, UpdateMessageReactions, UpdateNewChannelMessage, UpdateNewMessage,
        UpdatePinnedChannelMessages, UpdatePinnedMessages,
    )

    async def on_new_message(event):
        msg = event.message
        if not is_storable(msg):
            return
        sender = await msg.get_sender() if msg.sender_id else None
        async with AsyncSessionLocal() as db:
            conv = await _conversation_for_event(db, event, msg)
            saved = await _store_event_message(db, conv, msg)
            if saved is None:
                logger.error("Failed to persist incoming message %s in conversation %s", msg.id, conv.id)
                return
            photo_attachment = next(
                (a.id for a in saved.attachments if a.kind == "photo" and not a.local_path), None
            )

        profile = None
        if isinstance(sender, User):
            profile = {"bio": getattr(sender, "about", None), "is_contact": getattr(sender, "contact", None)}
        await fanout.message_received(conv, saved, sender_profile=profile)

        if media_type(msg) == "photo" and photo_attachment is not None:
            from backend.telegram.media import enqueue_photo
            enqueue_photo(conv.id, photo_attachment, msg)

    async def on_message_edited(event):
        msg = event.message
        if not is_storable(msg):
            return
        if msg.sender_id:
            await msg.get_sender()
        async with AsyncSessionLocal() as db:
            conv = await ids.conversation_for(db, event.chat_id)
            if conv is None:
                return
            old = await crud.get_message_by_external(db, conv.id, ids.message_key(msg.id))
            old_text = old.text if old is not None else None
            saved = await _store_event_message(db, conv, msg)
        if saved is None or saved._change is None:
            return
        await fanout.message_edited(conv.id, saved, text_changed=saved.text != old_text)

    async def on_raw_update(update_obj):
        if isinstance(update_obj, (UpdateNewMessage, UpdateNewChannelMessage)):
            # NewMessage skips MessageService (joins, calls, pins…) and the raw
            # update has no resolved entities: fetch the conversation's newest
            # page through the coordinator so the service row is stored like
            # any history row (single-flight per peer collapses bursts).
            msg = update_obj.message
            if isinstance(msg, MessageService) and msg.peer_id is not None:
                _schedule_service_sync(tl_utils.get_peer_id(msg.peer_id))
            return
        if isinstance(update_obj, UpdateMessageReactions):
            peer = tl_utils.get_peer_id(update_obj.peer)
            key = ids.message_key(update_obj.msg_id)
            async with AsyncSessionLocal() as db:
                conv = await ids.conversation_for(db, peer)
                if conv is None:
                    return
                await db.execute(
                    update(Message)
                    .where(Message.conversation_id == conv.id, Message.external_id == key)
                    .values(reactions_json=extract_reactions(update_obj))
                )
                await db.commit()
                saved = (await db.execute(
                    select(Message)
                    .where(Message.conversation_id == conv.id, Message.external_id == key)
                    .execution_options(populate_existing=True)
                )).scalar_one_or_none()
            if saved is not None:
                await fanout.message_edited(conv.id, saved, text_changed=False)
        elif isinstance(update_obj, (UpdatePinnedMessages, UpdatePinnedChannelMessages)):
            if isinstance(update_obj, UpdatePinnedChannelMessages):
                peer = tl_utils.get_peer_id(PeerChannel(update_obj.channel_id))
            else:
                peer = tl_utils.get_peer_id(update_obj.peer)
            pinned = bool(update_obj.pinned)
            async with AsyncSessionLocal() as db:
                conv = await ids.conversation_for(db, peer)
                if conv is None:
                    return
                coordinator.invalidate_pinned(conv.id)
                by_key = await crud.message_ids_by_external(
                    db, conv.id, [ids.message_key(i) for i in update_obj.messages]
                )
                message_ids = sorted(by_key.values())
                if message_ids:
                    await db.execute(update(Message).where(Message.id.in_(message_ids)).values(is_pinned=pinned))
                    if pinned:
                        newest = by_key[max(by_key, key=int)]  # highest Telegram id
                        await db.execute(
                            update(Conversation).where(Conversation.id == conv.id).values(pinned_message_id=newest)
                        )
                    else:
                        await db.execute(
                            update(Conversation)
                            .where(Conversation.id == conv.id, Conversation.pinned_message_id.in_(message_ids))
                            .values(pinned_message_id=None)
                        )
                    await db.commit()
            await ws_manager.broadcast({
                "type": "messages_pinned", "conversation_id": conv.id, "message_ids": message_ids, "pinned": pinned,
            })

    async def on_message_deleted(event):
        """Remove deleted messages from the local DB and notify the frontend."""
        deleted = [ids.message_key(i) for i in event.deleted_ids or []]
        peer = getattr(event, "chat_id", None)
        if not deleted:
            return
        from sqlalchemy import delete as sa_delete

        async with AsyncSessionLocal() as db:
            acc = await ids.account_id(db)
            if peer:
                scope = Conversation.external_id == ids.peer_key(peer)
            else:
                # No chat context: these ids live in the account-global
                # namespace shared by private chats and basic groups ONLY.
                # Channel/supergroup ids (marked peer < -10**12) are
                # per-channel and must never be deleted by a bare-id match.
                scope = cast(Conversation.external_id, BigInteger) > CHANNEL_PEER_FLOOR
            rows = (await db.execute(
                select(Message.id, Message.conversation_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Conversation.account_id == acc, scope, Message.external_id.in_(deleted))
            )).all()
            if rows:
                await db.execute(sa_delete(Message).where(Message.id.in_([r[0] for r in rows])))
                await db.commit()
        by_conv: dict[int, list[int]] = {}
        for mid, cid in rows:
            by_conv.setdefault(cid, []).append(mid)
        logger.info("Deleted %d messages (peer=%s)", len(rows), peer)
        for cid, mids in by_conv.items():
            await ws_manager.broadcast({"type": "messages_deleted", "conversation_id": cid, "message_ids": sorted(mids)})

    async def on_user_update(event):
        """Typing indicators and online status changes."""
        async with AsyncSessionLocal() as db:
            ident = await ids.identity_for_user_id(db, event.user_id, create=False)
            conv = await ids.conversation_for(db, event.chat_id or event.user_id) if event.typing else None
        if ident is None:
            return  # someone never stored
        if event.typing:
            if conv is None:
                return
            try:
                name = sender_name(await client.get_entity(event.user_id)) or ident.display_name
            except Exception:
                name = ident.display_name
            await ws_manager.broadcast({
                "type": "user_typing",
                "identity_id": ident.id,
                "conversation_id": conv.id,
                "name": name,
            })
        elif hasattr(event, "online") or hasattr(event, "status"):
            status_info = _extract_user_status(event)
            if status_info:
                await ws_manager.broadcast({"type": "user_status", "identity_id": ident.id, **status_info})

    handlers = [
        (on_new_message, events.NewMessage()),
        (on_message_edited, events.MessageEdited()),
        (on_raw_update, events.Raw(types=[
            UpdateMessageReactions, UpdatePinnedMessages, UpdatePinnedChannelMessages,
            UpdateNewMessage, UpdateNewChannelMessage,
        ])),
        (on_message_deleted, events.MessageDeleted()),
        (on_user_update, events.UserUpdate()),
    ]
    for callback, builder in handlers:
        client.add_event_handler(callback, builder)
    return handlers


def _extract_user_status(event) -> Optional[dict]:
    """Extract online/offline/last-seen info from a UserUpdate event."""
    status = getattr(event, "status", None)
    if status is None and hasattr(event, "online"):
        return {"status": "online" if event.online else "offline"}
    return status_info(status)


def status_info(status) -> Optional[dict]:
    """{status, last_seen?} for a Telethon UserStatus (None if unknown)."""
    from telethon.tl.types import (
        UserStatusOnline,
        UserStatusOffline,
        UserStatusRecently,
        UserStatusLastWeek,
        UserStatusLastMonth,
    )
    if isinstance(status, UserStatusOnline):
        return {"status": "online"}
    if isinstance(status, UserStatusOffline):
        last_seen = iso_utc(status.was_online)
        return {"status": "offline", "last_seen": last_seen}
    if isinstance(status, UserStatusRecently):
        return {"status": "recently"}
    if isinstance(status, UserStatusLastWeek):
        return {"status": "last_week"}
    if isinstance(status, UserStatusLastMonth):
        return {"status": "last_month"}
    return None
