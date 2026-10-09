import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select, text as sa_text, update
from telethon import events, utils as tl_utils
from telethon.tl.types import User

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Chat, Message
from backend.ingest import fanout
from backend.serialization import iso_utc
from backend.telegram.runtime import runtime
from backend.telegram.convert import extract_reactions, is_storable, message_to_row, media_type
from backend.telegram.coordinator import (
    broadcast_chats_synced, coordinator, sync_chats_since as _sync_chats_since,
)
from backend.telegram.entities import (
    chat_type, display_name, is_deactivated, is_forum, sender_name,
)
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------

# When the last complete enumeration (every folder, no limit) finished.
last_full_dialog_sync: Optional[datetime] = None


async def sync_dialogs(limit: Optional[int] = None, include_archived: bool = False) -> list[int]:
    """Fetch dialogs and upsert them into the chats table (one transaction).

    Args:
        limit: Max dialogs to fetch per folder. None = all dialogs.
        include_archived: Also scan the archived/contact-requests folder.

    Returns the telegram ids of chats seen for the first time.
    """
    from backend.telegram.media import download_profile_photo

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

    new_chats = []
    mention_chats = []
    async with AsyncSessionLocal() as db:
        known = await crud.get_existing_chat_ids(db, [d.id for d in dialogs])
        for dialog in dialogs:
            entity = dialog.entity
            is_user = isinstance(entity, User)
            name = display_name(entity, dialog.id, dialog.name)
            last_msg_date = getattr(dialog, "date", None)
            if last_msg_date is not None:
                last_msg_date = last_msg_date.replace(tzinfo=None)
            await crud.upsert_chat(
                db,
                telegram_id=dialog.id,
                name=name,
                chat_type=chat_type(entity),
                username=getattr(entity, "username", None),
                unread_count=dialog.unread_count,
                members_count=getattr(entity, "participants_count", None),
                is_forum=is_forum(entity),
                slow_mode_seconds=getattr(entity, "slowmode_seconds", 0) or 0,
                last_message_date=last_msg_date,
                is_deactivated=is_deactivated(entity),
                is_archived=getattr(dialog, "folder_id", None) == 1,
                is_bot=bool(getattr(entity, "bot", False)) if is_user else False,
                is_deleted_account=bool(getattr(entity, "deleted", False)) if is_user else False,
                is_self=bool(getattr(entity, "is_self", False)) if is_user else False,
                commit=False,
            )
            if dialog.id not in known:
                known.add(dialog.id)
                # Only a recent unread counts as "incoming" — a full dialog
                # sweep must not notify for months-old unread chats.
                fresh = last_msg_date is not None and last_msg_date > datetime.utcnow() - timedelta(minutes=10)
                new_chats.append((dialog.id, name, (dialog.unread_count or 0) if fresh else 0))
            unread_mentions = getattr(dialog, "unread_mentions_count", 0) or 0
            if unread_mentions > 0:
                mention_chats.append((dialog.id, unread_mentions))
        await db.commit()
    if limit is None and include_archived:
        global last_full_dialog_sync
        last_full_dialog_sync = datetime.utcnow()

    if new_chats:
        logger.info("Discovered %d new chats during dialog sync", len(new_chats))
        await broadcast_chats_synced(cid for cid, _, _ in new_chats)
        # A new chat with unreads behaves like an incoming message (notification)
        for cid, name, unread in new_chats:
            if unread > 0:
                await ws_manager.broadcast({
                    "type": "new_message",
                    "chat_id": cid,
                    "message_id": 0,
                    "chat_name": name,
                    "sender_name": name,
                    "text": "",
                    "is_tracked": False,
                    "is_outgoing": False,
                })

    # Deep-sync chats with unread mentions so the mention messages are stored
    changed = []
    for cid, count in mention_chats:
        res = await coordinator.sync_chat(cid, max(100, count * 5), max_wait=60)
        if res.flood_wait:
            logger.warning("Mention sync: FloodWait %ss — deferring remaining chats", res.flood_wait)
            break
        if res.changed:
            changed.append(cid)
    await broadcast_chats_synced(changed)

    for dialog in dialogs:
        if getattr(dialog.entity, "photo", None):
            await download_profile_photo(client, dialog.entity, dialog.id)

    return [cid for cid, _, _ in new_chats]


async def resolve_numeric_names() -> int:
    """Resolve private chats that still have numeric telegram IDs as their name."""
    client = await runtime.client(require=False)
    if not client:
        return 0

    updated = 0
    async with AsyncSessionLocal() as db:
        all_private = (await db.execute(
            select(Chat).where(Chat.type == "private")
        )).scalars().all()
        for chat in [c for c in all_private if c.name and c.name.isdigit()]:
            try:
                entity = await client.get_entity(chat.telegram_id)
            except Exception:
                continue  # deleted account etc.
            name = display_name(entity, chat.telegram_id)
            if name != chat.name:
                chat.name = name
                username = getattr(entity, "username", None)
                if username:
                    chat.username = username
                updated += 1
        if updated:
            await db.commit()

    logger.info("Resolved %d numeric chat names", updated)
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

    rows = []
    for msg in getattr(result, "messages", []):
        if not msg.peer_id or not is_storable(msg):
            continue
        chat_id = tl_utils.get_peer_id(msg.peer_id)
        from_id = getattr(msg, "from_id", None)
        sender = entities.get(tl_utils.get_peer_id(from_id)) if from_id else None
        chat_entity = entities.get(chat_id)
        rows.append(message_to_row(msg, chat_id, sender, forum=is_forum(chat_entity)))

    if rows:
        async with AsyncSessionLocal() as db:
            await crud.save_messages(db, rows)
    logger.info("Global mention search: stored %d messages for @%s", len(rows), username)
    return len(rows)


# ---------------------------------------------------------------------------
# Bulk / tag sync
# ---------------------------------------------------------------------------

async def bulk_sync(n_chats: int = 50, messages_per_chat: int = 30) -> int:
    """Sync recent history for the top ``n_chats`` dialogs ∪ tracked ∪ briefing
    chats. Broadcasts one ``chats_synced`` for the chats that changed.
    Returns the number of chats synced successfully."""
    client = await runtime.client(require=False)
    if not client:
        return 0

    candidates: list[int] = []
    try:
        async for dialog in client.iter_dialogs(limit=n_chats):
            candidates.append(dialog.id)
    except Exception:
        logger.warning("bulk_sync: listing dialogs failed", exc_info=True)

    async with AsyncSessionLocal() as db:
        flagged = (await db.execute(
            select(Chat.telegram_id).where((Chat.is_tracked == True) | (Chat.is_briefing == True))  # noqa: E712
        )).scalars().all()
        for cid in flagged:
            if cid not in candidates:
                candidates.append(cid)
        skip = set((await db.execute(
            select(Chat.telegram_id).where(
                Chat.telegram_id.in_(candidates),
                (Chat.is_hidden == True) | (Chat.is_deactivated == True),  # noqa: E712
            )
        )).scalars().all())

    synced = 0
    changed: list[int] = []
    for cid in candidates:
        if cid in skip:
            continue
        res = await coordinator.sync_chat(cid, messages_per_chat, max_wait=60)
        if res.flood_wait:
            # Continuing after a long flood wait escalates the penalty.
            logger.warning("bulk_sync: FloodWait %ss — stopping early (%d chats synced)", res.flood_wait, synced)
            break
        if res.completed:
            synced += 1
            if res.changed:
                changed.append(cid)

    await broadcast_chats_synced(changed)
    await ws_manager.broadcast({"type": "bulk_sync_complete", "synced": synced})
    return synced


_tag_sync_task: Optional["asyncio.Task"] = None
_tag_sync_state: dict = {}


def get_tag_sync_state() -> dict:
    """Current (or last finished) tag-sync run state; empty dict if never run."""
    return dict(_tag_sync_state)


async def _run_tag_sync(tag_name: str, chat_ids: list[int], messages_per_chat: int) -> None:
    global _tag_sync_task
    synced = 0
    changed: list[int] = []
    try:
        for chat_id in chat_ids:
            client = await runtime.client(require=False)
            try:
                entity = await client.get_entity(chat_id) if client else None
            except Exception:
                entity = None  # resolution failure is reported by the sync below
            if entity is not None and is_deactivated(entity):
                async with AsyncSessionLocal() as db:
                    await db.execute(update(Chat).where(Chat.telegram_id == chat_id).values(is_deactivated=True))
                    await db.commit()
                _tag_sync_state["skipped"] = _tag_sync_state.get("skipped", 0) + 1
                continue

            for _attempt in range(4):
                res = await coordinator.sync_chat(chat_id, messages_per_chat)
                if not res.flood_wait:
                    break
                wait = min(res.flood_wait + 5, 3600)
                _tag_sync_state["waiting_until"] = (datetime.utcnow() + timedelta(seconds=wait)).isoformat() + "Z"
                logger.info("tag sync '%s': FloodWait %ss on chat %s, sleeping %ss", tag_name, res.flood_wait, chat_id, wait)
                await asyncio.sleep(wait)
                _tag_sync_state.pop("waiting_until", None)
            if res.completed:
                synced += 1
                _tag_sync_state["done"] = synced
                if res.changed:
                    changed.append(chat_id)
            else:
                logger.warning("tag sync '%s': chat %s %s: %s", tag_name, chat_id, res.status, res.error)
                _tag_sync_state["failed"] = _tag_sync_state.get("failed", 0) + 1
    finally:
        _tag_sync_state["finished"] = True
        _tag_sync_state.pop("waiting_until", None)
        _tag_sync_task = None
        await broadcast_chats_synced(changed)
        await ws_manager.broadcast({"type": "bulk_sync_complete", "synced": synced})


def start_tag_sync(tag_name: str, chat_ids: list[int], messages_per_chat: int = 50) -> dict:
    """Start a background sync of the given chats unless one is already running.

    Returns the run state; check 'already_running' to distinguish.
    """
    global _tag_sync_task, _tag_sync_state
    from backend.tasks import spawn

    if _tag_sync_task is not None and not _tag_sync_task.done():
        return {**_tag_sync_state, "already_running": True}
    _tag_sync_state = {"tag_name": tag_name, "total": len(chat_ids), "done": 0, "failed": 0, "finished": False}
    _tag_sync_task = spawn(_run_tag_sync(tag_name, chat_ids, messages_per_chat), name="tag_sync")
    return {**_tag_sync_state, "already_running": False}


def cancel_tag_sync() -> None:
    if _tag_sync_task is not None and not _tag_sync_task.done():
        _tag_sync_task.cancel()


async def sync_chats_since(chat_ids: list[int], since: datetime, max_per_chat: int = 1000) -> dict:
    """Sync ``chat_ids`` back to ``since`` (naive UTC) before a briefing.
    Returns {chat_id: SyncResult}; see coordinator.sync_chats_since."""
    return await _sync_chats_since(chat_ids, since, max_per_chat)


# ---------------------------------------------------------------------------
# One-shot repair of text stored before message_to_row (migration 47)
# ---------------------------------------------------------------------------

# Markers Telethon's markdown unparse writes into ``msg.text``.
_MARKDOWN_MARKERS = ("**", "__", "~~", "`", "](")


def _possibly_garbled():
    """Rows the pre-message_to_row writers could have stored wrongly:
    - any row with text_html (entity messages: text from msg.text +
      raw-text offsets; the old Raw handler stored text_html="");
    - rows whose text carries markdown markers without HTML — compose /send
      stored ``msg.text`` (markdown-unparsed) with no text_html, and the
      entity path fell back to regex-only HTML when thtml failed.
    Must match migrations 47/48."""
    return or_(
        Message.text_html.isnot(None),
        *[Message.text.contains(m, autoescape=True) for m in _MARKDOWN_MARKERS],
    )


async def repair_garbled_text(batch: int = 100) -> int:
    """Re-fetch every stored formatted message queued by migration 47 and
    rewrite it through the upsert (raw text + entities). Chats are removed
    from ``sync_text_repair`` once done, so this runs to completion once
    across restarts. Returns the number of rows rewritten."""
    client = await runtime.client(require=False)
    if not client:
        return 0
    async with AsyncSessionLocal() as db:
        chat_ids = [r[0] for r in (await db.execute(sa_text("SELECT chat_id FROM sync_text_repair"))).all()]
    rewritten = 0
    changed_chats = []
    for chat_id in chat_ids:
        async with AsyncSessionLocal() as db:
            ids = [r[0] for r in (await db.execute(
                select(Message.tg_message_id).where(Message.chat_id == chat_id, _possibly_garbled())
            )).all()]
            chat = await crud.get_chat_by_telegram_id(db, chat_id)
            forum = bool(chat and chat.is_forum)
        try:
            for i in range(0, len(ids), batch):
                chunk = ids[i:i + batch]
                if not await coordinator.limiter.acquire(1):
                    break
                msgs = await client.get_messages(chat_id, ids=chunk)
                rows = [message_to_row(m, chat_id, forum=forum) for m in msgs if m is not None and is_storable(m)]
                async with AsyncSessionLocal() as db:
                    res = await crud.save_messages(db, rows)
                rewritten += len(res["updated"])
                if res["updated"]:
                    changed_chats.append(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            from telethon.errors import FloodWaitError
            if isinstance(e, FloodWaitError):
                coordinator.limiter.flood(e.seconds)
                logger.info("text repair paused by FloodWait %ss; resumes next start", e.seconds)
                break
            # Unresolvable/left chat: nothing more can be repaired for it.
            logger.info("text repair for chat %s abandoned: %s", chat_id, e)
        async with AsyncSessionLocal() as db:
            # The chat list preview was copied from the garbled text on insert.
            await crud.refresh_chat_preview(db, chat_id)
            await db.execute(sa_text("DELETE FROM sync_text_repair WHERE chat_id = :c"), {"c": chat_id})
            await db.commit()
    await broadcast_chats_synced(changed_chats)
    if chat_ids:
        logger.info("Text repair: rewrote %d messages across %d chats", rewritten, len(chat_ids))
    return rewritten


# ---------------------------------------------------------------------------
# Live update handlers
# ---------------------------------------------------------------------------

async def _ensure_chat_for_event(db, event, msg):
    """Return the Chat row for an incoming event, creating it if new."""
    chat = await crud.get_chat_by_telegram_id(db, event.chat_id)
    if chat is not None:
        return chat
    entity = await event.get_chat()
    chat = await crud.upsert_chat(
        db,
        event.chat_id,
        display_name(entity, event.chat_id),
        chat_type(entity),
        username=getattr(entity, "username", None),
        is_forum=is_forum(entity),
        last_message_date=msg.date.replace(tzinfo=None) if msg.date else None,
        is_deactivated=is_deactivated(entity),
    )
    logger.info("Auto-created chat %s (%s) from incoming message", chat.name, event.chat_id)
    return chat


async def _sync_after_service_message(chat_id: int) -> None:
    async with AsyncSessionLocal() as db:
        if await crud.get_chat_by_telegram_id(db, chat_id) is None:
            return  # unknown chat: dialog sync will pick it up
    res = await coordinator.sync_chat(chat_id, 20, max_wait=60)
    if res.changed:
        await broadcast_chats_synced([chat_id])


def _schedule_service_sync(chat_id: int) -> None:
    from backend.jobs import run_single
    run_single(f"service_sync:{chat_id}", lambda: _sync_after_service_message(chat_id))


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
        async with AsyncSessionLocal() as db:
            chat = await _ensure_chat_for_event(db, event, msg)
            sender = await msg.get_sender() if msg.sender_id else None
            saved = await crud.save_message(db, **message_to_row(msg, event.chat_id, sender, forum=chat.is_forum))
            if saved is None:
                logger.error("Failed to persist incoming message %s in chat %s", msg.id, event.chat_id)
                return

        profile = None
        if sender is not None:
            profile = {"bio": getattr(sender, "about", None), "is_contact": getattr(sender, "contact", None)}
        await fanout.message_received(chat, saved, sender_profile=profile)

        if media_type(msg) == "photo" and not saved.media_path:
            from backend.telegram.media import enqueue_photo
            enqueue_photo(event.chat_id, msg)

    async def on_message_edited(event):
        msg = event.message
        if not is_storable(msg):
            return
        async with AsyncSessionLocal() as db:
            chat = await crud.get_chat_by_telegram_id(db, event.chat_id)
            if chat is None:
                return
            old_text = (await db.execute(
                select(Message.text).where(Message.chat_id == event.chat_id, Message.tg_message_id == msg.id)
            )).scalar()
            sender = await msg.get_sender() if msg.sender_id else None
            saved = await crud.save_message(db, **message_to_row(msg, event.chat_id, sender, forum=chat.is_forum))
        if saved is None or saved._change is None:
            return
        await fanout.message_edited(event.chat_id, saved, text_changed=saved.text != old_text)

    async def on_raw_update(update_obj):
        if isinstance(update_obj, (UpdateNewMessage, UpdateNewChannelMessage)):
            # NewMessage skips MessageService (joins, calls, pins…) and the raw
            # update has no resolved entities: fetch the chat's newest page
            # through the coordinator so the service row is stored like any
            # history row (single-flight per chat collapses bursts).
            msg = update_obj.message
            if isinstance(msg, MessageService) and msg.peer_id is not None:
                _schedule_service_sync(tl_utils.get_peer_id(msg.peer_id))
            return
        if isinstance(update_obj, UpdateMessageReactions):
            chat_id = tl_utils.get_peer_id(update_obj.peer)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Message)
                    .where(Message.chat_id == chat_id, Message.tg_message_id == update_obj.msg_id)
                    .values(reactions_json=extract_reactions(update_obj))
                )
                await db.commit()
                saved = (await db.execute(
                    select(Message).where(Message.chat_id == chat_id, Message.tg_message_id == update_obj.msg_id)
                )).scalars().first()
            if saved is not None:
                await fanout.message_edited(chat_id, saved, text_changed=False)
        elif isinstance(update_obj, (UpdatePinnedMessages, UpdatePinnedChannelMessages)):
            if isinstance(update_obj, UpdatePinnedChannelMessages):
                chat_id = tl_utils.get_peer_id(PeerChannel(update_obj.channel_id))
            else:
                chat_id = tl_utils.get_peer_id(update_obj.peer)
            ids = list(update_obj.messages)
            pinned = bool(update_obj.pinned)
            coordinator.invalidate_pinned(chat_id)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Message)
                    .where(Message.chat_id == chat_id, Message.tg_message_id.in_(ids))
                    .values(is_pinned=pinned)
                )
                if pinned:
                    await db.execute(update(Chat).where(Chat.telegram_id == chat_id).values(pinned_message_id=max(ids)))
                else:
                    await db.execute(
                        update(Chat)
                        .where(Chat.telegram_id == chat_id, Chat.pinned_message_id.in_(ids))
                        .values(pinned_message_id=None)
                    )
                await db.commit()
            await ws_manager.broadcast({
                "type": "messages_pinned", "chat_id": chat_id, "message_ids": ids, "pinned": pinned,
            })

    async def on_message_deleted(event):
        """Remove deleted messages from the local DB and notify the frontend."""
        deleted_ids = event.deleted_ids
        chat_id = getattr(event, "chat_id", None)
        if not deleted_ids:
            return
        from sqlalchemy import delete as sa_delete

        async with AsyncSessionLocal() as db:
            if chat_id:
                await db.execute(sa_delete(Message).where(
                    Message.chat_id == chat_id, Message.tg_message_id.in_(deleted_ids),
                ))
            else:
                # No chat context: these ids live in the account-global namespace
                # shared by private chats and basic groups ONLY. Channel/supergroup
                # ids are per-channel and must never be deleted by a bare-id match.
                await db.execute(sa_delete(Message).where(
                    Message.chat_id > -1000000000000, Message.tg_message_id.in_(deleted_ids),
                ))
            await db.commit()
        logger.info("Deleted %d messages (chat_id=%s)", len(deleted_ids), chat_id)
        await ws_manager.broadcast({
            "type": "messages_deleted", "chat_id": chat_id, "message_ids": deleted_ids,
        })

    async def on_user_update(event):
        """Typing indicators and online status changes."""
        if event.typing:
            user_id = event.user_id
            try:
                user_name = sender_name(await client.get_entity(user_id)) or str(user_id)
            except Exception:
                user_name = str(user_id)
            await ws_manager.broadcast({
                "type": "user_typing",
                "user_id": user_id,
                "chat_id": event.chat_id or user_id,
                "user_name": user_name,
            })
        elif hasattr(event, "online") or hasattr(event, "status"):
            status_info = _extract_user_status(event)
            if status_info:
                await ws_manager.broadcast({"type": "user_status", "user_id": event.user_id, **status_info})

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
    from telethon.tl.types import (
        UserStatusOnline,
        UserStatusOffline,
        UserStatusRecently,
        UserStatusLastWeek,
        UserStatusLastMonth,
    )
    status = getattr(event, "status", None)
    if status is None and hasattr(event, "online"):
        return {"status": "online" if event.online else "offline"}
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

