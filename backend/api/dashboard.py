from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, desc, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import Chat, Message, Contact, ContactPosition, BriefingRun, BriefingEntry
from backend.db.crud import explain_relationship_status, compute_relationship_status

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
    # Get important contact telegram_ids for filtering
    important_tids_q = select(Contact.telegram_id).where(Contact.is_important == True)  # noqa: E712
    important_tids = set(
        (await db.execute(important_tids_q)).scalars().all()
    )

    # --- Stats ---
    # Unread today: count of unread messages from today in tracked chats or
    # private chats with important contacts
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)

    # Build filter for "relevant" chats: tracked OR private chat with important contact
    relevant_chat_filter = or_(
        Chat.is_tracked == True,  # noqa: E712
        and_(
            Chat.type == "private",
            Chat.telegram_id.in_(important_tids) if important_tids else False,
        ),
    )

    unread_today = (await db.execute(
        select(func.count()).select_from(Message)
        .join(Chat, Chat.telegram_id == Message.chat_id)
        .where(
            Message.date >= cutoff_24h,
            Message.is_outgoing == False,  # noqa: E712
            Chat.is_hidden == False,  # noqa: E712
            relevant_chat_filter,
        )
    )).scalar() or 0

    stats = {
        "unread_today": unread_today,
    }

    # --- Needs Reply ---
    # Find chats where the most recent message is incoming (not outgoing)
    # Only from tracked chats or private chats with important contacts
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
            relevant_chat_filter,
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
    # Important contacts
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

        # Recompute relationship status live to avoid stale needs_reply
        is_manual = getattr(c, "relationship_status_manual", False)
        if is_manual:
            live_status = getattr(c, "relationship_status", None)
            live_last_interaction = getattr(c, "last_interaction_at", None)
            live_last_outgoing = getattr(c, "last_outgoing_at", None)
            live_last_incoming = getattr(c, "last_incoming_at", None)
        else:
            live_status, live_last_interaction, live_last_outgoing, live_last_incoming = (
                await compute_relationship_status(db, c)
            )

        status_info = explain_relationship_status(
            status=live_status,
            is_manual=is_manual,
            last_interaction=live_last_interaction,
            last_outgoing=live_last_outgoing,
            last_incoming=live_last_incoming,
            is_important=c.is_important,
        )
        contact_data = {
            "id": c.id,
            "telegram_id": c.telegram_id,
            "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or str(c.telegram_id),
            "username": c.username,
            "is_important": c.is_important,
            "relationship_status": live_status,
            "relationship_status_manual": is_manual,
            "last_interaction_at": live_last_interaction,
            "primary_position": primary,
        }
        contact_data.update(status_info)
        attention_contacts.append(contact_data)

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
