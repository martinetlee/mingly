"""Telegram-only operations (/api/telegram): folders, directory search,
opening / creating / joining chats, live profiles and username / user-id
resolution. The Telegram work is ``backend.telegram.directory``; everything
returned is keyed by internal ids, Telegram ids appear only as ``peer_id`` on
directory hits that aren't stored yet."""

import logging
import time
from collections import OrderedDict
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.views import conversation_json
from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Conversation, Identity
from backend.errors import DomainError
from backend.jobs import await_single
from backend.telegram import directory, ids

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
    user_id = await directory.person_user_id(db, person_id)
    if user_id is None:
        raise HTTPException(status_code=409, detail={
            "message": "This person has no Telegram account", "code": "unsupported_channel", "channel": None,
        })
    return user_id


# ── folders ──────────────────────────────────────────────────────────────────

@router.get("/folders")
async def list_folders(db: AsyncSession = Depends(get_db)):
    """The user's Telegram chat folders; members as conversation ids."""
    return await directory.folders(db)


# ── directory search / open ──────────────────────────────────────────────────

@router.get("/search")
async def search_directory(q: str, db: AsyncSession = Depends(get_db)):
    """Find Telegram chats by name: the user's own dialogs locally (dialog sync
    stores every one), plus Telegram's global directory for people / groups /
    channels not in the dialog list. One RPC per query."""
    q = q.strip()
    if not q:
        return []
    local = await crud.search_account_conversations(db, await ids.account_id(db), q, 20)
    found = [conversation_json(c) for c in local]
    if len(q) < 2:
        return found
    remote = await directory.search_directory(db, q, {ids.peer_id_of(c) for c in local})
    return found + [conversation_json(r) if isinstance(r, Conversation) else r for r in remote]


class EnsureRequest(BaseModel):
    peer_id: int


@router.post("/conversations/ensure")
async def ensure_conversation(req: EnsureRequest, db: AsyncSession = Depends(get_db)):
    """The conversation for a Telegram peer, created from Telegram if new.
    Returns immediately; opening it starts the background history sync."""
    return conversation_json(await directory.ensure_conversation(db, req.peer_id))


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
    return conversation_json(await directory.ensure_conversation(db, user_id))


@router.post("/create/group")
async def create_group(req: CreateGroupRequest, db: AsyncSession = Depends(get_db)):
    """Create a new Telegram group with the given people."""
    if not req.person_ids:
        raise HTTPException(status_code=400, detail="At least one member is required")
    user_ids = []
    for pid in req.person_ids:
        uid = await directory.person_user_id(db, pid)
        if uid is not None:
            user_ids.append(uid)
    try:
        conv = await directory.create_group(db, req.name, user_ids)
    except directory.NoMembers as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DomainError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create group: {e}")
    return conversation_json(conv)


@router.post("/create/channel")
async def create_channel(req: CreateChannelRequest, db: AsyncSession = Depends(get_db)):
    """Create a new Telegram broadcast channel."""
    try:
        conv = await directory.create_channel(db, req.name, req.about)
    except DomainError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create channel: {e}")
    return conversation_json(conv)


@router.post("/join")
async def join(req: JoinRequest, db: AsyncSession = Depends(get_db)):
    """Join a chat via an invite link (t.me/joinchat/…, t.me/+…) or a public
    username (t.me/name, @name)."""
    link = req.link.strip()
    if not link:
        raise HTTPException(status_code=400, detail="Link is required")
    if not any(link.startswith(p) for p in directory._INVITE_PREFIXES) and not directory.public_username(link):
        raise HTTPException(status_code=400, detail="Invalid invite link format")
    try:
        conv = await directory.join(db, link)
    except DomainError:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to join: {e}")
    return conversation_json(conv)


# ── people resolution ────────────────────────────────────────────────────────

@router.get("/users/{user_id}/person")
async def person_for_user(user_id: int, db: AsyncSession = Depends(get_db)):
    """The person behind a Telegram user id (from tg://user?id= links),
    recorded on first sight. 404 when the id is neither stored nor
    resolvable through Telegram (no placeholder person is created)."""
    ident = await directory.identity_for_user(db, user_id) if user_id > 0 else None
    if ident is None:
        raise HTTPException(status_code=404, detail="Telegram user not found")
    return {"person_id": ident.person_id, "identity_id": ident.id}


@router.get("/resolve")
async def resolve_username(username: str, db: AsyncSession = Depends(get_db)):
    """Resolve a public @username: a user → their person (and direct
    conversation when stored); a group/channel → its conversation."""
    name = username.strip().lstrip("@")
    if not name:
        raise HTTPException(status_code=400, detail="username is required")
    return await directory.resolve_username(db, name)


# ── live profiles ────────────────────────────────────────────────────────────

PROFILE_TTL_SECONDS = 300
_CACHE_MAX = 500
_cache: "OrderedDict[int, tuple[float, dict]]" = OrderedDict()  # identity_id -> (fetched_at, profile)


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
        profile = await await_single(f"profile:{identity_id}", lambda: directory.fetch_profile(identity_id, user_id))
    except directory.ProfileNotFound as e:
        raise HTTPException(status_code=404, detail=f"User not found: {e}")
    except Exception as e:
        # Offline / FloodWait / RPC failure: degrade to the last known copy
        if hit:
            return {**hit[1], "stale": True}
        raise HTTPException(status_code=503, detail=f"Telegram is unavailable: {e}")
    _cache_put(identity_id, profile)
    return {**profile, "stale": False}
