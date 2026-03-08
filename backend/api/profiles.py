from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.config import DATA_DIR
from backend.telegram.client import get_client, ensure_connected

router = APIRouter()

PROFILE_PHOTO_DIR = DATA_DIR / "profile_photos"
PROFILE_PHOTO_DIR.mkdir(exist_ok=True)


@router.get("/{user_id}")
async def get_profile(user_id: int):
    """Fetch a Telegram user's profile by their numeric ID."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    try:
        entity = await client.get_entity(user_id)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"User not found: {e}")

    from telethon.tl.types import User, Channel, Chat as TgChat

    result: dict = {
        "id": entity.id,
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

    # Profile photo path
    photo_path = PROFILE_PHOTO_DIR / f"{entity.id}.jpg"
    if not photo_path.exists():
        try:
            downloaded = await client.download_profile_photo(
                entity, file=str(photo_path), download_big=False
            )
            if not downloaded:
                photo_path = None
        except Exception:
            photo_path = None

    if photo_path and photo_path.exists():
        result["photo_url"] = f"/api/media/profile_photos/{entity.id}.jpg"
    else:
        result["photo_url"] = None

    # Get common chats for users
    if isinstance(entity, User) and not entity.is_self:
        try:
            from telethon.tl.functions.messages import GetCommonChatsRequest
            common = await client(GetCommonChatsRequest(user_id=entity.id, max_id=0, limit=50))
            result["common_chats"] = [
                {"id": c.id, "title": c.title}
                for c in common.chats
            ]
        except Exception:
            result["common_chats"] = []

    return result


def _get_full_request(entity):
    from telethon.tl.types import User, Channel, Chat as TgChat
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetFullChatRequest

    if isinstance(entity, User):
        return GetFullUserRequest(entity)
    elif isinstance(entity, Channel):
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
