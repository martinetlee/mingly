from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import utils as tl_utils
from telethon.tl.functions.contacts import SearchRequest as TgSearchRequest, GetContactsRequest

from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Chat, Message, Contact, ContactPosition, Organization
from backend.serialization import iso_utc
from backend.telegram.runtime import runtime
from backend.telegram.entities import chat_type as _tg_chat_type

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("")
async def global_search(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, le=50),
    db: AsyncSession = Depends(get_db),
):

    # Messages — join with Chat for chat_name, limit text to 200 chars
    msg_q = (
        select(
            Message.tg_message_id,
            Message.chat_id,
            Chat.name.label("chat_name"),
            Message.sender_name,
            func.substr(Message.text, 1, 200).label("text"),
            Message.date,
        )
        .join(Chat, Chat.telegram_id == Message.chat_id)
        .where(crud.contains_ci(Message.text, q))
        .order_by(desc(Message.date))
        .limit(limit)
    )
    msg_rows = (await db.execute(msg_q)).all()
    messages = [
        {
            "tg_message_id": r.tg_message_id,
            "chat_id": r.chat_id,
            "chat_name": r.chat_name,
            "sender_name": r.sender_name,
            "text": r.text,
            "date": iso_utc(r.date),
        }
        for r in msg_rows
    ]

    # Chats — non-hidden, sorted by most recent message
    last_msg = (
        select(
            Message.chat_id,
            func.max(Message.date).label("last_date"),
        )
        .group_by(Message.chat_id)
        .subquery("last_msg")
    )
    chat_q = (
        select(Chat)
        .where(Chat.is_hidden == False, Chat.is_deactivated == False, crud.contains_ci(Chat.name, q))  # noqa: E712
        .outerjoin(last_msg, Chat.telegram_id == last_msg.c.chat_id)
        .order_by(desc(last_msg.c.last_date), Chat.name)
        .limit(limit)
    )
    chat_rows = (await db.execute(chat_q)).scalars().all()
    chats = [
        {
            "id": c.id,
            "telegram_id": c.telegram_id,
            "name": c.name,
            "type": c.type,
            "username": c.username,
            "is_monitored": c.is_monitored,
            "is_hidden": c.is_hidden,
            "is_briefing": c.is_briefing,
            "is_tracked": c.is_tracked,
            "unread_count": c.unread_count or 0,
            "last_synced_at": iso_utc(c.last_synced_at),
        }
        for c in chat_rows
    ]

    # Contacts
    contact_q = (
        select(Contact)
        .where(or_(
            crud.contains_ci(Contact.first_name, q),
            crud.contains_ci(Contact.last_name, q),
            crud.contains_ci(Contact.username, q),
            crud.contains_ci(Contact.notes, q),
        ))
        .order_by(Contact.first_name, Contact.last_name)
        .limit(limit)
    )
    contact_rows = (await db.execute(contact_q)).scalars().all()

    # Batch-load positions by telegram_id
    pos_map = {}
    if contact_rows:
        telegram_ids = [c.telegram_id for c in contact_rows]
        pos_result = await db.execute(
            select(ContactPosition).where(ContactPosition.telegram_id.in_(telegram_ids))
        )
        for p in pos_result.scalars().all():
            pos_map.setdefault(p.telegram_id, []).append(p)

    contacts = [
        {
            "id": c.id,
            "telegram_id": c.telegram_id,
            "first_name": c.first_name,
            "last_name": c.last_name,
            "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or str(c.telegram_id),
            "username": c.username,
            "phone": c.phone,
            "twitter_handle": c.twitter_handle,
            "linkedin_url": c.linkedin_url,
            "notes": c.notes,
            "is_important": c.is_important,
            "synced_at": iso_utc(c.synced_at),
            "positions": [
                {
                    "id": p.id,
                    "organization_id": p.organization_id,
                    "organization": p.organization,
                    "title": p.title,
                    "is_active": p.is_active,
                    "ended_at": iso_utc(p.ended_at),
                }
                for p in pos_map.get(c.telegram_id, [])
            ],
        }
        for c in contact_rows
    ]

    # Organizations
    org_q = (
        select(Organization)
        .where(or_(
            crud.contains_ci(Organization.name, q),
            crud.contains_ci(Organization.context, q),
        ))
        .order_by(Organization.name)
        .limit(limit)
    )
    org_rows = (await db.execute(org_q)).scalars().all()
    organizations = [
        {
            "id": o.id,
            "name": o.name,
            "context": (o.context[:100] if o.context else None),
        }
        for o in org_rows
    ]

    return {
        "messages": messages,
        "chats": chats,
        "contacts": contacts,
        "organizations": organizations,
    }


@router.get("/telegram")
async def telegram_search(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, le=50),
    db: AsyncSession = Depends(get_db),
):
    """Search Telegram dialogs that aren't synced locally yet."""
    # Offline is an empty result here, not an error (local hits still show)
    client = await runtime.client(require=False)
    if client is None:
        return {"telegram_dialogs": []}

    seen_ids: set[int] = set()
    dialogs: list[dict] = []

    def _add(tid: int, name: str, chat_type: str, username: str | None) -> None:
        if tid in seen_ids:
            return
        seen_ids.add(tid)
        dialogs.append({
            "telegram_id": tid,
            "name": name,
            "type": chat_type,
            "username": username,
        })

    # 1) Search user's own contacts by name (client-side filter)
    q_lower = q.lower()
    try:
        contacts_result = await client(GetContactsRequest(hash=0))
        for user in contacts_result.users:
            name = f"{user.first_name or ''} {user.last_name or ''}".strip()
            if not name:
                continue
            if q_lower in name.lower() or (user.username and q_lower in user.username.lower()):
                _add(user.id, name, "private", getattr(user, "username", None))
    except Exception as e:
        logger.warning("Telegram contacts search failed: %s", e)

    # 2) Search Telegram's global directory for users and chats
    try:
        result = await client(TgSearchRequest(q=q, limit=limit))
        for user in result.users:
            name = f"{user.first_name or ''} {user.last_name or ''}".strip() or str(user.id)
            _add(user.id, name, "private", getattr(user, "username", None))
        for chat in result.chats:
            _add(
                tl_utils.get_peer_id(chat),  # marked id, matches DB convention
                getattr(chat, "title", None) or str(chat.id),
                _tg_chat_type(chat),
                getattr(chat, "username", None),
            )
    except Exception as e:
        logger.warning("Telegram global search failed: %s", e)

    if not dialogs:
        return {"telegram_dialogs": []}

    # Trim to limit
    dialogs = dialogs[:limit]

    return {"telegram_dialogs": dialogs}
