"""Telegram conversations beyond messages: the user's dialog-list state,
members and presence, forum topics, global search and the address book.

Called by ``TelegramConnector``; arguments are stored rows.
"""

import calendar
import logging
from datetime import datetime
from typing import Optional

from telethon import utils
from telethon.errors import FloodWaitError

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation
from backend.serialization import iso_utc
from backend.telegram import client as tg_client, ids
from backend.telegram.coordinator import coordinator
from backend.telegram.entities import display_name
from backend.telegram.sync import status_info

logger = logging.getLogger(__name__)


# ── dialog-list state (best effort: skipped while offline) ───────────────────

async def _best_effort_client():
    return await tg_client.connected_client(require=False)


async def set_dialog_pinned(conv: Conversation, pinned: bool) -> None:
    from telethon.tl.functions.messages import ToggleDialogPinRequest
    from telethon.tl.types import InputDialogPeer

    client = await _best_effort_client()
    if client is None:
        return
    entity = await client.get_input_entity(ids.peer_id_of(conv))
    await client(ToggleDialogPinRequest(peer=InputDialogPeer(peer=entity), pinned=pinned))


async def set_dialog_archived(conv: Conversation, archived: bool) -> None:
    from telethon.tl.functions.folders import EditPeerFoldersRequest
    from telethon.tl.types import InputFolderPeer

    client = await _best_effort_client()
    if client is None:
        return
    folder_id = 1 if archived else 0  # 1 = Archive folder
    entity = await client.get_input_entity(ids.peer_id_of(conv))
    await client(EditPeerFoldersRequest(folder_peers=[InputFolderPeer(peer=entity, folder_id=folder_id)]))


async def set_dialog_muted(conv: Conversation, muted: bool, until: Optional[datetime]) -> None:
    from telethon.tl.functions.account import UpdateNotifySettingsRequest
    from telethon.tl.types import InputNotifyPeer, InputPeerNotifySettings

    client = await _best_effort_client()
    if client is None:
        return
    # ``until`` is naive UTC: timegm, not .timestamp() (which reads it as local time)
    mute_until = calendar.timegm((until or datetime(2038, 1, 1)).timetuple()) if muted else 0
    entity = await client.get_input_entity(ids.peer_id_of(conv))
    await client(UpdateNotifySettingsRequest(
        peer=InputNotifyPeer(peer=entity), settings=InputPeerNotifySettings(mute_until=mute_until),
    ))


# ── members & presence ───────────────────────────────────────────────────────

_ROLE_ORDER = {"creator": 0, "admin": 1, "member": 2}


async def list_members(conv: Conversation, limit: int) -> list[dict]:
    from telethon.tl.types import User

    client = await tg_client.connected_client()
    members = []
    async with AsyncSessionLocal() as db:
        async for user in client.iter_participants(ids.peer_id_of(conv), limit=limit):
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
    members.sort(key=lambda m: (_ROLE_ORDER.get(m["role"], 2), m["name"].lower()))
    return members


async def presence(conv: Conversation) -> dict:
    from telethon.tl.functions.users import GetFullUserRequest


    if conv.kind != "direct":
        return {"status": "unknown"}
    client = await tg_client.connected_client()
    try:
        full = await client(GetFullUserRequest(ids.peer_id_of(conv)))
        user = full.users[0] if full.users else None
        if not user:
            return {"status": "unknown"}
        return status_info(getattr(user, "status", None)) or {"status": "unknown"}
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


async def send_typing(conv: Conversation) -> None:
    from telethon.tl.functions.messages import SetTypingRequest
    from telethon.tl.types import SendMessageTypingAction

    client = await tg_client.connected_client()
    await client(SetTypingRequest(peer=ids.peer_id_of(conv), action=SendMessageTypingAction()))


# ── forum topics ─────────────────────────────────────────────────────────────

async def list_topics(conv: Conversation) -> list[dict]:
    from telethon.tl.functions.messages import GetForumTopicsRequest
    from telethon.tl.types import ForumTopic

    client = await tg_client.connected_client()
    result = await client(GetForumTopicsRequest(
        peer=ids.peer_id_of(conv), offset_date=None, offset_id=0, offset_topic=0, limit=100,
    ))
    return [
        {
            "id": topic.id,
            "title": topic.title,
            "icon_color": getattr(topic, "icon_color", None),
            "icon_emoji_id": getattr(topic, "icon_emoji_id", None),
            "date": iso_utc(topic.date),
            "is_closed": bool(getattr(topic, "closed", False)),
            "is_pinned": bool(getattr(topic, "pinned", False)),
        }
        for topic in result.topics
        if isinstance(topic, ForumTopic)  # skips ForumTopicDeleted
    ]


async def create_topic(conv: Conversation, title: str) -> Optional[int]:
    from telethon.tl.functions.messages import CreateForumTopicRequest

    client = await tg_client.connected_client()
    result = await client(CreateForumTopicRequest(peer=ids.peer_id_of(conv), title=title))
    for upd in getattr(result, "updates", []):
        msg = getattr(upd, "message", None)
        if msg:
            return getattr(msg, "id", None)
    return None


async def set_topic_closed(conv: Conversation, topic_id: int, closed: bool) -> None:
    from telethon.tl.functions.messages import EditForumTopicRequest

    client = await tg_client.connected_client()
    await client(EditForumTopicRequest(peer=ids.peer_id_of(conv), topic_id=topic_id, closed=closed))


# ── global search ────────────────────────────────────────────────────────────

async def search_messages(query: str) -> list[dict]:
    """Telegram's global message search; [] while offline, rate-limited or on
    failure (it's a best-effort addition to local search)."""
    from telethon.tl.functions.messages import SearchGlobalRequest
    from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty

    client = await tg_client.connected_client(require=False)
    if not client or not await coordinator.limiter.acquire(1, max_wait=2):
        return []
    try:
        result = await client(SearchGlobalRequest(
            q=query, filter=InputMessagesFilterEmpty(), min_date=None, max_date=None,
            offset_rate=0, offset_peer=InputPeerEmpty(), offset_id=0, limit=30,
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
    async with AsyncSessionLocal() as db:
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


# ── address book ─────────────────────────────────────────────────────────────

async def sync_address_book(configured: bool) -> Optional[dict]:
    """Import the Telegram contact list. None when not signed in;
    TelegramUnavailable (503) when signed in but unreachable."""
    from telethon.tl.functions.contacts import GetContactsRequest

    client = await tg_client.connected_client(require=False)
    if client is None:
        if not configured:
            return None
        client = await tg_client.connected_client()
    result = await client(GetContactsRequest(hash=0))
    users = list(getattr(result, "users", None) or [])

    async with AsyncSessionLocal() as db:
        before = await crud.identity_ids_by_external(db, ids.CHANNEL, [str(u.id) for u in users])
        was_contact = {
            iid for iid, ident in (await crud.get_identities(db, before.values())).items() if ident.is_contact
        }
        contact_ids = []
        for u in users:
            contact_ids.append((await ids.ensure_user_identity(db, u, is_contact=True)).id)
        await crud.set_address_book(db, ids.CHANNEL, contact_ids)
        await db.commit()
    return {"imported": len(set(contact_ids) - was_contact), "total": len(users)}
