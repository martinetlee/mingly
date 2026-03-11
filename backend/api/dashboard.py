from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, desc, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import Chat, Message, Contact, ContactPosition, BriefingRun, BriefingEntry

router = APIRouter()


def _chat_dict(c) -> dict:
    return {
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


@router.get("")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    # --- Stats ---
    total_unread = (await db.execute(
        select(func.coalesce(func.sum(Chat.unread_count), 0))
        .where(Chat.is_hidden == False)  # noqa: E712
    )).scalar() or 0

    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    messages_today = (await db.execute(
        select(func.count()).select_from(Message).where(Message.date >= cutoff_24h)
    )).scalar() or 0

    # Active contacts = contacts with any message in last 7 days
    cutoff_7d = datetime.utcnow() - timedelta(days=7)
    active_contact_ids = (await db.execute(
        select(func.count(func.distinct(Message.sender_id)))
        .where(Message.date >= cutoff_7d, Message.sender_id.isnot(None), Message.is_outgoing == False)  # noqa: E712
    )).scalar() or 0

    stats = {
        "total_unread": total_unread,
        "messages_today": messages_today,
        "active_contacts": active_contact_ids,
    }

    # --- Needs Reply ---
    # Find chats where the most recent message is incoming (not outgoing)
    # Use a window function to get the last message per chat
    last_msg_subq = (
        select(
            Message.chat_id,
            Message.sender_name,
            func.substr(Message.text, 1, 200).label("text"),
            Message.date,
            Message.is_outgoing,
            func.row_number().over(
                partition_by=Message.chat_id,
                order_by=desc(Message.date),
            ).label("rn"),
        )
        .subquery()
    )

    needs_reply_q = (
        select(
            Chat,
            last_msg_subq.c.sender_name,
            last_msg_subq.c.text,
            last_msg_subq.c.date.label("msg_date"),
        )
        .join(last_msg_subq, and_(
            Chat.telegram_id == last_msg_subq.c.chat_id,
            last_msg_subq.c.rn == 1,
        ))
        .where(
            last_msg_subq.c.is_outgoing == False,  # noqa: E712
            Chat.is_hidden == False,  # noqa: E712
            Chat.is_briefing == False,  # noqa: E712
        )
        .order_by(desc(last_msg_subq.c.date))
        .limit(10)
    )
    needs_reply_rows = (await db.execute(needs_reply_q)).all()
    needs_reply = [
        {
            "chat": _chat_dict(row[0]),
            "last_message": {
                "sender_name": row[1],
                "text": row[2],
                "date": row[3],
            },
        }
        for row in needs_reply_rows
    ]

    # --- Attention Contacts ---
    # Important contacts who haven't been messaged recently (no outgoing in 7d)
    # Since relationship_status may not exist on the model yet, we use a simpler heuristic
    important_contacts_q = (
        select(Contact)
        .where(Contact.is_important == True)  # noqa: E712
        .order_by(Contact.first_name, Contact.last_name)
        .limit(10)
    )
    important_contacts = (await db.execute(important_contacts_q)).scalars().all()

    # Batch-load positions
    pos_map = {}
    if important_contacts:
        contact_ids = [c.id for c in important_contacts]
        pos_result = await db.execute(
            select(ContactPosition).where(ContactPosition.contact_id.in_(contact_ids))
        )
        for p in pos_result.scalars().all():
            pos_map.setdefault(p.contact_id, []).append(p)

    attention_contacts = []
    for c in important_contacts:
        positions = pos_map.get(c.id, [])
        active_pos = [p for p in positions if p.is_active]
        primary = None
        if active_pos:
            primary = {"title": active_pos[0].title, "organization": active_pos[0].organization}
        attention_contacts.append({
            "id": c.id,
            "telegram_id": c.telegram_id,
            "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or str(c.telegram_id),
            "username": c.username,
            "is_important": c.is_important,
            "relationship_status": getattr(c, "relationship_status", None),
            "last_interaction_at": getattr(c, "last_interaction_at", None),
            "primary_position": primary,
        })

    # --- Latest Briefing ---
    latest_run = (await db.execute(
        select(BriefingRun).order_by(desc(BriefingRun.generated_at)).limit(1)
    )).scalar_one_or_none()

    latest_briefing = None
    if latest_run:
        entries = (await db.execute(
            select(BriefingEntry)
            .where(BriefingEntry.run_id == latest_run.id)
            .limit(5)
        )).scalars().all()
        latest_briefing = {
            "id": latest_run.id,
            "generated_at": latest_run.generated_at,
            "days": latest_run.days,
            "entries": [
                {
                    "id": e.id,
                    "chat_id": e.chat_id,
                    "chat_name": e.chat_name,
                    "category": e.category,
                    "summary": e.summary,
                }
                for e in entries
            ],
        }

    return {
        "stats": stats,
        "needs_reply": needs_reply,
        "attention_contacts": attention_contacts,
        "latest_briefing": latest_briefing,
    }
