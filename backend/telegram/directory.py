"""Telegram's directory and chat management: folders, global directory
search, resolving peers / usernames / user ids, creating and joining chats,
and live user profiles.

Used by the Telegram-only API router (``api/telegram.py``); everything comes
back as stored rows (or plain dicts) keyed by internal ids.
"""

import logging
import time
from typing import Optional, Union

from telethon import utils as tl_utils
from telethon.errors import FloodWaitError
from telethon.tl.types import User

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Identity
from backend.errors import NotFound
from backend.serializers import media_url
from backend.telegram import client as tg_client, ids
from backend.telegram.coordinator import coordinator
from backend.telegram.entities import display_name, is_deactivated
from backend.telegram.sync import status_info

logger = logging.getLogger(__name__)


class ProfileNotFound(Exception):
    pass


async def person_user_id(db, person_id: int) -> Optional[int]:
    """The Telegram user id of a person (their first Telegram identity)."""
    for ident in await crud.identities_for_person(db, person_id):
        if ident.channel == ids.CHANNEL:
            return int(ident.external_id)
    return None


# ── folders ──────────────────────────────────────────────────────────────────

async def folders(db) -> list[dict]:
    """The user's chat folders, members as conversation ids ([] offline)."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    from telethon.tl.types import DialogFilter

    client = await tg_client.connected_client(require=False)
    if not client:
        return []
    try:
        response = await client(GetDialogFiltersRequest())
    except Exception as e:
        logger.warning("Failed to fetch dialog folders: %s", e)
        return []
    filters = response.filters if hasattr(response, "filters") else response
    dialog_filters = [f for f in filters if isinstance(f, DialogFilter)]  # skip default / chatlist
    peers: set[int] = set()
    for f in dialog_filters:
        for peer in f.include_peers:
            try:
                peers.add(tl_utils.get_peer_id(peer))
            except Exception:
                pass
    by_peer = await ids.conversation_ids_for(db, peers)
    result = []
    for f in dialog_filters:
        conversation_ids = []
        for peer in f.include_peers:
            try:
                cid = by_peer.get(tl_utils.get_peer_id(peer))
            except Exception:
                cid = None
            if cid is not None:
                conversation_ids.append(cid)
        result.append({
            "id": f.id,
            "title": f.title.text if hasattr(f.title, "text") else str(f.title),
            "conversation_ids": conversation_ids,
            "include_contacts": f.contacts,
            "include_non_contacts": f.non_contacts,
            "include_groups": f.groups,
            "include_channels": f.broadcasts,
            "include_bots": f.bots,
        })
    return result


# ── directory search / open ──────────────────────────────────────────────────

def _directory_hit(peer_id: int, entity) -> dict:
    """A directory result that isn't stored yet: open it via
    POST /conversations/ensure {peer_id}."""
    return {
        "id": None,
        "peer_id": peer_id,
        "channel": ids.CHANNEL,
        "title": display_name(entity, peer_id),
        "kind": ids.conversation_kind(entity),
        "username": getattr(entity, "username", None),
    }


async def search_directory(db, query: str, seen: set[int]) -> list[Union[Conversation, dict]]:
    """Telegram's global directory (people / groups / channels) for
    ``query``, skipping peers in ``seen``: stored conversations, or hit dicts
    for unstored ones. One RPC; [] offline or rate-limited (typeahead must
    never queue behind a FloodWait)."""
    client = await tg_client.connected_client(require=False)
    if not client or not await coordinator.limiter.acquire(1, max_wait=2):
        return []
    try:
        from telethon.tl.functions.contacts import SearchRequest
        res = await client(SearchRequest(q=query, limit=20))
    except FloodWaitError as e:
        coordinator.limiter.flood(e.seconds)
        logger.info("Telegram directory search rate-limited (%ss)", e.seconds)
        return []
    except Exception as e:
        logger.warning("Telegram directory search failed: %s", e)
        return []
    entities = [(u.id, u) for u in res.users] + [(tl_utils.get_peer_id(c), c) for c in res.chats]
    stored = await ids.conversation_ids_for(db, [p for p, _ in entities if p not in seen])
    stored_convs = await crud.get_conversations(db, stored.values())
    found: list[Union[Conversation, dict]] = []
    seen = set(seen)
    for peer, entity in entities:
        if peer in seen:
            continue
        seen.add(peer)
        found.append(stored_convs[stored[peer]] if peer in stored else _directory_hit(peer, entity))
    return found


async def ensure_conversation(db, peer_id: int) -> Conversation:
    """The conversation for a peer, created from Telegram if new."""
    conv = await ids.conversation_for(db, peer_id)
    if conv is not None:
        return conv
    client = await tg_client.connected_client()
    try:
        entity = await client.get_entity(peer_id)
    except Exception as e:
        raise NotFound(f"Could not resolve Telegram chat: {e}")
    return await ids.upsert_dialog(db, tl_utils.get_peer_id(entity), entity)


# ── create / join ────────────────────────────────────────────────────────────

class NoMembers(ValueError):
    """None of the requested members could be resolved."""


async def create_group(db, name: str, user_ids: list[int]) -> Conversation:
    """Raises NoMembers when no user resolves; other errors are Telegram's."""
    client = await tg_client.connected_client()
    users = []
    for uid in user_ids:
        try:
            users.append(await client.get_entity(uid))
        except Exception:
            pass  # skip unresolvable people
    if not users:
        raise NoMembers("No valid users could be resolved")
    from telethon.tl.functions.messages import CreateChatRequest
    result = await client(CreateChatRequest(users=users, title=name))
    created = result.chats[0] if getattr(result, "chats", None) else result.updates.chats[0]
    return await ids.upsert_dialog(db, tl_utils.get_peer_id(created), created, title=name)


async def create_channel(db, name: str, about: str) -> Conversation:
    from telethon.tl.functions.channels import CreateChannelRequest

    client = await tg_client.connected_client()
    result = await client(CreateChannelRequest(title=name, about=about, megagroup=False, broadcast=True))
    created = result.chats[0]
    return await ids.upsert_dialog(db, tl_utils.get_peer_id(created), created, title=name)


_INVITE_PREFIXES = ("https://t.me/joinchat/", "https://t.me/+", "t.me/joinchat/", "t.me/+")


def public_username(link: str) -> str:
    return link.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").lstrip("@")


async def join(db, link: str) -> Conversation:
    """Join via an invite link (t.me/joinchat/…, t.me/+…) or a public
    username (t.me/name, @name; must be non-empty)."""
    client = await tg_client.connected_client()
    invite_hash = next((link[len(p):] for p in _INVITE_PREFIXES if link.startswith(p)), None)
    if invite_hash:
        from telethon.tl.functions.messages import ImportChatInviteRequest
        joined = (await client(ImportChatInviteRequest(hash=invite_hash))).chats[0]
    else:
        from telethon.tl.functions.channels import JoinChannelRequest
        entity = await client.get_entity(public_username(link))
        result = await client(JoinChannelRequest(entity))
        joined = result.chats[0] if result.chats else entity
    return await ids.upsert_dialog(db, tl_utils.get_peer_id(joined), joined, is_deactivated=is_deactivated(joined))


# ── people resolution ────────────────────────────────────────────────────────

async def identity_for_user(db, user_id: int) -> Optional[Identity]:
    """The identity of a Telegram user id, recorded on first sight; None when
    neither stored nor resolvable (no placeholder is created)."""
    ident = await ids.identity_for_user_id(db, user_id, create=False)
    if ident is not None:
        return ident
    client = await tg_client.connected_client(require=False)
    entity = None
    if client is not None:
        try:
            entity = await client.get_entity(user_id)
        except Exception:
            entity = None
    if not isinstance(entity, User):
        return None
    ident = await ids.ensure_user_identity(db, entity)
    await db.commit()
    return ident


async def resolve_username(db, name: str) -> dict:
    """A user → their identity / person (and stored direct conversation); a
    group/channel → its conversation. NotFound when Telegram has no such name."""
    client = await tg_client.connected_client()
    try:
        entity = await client.get_entity(name)
    except (ValueError, TypeError) as e:
        raise NotFound(f"Not found: {e}")
    if isinstance(entity, User):
        ident = await ids.ensure_user_identity(db, entity)
        await db.commit()
        conv = await ids.conversation_for(db, entity.id)
        return {"person_id": ident.person_id, "identity_id": ident.id, "conversation_id": conv.id if conv else None}
    peer = tl_utils.get_peer_id(entity)
    conv = await ids.conversation_for(db, peer) or await ids.upsert_dialog(db, peer, entity)
    return {"person_id": None, "identity_id": None, "conversation_id": conv.id}


# ── live profiles ────────────────────────────────────────────────────────────

def _user_status(status) -> str:
    info = status_info(status)
    return info["status"] if info else "unknown"


async def fetch_profile(identity_id: int, user_id: int) -> dict:
    """Live profile (bio, photo, status, common conversations). Raises
    ProfileNotFound for unknown users, other errors when Telegram is offline."""
    from backend.telegram.media import download_avatar

    client = await tg_client.connected_client(require=False)
    if client is None:
        raise ConnectionError("Telegram client not connected")
    try:
        entity = await client.get_entity(user_id)
    except (ValueError, TypeError) as e:
        raise ProfileNotFound(str(e))
    if not isinstance(entity, User):
        raise ProfileNotFound("not a user")

    bio = None
    try:
        from telethon.tl.functions.users import GetFullUserRequest
        bio = (await client(GetFullUserRequest(entity))).full_user.about
    except Exception:
        pass

    async with AsyncSessionLocal() as db:
        ident = await ids.ensure_user_identity(db, entity, bio=bio, commit=True)
        person_id = ident.person_id
    avatar = await download_avatar(client, entity, identity_id=identity_id)

    result: dict = {
        "identity_id": identity_id,
        "person_id": person_id,
        "type": "user",
        "first_name": entity.first_name,
        "last_name": entity.last_name,
        "username": entity.username,
        "phone": entity.phone,
        "is_bot": entity.bot,
        "is_self": entity.is_self,
        "is_verified": entity.verified,
        "is_premium": getattr(entity, "premium", False),
        "status": _user_status(entity.status),
        "bio": bio,
        "avatar_url": media_url(avatar),
        "fetched_at": time.time(),
        "common_conversations": [],
    }

    if not entity.is_self:
        try:
            from telethon.tl.functions.messages import GetCommonChatsRequest
            common = await client(GetCommonChatsRequest(user_id=entity.id, max_id=0, limit=50))
            async with AsyncSessionLocal() as db:
                for chat in common.chats:
                    peer = tl_utils.get_peer_id(chat)
                    conv = await ids.conversation_for(db, peer) or await ids.upsert_dialog(db, peer, chat)
                    result["common_conversations"].append({"conversation_id": conv.id, "title": conv.title})
        except Exception:
            logger.debug("common chats lookup failed for %s", identity_id, exc_info=True)
    return result
