"""Telegram-only operations (/api/telegram): folders, directory search,
opening / creating / joining chats, live profiles and username / user-id
resolution. Everything returned is keyed by internal ids; Telegram ids appear
only as ``peer_id`` on directory hits that aren't stored yet."""

import logging
import time
from collections import OrderedDict
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import utils as tl_utils
from telethon.errors import FloodWaitError
from telethon.tl.types import User

from backend.db import crud
from backend.db.database import AsyncSessionLocal, get_db
from backend.db.models import Conversation, Identity
from backend.jobs import await_single
from backend.serializers import conversation_out, media_url
from backend.telegram import ids
from backend.telegram.coordinator import coordinator
from backend.telegram.entities import display_name, is_deactivated
from backend.telegram.runtime import runtime

logger = logging.getLogger(__name__)

router = APIRouter()


async def _telegram_identity(db: AsyncSession, identity_id: int) -> Identity:
    ident = await crud.get_identity(db, identity_id)
    if ident is None:
        raise HTTPException(status_code=404, detail="Identity not found")
    if ident.channel != ids.CHANNEL:
        raise HTTPException(status_code=409, detail={
            "message": "Not a Telegram identity", "code": "unsupported_channel", "channel": ident.channel,
        })
    return ident


async def _person_user_id(db: AsyncSession, person_id: int) -> int:
    """The Telegram user id of a person (their first Telegram identity)."""
    for ident in await crud.identities_for_person(db, person_id):
        if ident.channel == ids.CHANNEL:
            return int(ident.external_id)
    raise HTTPException(status_code=409, detail={
        "message": "This person has no Telegram account", "code": "unsupported_channel", "channel": None,
    })


# ── folders ──────────────────────────────────────────────────────────────────

@router.get("/folders")
async def list_folders(db: AsyncSession = Depends(get_db)):
    """The user's Telegram chat folders; members as conversation ids."""
    from telethon.tl.functions.messages import GetDialogFiltersRequest
    from telethon.tl.types import DialogFilter

    client = await runtime.client(require=False)
    if not client:
        return []
    try:
        response = await client(GetDialogFiltersRequest())
    except Exception as e:
        logger.warning("Failed to fetch dialog folders: %s", e)
        return []
    filters = response.filters if hasattr(response, "filters") else response
    folders = [f for f in filters if isinstance(f, DialogFilter)]  # skip default / chatlist
    peers: set[int] = set()
    for f in folders:
        for peer in f.include_peers:
            try:
                peers.add(tl_utils.get_peer_id(peer))
            except Exception:
                pass
    by_peer = await ids.conversation_ids_for(db, peers)
    result = []
    for f in folders:
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


@router.get("/search")
async def search_directory(q: str, db: AsyncSession = Depends(get_db)):
    """Find Telegram chats by name: the user's own dialogs locally (dialog sync
    stores every one), plus Telegram's global directory for people / groups /
    channels not in the dialog list. One RPC per query."""
    q = q.strip()
    if not q:
        return []
    acc = await ids.account_id(db)
    pattern = f"%{q}%"
    local = (await db.execute(
        select(Conversation)
        .where(
            Conversation.account_id == acc,
            or_(Conversation.title.ilike(pattern), Conversation.username.ilike(pattern)),
            Conversation.is_deactivated == False,  # noqa: E712
        )
        .order_by(Conversation.last_activity_at.desc().nullslast())
        .limit(20)
    )).scalars().all()
    found = [conversation_out(c) for c in local]
    seen = {ids.peer_id_of(c) for c in local}

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
    entities = [(u.id, u) for u in res.users] + [(tl_utils.get_peer_id(c), c) for c in res.chats]
    stored = await ids.conversation_ids_for(db, [p for p, _ in entities if p not in seen])
    stored_convs = await crud.get_conversations(db, stored.values())
    for peer, entity in entities:
        if peer in seen:
            continue
        seen.add(peer)
        if peer in stored:
            found.append(conversation_out(stored_convs[stored[peer]]))
        else:
            found.append(_directory_hit(peer, entity))
    return found


class EnsureRequest(BaseModel):
    peer_id: int


async def _ensure_conversation(db: AsyncSession, peer_id: int) -> Conversation:
    conv = await ids.conversation_for(db, peer_id)
    if conv is not None:
        return conv
    client = await runtime.client()
    try:
        entity = await client.get_entity(peer_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Could not resolve Telegram chat: {e}")
    return await ids.upsert_dialog(db, tl_utils.get_peer_id(entity), entity)


@router.post("/conversations/ensure")
async def ensure_conversation(req: EnsureRequest, db: AsyncSession = Depends(get_db)):
    """The conversation for a Telegram peer, created from Telegram if new.
    Returns immediately; opening it starts the background history sync."""
    return conversation_out(await _ensure_conversation(db, req.peer_id))


# ── create / join ────────────────────────────────────────────────────────────

class StartPrivateRequest(BaseModel):
    person_id: int


class CreateGroupRequest(BaseModel):
    name: str
    person_ids: List[int]


class CreateChannelRequest(BaseModel):
    name: str
    about: str = ""
    is_public: bool = False


class JoinRequest(BaseModel):
    link: str


@router.post("/create/private")
async def start_private(req: StartPrivateRequest, db: AsyncSession = Depends(get_db)):
    """Open (or start) a direct conversation with a person on Telegram."""
    user_id = await _person_user_id(db, req.person_id)
    return conversation_out(await _ensure_conversation(db, user_id))


@router.post("/create/group")
async def create_group(req: CreateGroupRequest, db: AsyncSession = Depends(get_db)):
    """Create a new Telegram group with the given people."""
    if not req.person_ids:
        raise HTTPException(status_code=400, detail="At least one member is required")
    client = await runtime.client()
    users = []
    for pid in req.person_ids:
        try:
            users.append(await client.get_entity(await _person_user_id(db, pid)))
        except Exception:
            pass  # skip unresolvable people
    if not users:
        raise HTTPException(status_code=400, detail="No valid users could be resolved")
    try:
        from telethon.tl.functions.messages import CreateChatRequest
        result = await client(CreateChatRequest(users=users, title=req.name))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create group: {e}")
    created = result.chats[0] if getattr(result, "chats", None) else result.updates.chats[0]
    conv = await ids.upsert_dialog(db, tl_utils.get_peer_id(created), created, title=req.name)
    return conversation_out(conv)


@router.post("/create/channel")
async def create_channel(req: CreateChannelRequest, db: AsyncSession = Depends(get_db)):
    """Create a new Telegram broadcast channel."""
    client = await runtime.client()
    try:
        from telethon.tl.functions.channels import CreateChannelRequest as TLCreateChannel
        result = await client(TLCreateChannel(title=req.name, about=req.about, megagroup=False, broadcast=True))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create channel: {e}")
    created = result.chats[0]
    conv = await ids.upsert_dialog(db, tl_utils.get_peer_id(created), created, title=req.name)
    return conversation_out(conv)


_INVITE_PREFIXES = ("https://t.me/joinchat/", "https://t.me/+", "t.me/joinchat/", "t.me/+")


@router.post("/join")
async def join(req: JoinRequest, db: AsyncSession = Depends(get_db)):
    """Join a chat via an invite link (t.me/joinchat/…, t.me/+…) or a public
    username (t.me/name, @name)."""
    client = await runtime.client()
    link = req.link.strip()
    if not link:
        raise HTTPException(status_code=400, detail="Link is required")
    try:
        invite_hash = next((link[len(p):] for p in _INVITE_PREFIXES if link.startswith(p)), None)
        if invite_hash:
            from telethon.tl.functions.messages import ImportChatInviteRequest
            joined = (await client(ImportChatInviteRequest(hash=invite_hash))).chats[0]
        else:
            username = link.replace("https://t.me/", "").replace("http://t.me/", "").replace("t.me/", "").lstrip("@")
            if not username:
                raise HTTPException(status_code=400, detail="Invalid invite link format")
            from telethon.tl.functions.channels import JoinChannelRequest
            entity = await client.get_entity(username)
            result = await client(JoinChannelRequest(entity))
            joined = result.chats[0] if result.chats else entity
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to join: {e}")
    conv = await ids.upsert_dialog(db, tl_utils.get_peer_id(joined), joined, is_deactivated=is_deactivated(joined))
    return conversation_out(conv)


# ── people resolution ────────────────────────────────────────────────────────

@router.get("/users/{user_id}/person")
async def person_for_user(user_id: int, db: AsyncSession = Depends(get_db)):
    """The person behind a Telegram user id (from tg://user?id= links),
    recorded on first sight."""
    ident = await ids.identity_for_user_id(db, user_id, create=False)
    if ident is None:
        client = await runtime.client(require=False)
        entity = None
        if client is not None:
            try:
                entity = await client.get_entity(user_id)
            except Exception:
                entity = None
        if isinstance(entity, User):
            ident = await ids.ensure_user_identity(db, entity)
        else:
            ident = await ids.identity_for_user_id(db, user_id)
        await db.commit()
    return {"person_id": ident.person_id, "identity_id": ident.id}


@router.get("/resolve")
async def resolve_username(username: str, db: AsyncSession = Depends(get_db)):
    """Resolve a public @username: a user → their person (and direct
    conversation when stored); a group/channel → its conversation."""
    name = username.strip().lstrip("@")
    if not name:
        raise HTTPException(status_code=400, detail="username is required")
    client = await runtime.client()
    try:
        entity = await client.get_entity(name)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=404, detail=f"Not found: {e}")
    if isinstance(entity, User):
        ident = await ids.ensure_user_identity(db, entity)
        await db.commit()
        conv = await ids.conversation_for(db, entity.id)
        return {"person_id": ident.person_id, "identity_id": ident.id, "conversation_id": conv.id if conv else None}
    conv = await ids.conversation_for(db, tl_utils.get_peer_id(entity)) or await ids.upsert_dialog(
        db, tl_utils.get_peer_id(entity), entity
    )
    return {"person_id": None, "identity_id": None, "conversation_id": conv.id}


# ── live profiles ────────────────────────────────────────────────────────────

PROFILE_TTL_SECONDS = 300
_CACHE_MAX = 500
_cache: "OrderedDict[int, tuple[float, dict]]" = OrderedDict()  # identity_id -> (fetched_at, profile)


class _ProfileNotFound(Exception):
    pass


def clear_profile_cache() -> None:
    _cache.clear()


def _cache_put(identity_id: int, profile: dict) -> None:
    _cache[identity_id] = (time.monotonic(), profile)
    _cache.move_to_end(identity_id)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


@router.get("/identities/{identity_id}/profile")
async def get_profile(identity_id: int, refresh: bool = False, db: AsyncSession = Depends(get_db)):
    """Live Telegram profile of an identity (bio, photo, status, common
    conversations). Local person data is served by /api/people/{id}, so the
    person page works offline. Results are cached for a short TTL,
    concurrent fetches collapse, and when Telegram is unreachable the last
    copy is served marked ``stale: true``."""
    ident = await _telegram_identity(db, identity_id)
    hit = _cache.get(identity_id)
    if hit and not refresh and time.monotonic() - hit[0] < PROFILE_TTL_SECONDS:
        return {**hit[1], "stale": False}
    user_id = int(ident.external_id)
    try:
        profile = await await_single(f"profile:{identity_id}", lambda: _fetch_profile(identity_id, user_id))
    except _ProfileNotFound as e:
        raise HTTPException(status_code=404, detail=f"User not found: {e}")
    except Exception as e:
        # Offline / FloodWait / RPC failure: degrade to the last known copy
        if hit:
            return {**hit[1], "stale": True}
        raise HTTPException(status_code=503, detail=f"Telegram is unavailable: {e}")
    _cache_put(identity_id, profile)
    return {**profile, "stale": False}


def _user_status(status) -> str:
    from backend.telegram.sync import status_info
    info = status_info(status)
    return info["status"] if info else "unknown"


async def _fetch_profile(identity_id: int, user_id: int) -> dict:
    from backend.telegram.media import download_avatar

    client = await runtime.client(require=False)
    if client is None:
        raise ConnectionError("Telegram client not connected")
    try:
        entity = await client.get_entity(user_id)
    except (ValueError, TypeError) as e:
        raise _ProfileNotFound(str(e))
    if not isinstance(entity, User):
        raise _ProfileNotFound("not a user")

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

