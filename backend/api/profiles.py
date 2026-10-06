"""Live Telegram profile metadata (bio, photo, status, common chats).

Local person data (contact, notes, positions) is served separately by
GET /api/people/{telegram_id} so the Profile page works offline. This endpoint
caches results for a short TTL, collapses concurrent fetches of the same id,
and when Telegram is unreachable serves the last cached copy marked
``stale: true`` instead of failing.
"""

import time
from collections import OrderedDict
from typing import Optional

from fastapi import APIRouter, HTTPException
from telethon import utils as tl_utils

from backend.jobs import await_single
from backend.telegram.runtime import runtime
from backend.telegram.media import PROFILE_PHOTO_DIR, download_profile_photo

router = APIRouter()

PROFILE_TTL_SECONDS = 300
_CACHE_MAX = 500
# key -> (fetched_at monotonic, profile dict)
_cache: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


class _ProfileNotFound(Exception):
    pass


def _cache_key(user_id: str) -> str:
    return user_id.strip().lower()


def _cache_get(key: str) -> Optional[tuple[float, dict]]:
    hit = _cache.get(key)
    if hit is not None:
        _cache.move_to_end(key)
    return hit


def _cache_put(key: str, profile: dict) -> None:
    now = time.monotonic()
    for k in {key, str(profile.get("id"))}:
        _cache[k] = (now, profile)
        _cache.move_to_end(k)
    while len(_cache) > _CACHE_MAX:
        _cache.popitem(last=False)


def clear_profile_cache() -> None:
    _cache.clear()


@router.get("/{user_id}")
async def get_profile(user_id: str, refresh: bool = False):
    """Fetch a Telegram user's/chat's profile by numeric (marked) ID or @username.

    Fresh cache hit (< PROFILE_TTL_SECONDS) is returned without any RPC unless
    ``refresh=true``. Response always carries ``stale`` (bool) and
    ``fetched_at`` (epoch seconds of the Telegram fetch)."""
    key = _cache_key(user_id)
    hit = _cache_get(key)
    if hit and not refresh and time.monotonic() - hit[0] < PROFILE_TTL_SECONDS:
        return {**hit[1], "stale": False}

    try:
        profile = await await_single(f"profile:{key}", lambda: _fetch_profile(user_id))
    except _ProfileNotFound as e:
        raise HTTPException(status_code=404, detail=f"User not found: {e}")
    except Exception as e:
        # Offline / FloodWait / RPC failure: degrade to the last known copy
        if hit:
            return {**hit[1], "stale": True}
        raise HTTPException(status_code=503, detail=f"Telegram is unavailable: {e}")
    _cache_put(key, profile)
    return {**profile, "stale": False}


async def _fetch_profile(user_id: str) -> dict:
    client = await runtime.client(require=False)
    if client is None:
        raise ConnectionError("Telegram client not connected")

    try:
        # Support both numeric IDs and @username lookups
        if user_id.startswith("@"):
            entity = await client.get_entity(user_id)
        else:
            entity = await client.get_entity(int(user_id))
    except (ValueError, TypeError) as e:
        # Telethon raises ValueError for unknown ids/usernames; int() for junk
        raise _ProfileNotFound(str(e))

    from telethon.tl.types import User, Channel, Chat as TgChat, Community

    marked_id = tl_utils.get_peer_id(entity)  # marked id, matches DB convention
    result: dict = {
        "id": marked_id,
        "type": "unknown",
    }

    if isinstance(entity, User):
        result.update({
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
        })
    elif isinstance(entity, Channel):
        result.update({
            "type": "channel" if not entity.megagroup else "group",
            "title": entity.title,
            "username": entity.username,
            "members_count": getattr(entity, "participants_count", None),
            "is_verified": entity.verified,
        })
    elif isinstance(entity, Community):
        result.update({
            "type": "group",
            "title": entity.title,
        })
    elif isinstance(entity, TgChat):
        result.update({
            "type": "group",
            "title": entity.title,
            "members_count": getattr(entity, "participants_count", None),
        })

    # Try to get full info (bio, about, etc.)
    try:
        full = await client(
            _get_full_request(entity)
        )
        if hasattr(full, "full_user"):
            result["bio"] = full.full_user.about
        elif hasattr(full, "full_chat"):
            result["about"] = full.full_chat.about
    except Exception:
        pass

    # Profile photo: shared helper downloads to a temp file and renames into
    # place, so a crash or concurrent fetch never leaves a truncated jpg
    photo_path = PROFILE_PHOTO_DIR / f"{marked_id}.jpg"
    await download_profile_photo(client, entity, marked_id)

    if photo_path.exists():
        result["photo_url"] = f"/api/media/profile_photos/{marked_id}.jpg"
    else:
        result["photo_url"] = None
    result["fetched_at"] = time.time()

    # Get common chats for users
    if isinstance(entity, User) and not entity.is_self:
        try:
            from telethon.tl.functions.messages import GetCommonChatsRequest
            common = await client(GetCommonChatsRequest(user_id=entity.id, max_id=0, limit=50))
            result["common_chats"] = [
                {"id": tl_utils.get_peer_id(c), "title": c.title}
                for c in common.chats
            ]
        except Exception:
            result["common_chats"] = []

    return result


def _get_full_request(entity):
    from telethon.tl.types import User, Channel, Chat as TgChat, Community
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetFullChatRequest

    if isinstance(entity, User):
        return GetFullUserRequest(entity)
    elif isinstance(entity, (Channel, Community)):
        return GetFullChannelRequest(entity)
    elif isinstance(entity, TgChat):
        return GetFullChatRequest(entity.id)
    raise ValueError("Unknown entity type")


def _user_status(status) -> str:
    if status is None:
        return "unknown"
    cls_name = type(status).__name__
    mapping = {
        "UserStatusOnline": "online",
        "UserStatusOffline": "offline",
        "UserStatusRecently": "recently",
        "UserStatusLastWeek": "last_week",
        "UserStatusLastMonth": "last_month",
        "UserStatusEmpty": "unknown",
    }
    return mapping.get(cls_name, "unknown")
