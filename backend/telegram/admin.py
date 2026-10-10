"""Administering Telegram groups and channels: admin rights, bans and
restrictions, invite links, title / description, slow mode and post stats.

Called by ``TelegramConnector``; arguments are stored rows and internal
identity ids.
"""

from datetime import datetime
from typing import Optional

from telethon.errors import FloodWaitError

from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation
from backend.errors import NotFound, RateLimited
from backend.serialization import iso_utc
from backend.telegram import client as tg_client
from backend.telegram import ids
from backend.telegram.coordinator import coordinator


async def _user_id(identity_id: int) -> int:
    """The Telegram user id of the identity an admin action targets."""
    async with AsyncSessionLocal() as db:
        user_ids = await ids.user_ids_for(db, [identity_id])
    if identity_id not in user_ids:
        raise NotFound("Telegram user not found")
    return user_ids[identity_id]


async def promote(conv: Conversation, identity_id: int, permissions: dict) -> None:
    from telethon.tl.functions.channels import EditAdminRequest
    from telethon.tl.types import ChatAdminRights

    client = await tg_client.connected_client()
    await client(EditAdminRequest(
        channel=ids.peer_id_of(conv), user_id=await _user_id(identity_id),
        admin_rights=ChatAdminRights(**permissions), rank="Admin",
    ))


async def demote(conv: Conversation, identity_id: int) -> None:
    from telethon.tl.functions.channels import EditAdminRequest
    from telethon.tl.types import ChatAdminRights

    client = await tg_client.connected_client()
    # All-false rights effectively demotes
    await client(EditAdminRequest(
        channel=ids.peer_id_of(conv), user_id=await _user_id(identity_id), admin_rights=ChatAdminRights(), rank="",
    ))


async def _edit_banned(conv: Conversation, identity_id: int, rights) -> None:
    from telethon.tl.functions.channels import EditBannedRequest

    client = await tg_client.connected_client()
    await client(EditBannedRequest(
        channel=ids.peer_id_of(conv), participant=await _user_id(identity_id), banned_rights=rights,
    ))


async def ban(conv: Conversation, identity_id: int) -> None:
    from telethon.tl.types import ChatBannedRights

    await _edit_banned(conv, identity_id, ChatBannedRights(
        until_date=None, view_messages=True, send_messages=True, send_media=True, send_stickers=True,
        send_gifs=True, send_games=True, send_inline=True, embed_links=True,
    ))


async def restrict(conv: Conversation, identity_id: int, allowed: dict, until: Optional[datetime]) -> None:
    """``allowed``: permission → still allowed; Telegram's banned rights are
    the negation."""
    from telethon.tl.types import ChatBannedRights

    await _edit_banned(conv, identity_id, ChatBannedRights(
        until_date=until, **{name: not ok for name, ok in allowed.items()},
    ))


async def approve(conv: Conversation, identity_id: int) -> None:
    from telethon.tl.types import ChatBannedRights

    # All-false rights = no restrictions
    await _edit_banned(conv, identity_id, ChatBannedRights(until_date=None))


def _invite_out(invite, *, revoked: Optional[bool] = None) -> dict:
    return {
        "link": invite.link,
        "title": getattr(invite, "title", None),
        "expire_date": iso_utc(invite.expire_date),
        "usage_limit": getattr(invite, "usage_limit", None),
        "usage": getattr(invite, "usage", 0) or 0,
        "request_needed": getattr(invite, "request_needed", False),
        "revoked": getattr(invite, "revoked", False) if revoked is None else revoked,
    }


async def list_invite_links(conv: Conversation) -> list[dict]:
    from telethon.tl.functions.messages import GetExportedChatInvitesRequest

    client = await tg_client.connected_client()
    me = await client.get_me()
    result = await client(GetExportedChatInvitesRequest(
        peer=ids.peer_id_of(conv), admin_id=me, limit=50, revoked=False,
    ))
    return [_invite_out(invite) for invite in result.invites]


async def create_invite_link(conv: Conversation, *, expire: Optional[datetime], usage_limit: Optional[int],
                             title: Optional[str], request_needed: bool) -> dict:
    from telethon.tl.functions.messages import ExportChatInviteRequest

    client = await tg_client.connected_client()
    result = await client(ExportChatInviteRequest(
        peer=ids.peer_id_of(conv), expire_date=expire, usage_limit=usage_limit, title=title,
        request_needed=request_needed,
    ))
    return _invite_out(result, revoked=False)


async def revoke_invite_link(conv: Conversation, link: str) -> None:
    from telethon.tl.functions.messages import EditExportedChatInviteRequest

    client = await tg_client.connected_client()
    await client(EditExportedChatInviteRequest(peer=ids.peer_id_of(conv), link=link, revoked=True))


async def get_group_settings(conv: Conversation) -> dict:
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetFullChatRequest

    client = await tg_client.connected_client()
    peer = ids.peer_id_of(conv)
    full = await client.get_entity(peer)
    title = getattr(full, "title", "")
    # about requires full chat info
    try:
        about = (await client(GetFullChannelRequest(channel=peer))).full_chat.about or ""
    except Exception:
        try:
            about = (await client(GetFullChatRequest(chat_id=peer))).full_chat.about or ""
        except Exception:
            about = ""
    return {"title": title, "about": about}


async def update_group_settings(conv: Conversation, *, title: Optional[str], about: Optional[str]) -> list[str]:
    from telethon.tl.functions.channels import EditTitleRequest
    from telethon.tl.functions.messages import EditChatAboutRequest

    client = await tg_client.connected_client()
    peer = ids.peer_id_of(conv)
    changes = []
    if title is not None:
        await client(EditTitleRequest(channel=peer, title=title))
        changes.append("title")
    if about is not None:
        await client(EditChatAboutRequest(peer=peer, about=about))
        changes.append("about")
    return changes


async def set_slow_mode(conv: Conversation, seconds: int) -> None:
    from telethon.tl.functions.channels import ToggleSlowModeRequest

    client = await tg_client.connected_client()
    await client(ToggleSlowModeRequest(channel=ids.peer_id_of(conv), seconds=seconds))


async def post_stats(conv: Conversation, limit: int) -> list[dict]:
    """Views / forwards / replies of recent channel posts (shares the history
    rate limiter)."""
    client = await tg_client.connected_client()
    if not await coordinator.limiter.acquire(1, max_wait=30):
        raise RateLimited(coordinator.limiter.flood_remaining)
    posts = []
    try:
        async for msg in client.iter_messages(ids.peer_id_of(conv), limit=limit):
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
        raise RateLimited(e.seconds)
    return posts
