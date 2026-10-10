"""Global local search (⌘K): messages, conversations, people and
organizations across every channel. Telegram's directory search lives at
/api/telegram/search."""

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Conversation, Identity, Message, Organization, Person, PersonPosition
from backend.serialization import iso_utc
from backend.serializers import person_out
from backend.api.views import conversation_json

logger = logging.getLogger(__name__)

router = APIRouter()


def _position_out(p: PersonPosition) -> dict:
    return {
        "id": p.id,
        "organization_id": p.organization_id,
        "organization": p.organization,
        "title": p.title,
        "is_active": p.is_active,
        "ended_at": iso_utc(p.ended_at),
    }


@router.get("")
async def global_search(
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
):
    # Messages (text or email subject), newest first
    msg_rows = (await db.execute(
        select(
            Message.id,
            Message.conversation_id,
            Conversation.title.label("conversation_title"),
            Message.sender_name,
            Message.subject,
            func.substr(Message.text, 1, 200).label("text"),
            Message.date,
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(or_(crud.contains_ci(Message.text, q), crud.contains_ci(Message.subject, q)))
        .order_by(desc(Message.date))
        .limit(limit)
    )).all()
    messages = [
        {
            "message_id": r.id,
            "conversation_id": r.conversation_id,
            "conversation_title": r.conversation_title,
            "sender_name": r.sender_name,
            "subject": r.subject,
            "text": r.text,
            "date": iso_utc(r.date),
        }
        for r in msg_rows
    ]

    # Conversations — visible ones, most recently active first
    conversations = [
        conversation_json(c) for c in (await db.execute(
            select(Conversation)
            .where(
                Conversation.is_hidden == False,  # noqa: E712
                Conversation.is_deactivated == False,  # noqa: E712
                or_(crud.contains_ci(Conversation.title, q), crud.contains_ci(Conversation.username, q)),
            )
            .order_by(desc(Conversation.last_activity_at), Conversation.title)
            .limit(limit)
        )).scalars().all()
    ]

    # People — by any identity's name / handle / phone, or the person's own name / notes
    matching_identities = select(Identity.person_id).where(or_(
        crud.contains_ci(Identity.display_name, q),
        crud.contains_ci(Identity.username, q),
        crud.contains_ci(Identity.phone, q),
        crud.contains_ci(Identity.external_id, q),
    ))
    people_rows = (await db.execute(
        select(Person)
        .where(or_(
            Person.id.in_(matching_identities),
            crud.contains_ci(Person.display_name, q),
            crud.contains_ci(Person.notes, q),
        ))
        .order_by(Person.is_important.desc(), Person.name)
        .limit(limit)
    )).scalars().all()
    identities = await crud.identities_for_people(db, [p.id for p in people_rows])
    positions: dict[int, list] = {}
    if people_rows:
        for pos in (await db.execute(
            select(PersonPosition).where(PersonPosition.person_id.in_([p.id for p in people_rows]))
        )).scalars().all():
            positions.setdefault(pos.person_id, []).append(_position_out(pos))
    people = [
        {**person_out(p, identities.get(p.id, [])), "positions": positions.get(p.id, [])}
        for p in people_rows
    ]

    organizations = [
        {"id": o.id, "name": o.name, "context": (o.context[:100] if o.context else None)}
        for o in (await db.execute(
            select(Organization)
            .where(or_(crud.contains_ci(Organization.name, q), crud.contains_ci(Organization.context, q)))
            .order_by(Organization.name)
            .limit(limit)
        )).scalars().all()
    ]

    return {
        "messages": messages,
        "conversations": conversations,
        "people": people,
        "organizations": organizations,
    }
