from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import Chat, Message, Contact, ContactPosition, Organization

router = APIRouter()


@router.get("")
async def global_search(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, le=50),
    db: AsyncSession = Depends(get_db),
):
    pattern = f"%{q}%"

    # Messages — join with Chat for chat_name, limit text to 200 chars
    msg_q = (
        select(
            Message.telegram_id,
            Message.chat_id,
            Chat.name.label("chat_name"),
            Message.sender_name,
            func.substr(Message.text, 1, 200).label("text"),
            Message.date,
        )
        .join(Chat, Chat.telegram_id == Message.chat_id)
        .where(Message.text.ilike(pattern))
        .order_by(desc(Message.date))
        .limit(limit)
    )
    msg_rows = (await db.execute(msg_q)).all()
    messages = [
        {
            "telegram_id": r.telegram_id,
            "chat_id": r.chat_id,
            "chat_name": r.chat_name,
            "sender_name": r.sender_name,
            "text": r.text,
            "date": r.date,
        }
        for r in msg_rows
    ]

    # Chats — non-hidden
    chat_q = (
        select(Chat)
        .where(Chat.is_hidden == False, Chat.name.ilike(pattern))  # noqa: E712
        .order_by(Chat.name)
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
            "last_synced_at": c.last_synced_at,
        }
        for c in chat_rows
    ]

    # Contacts
    contact_q = (
        select(Contact)
        .where(or_(
            Contact.first_name.ilike(pattern),
            Contact.last_name.ilike(pattern),
            Contact.username.ilike(pattern),
            Contact.notes.ilike(pattern),
        ))
        .order_by(Contact.first_name, Contact.last_name)
        .limit(limit)
    )
    contact_rows = (await db.execute(contact_q)).scalars().all()

    # Batch-load positions for contacts
    pos_map = {}
    if contact_rows:
        contact_ids = [c.id for c in contact_rows]
        pos_result = await db.execute(
            select(ContactPosition).where(ContactPosition.contact_id.in_(contact_ids))
        )
        for p in pos_result.scalars().all():
            pos_map.setdefault(p.contact_id, []).append(p)

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
            "synced_at": c.synced_at,
            "positions": [
                {
                    "id": p.id,
                    "organization": p.organization,
                    "title": p.title,
                    "is_active": p.is_active,
                    "ended_at": p.ended_at.isoformat() if p.ended_at else None,
                }
                for p in pos_map.get(c.id, [])
            ],
        }
        for c in contact_rows
    ]

    # Organizations
    org_q = (
        select(Organization)
        .where(or_(
            Organization.name.ilike(pattern),
            Organization.context.ilike(pattern),
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
