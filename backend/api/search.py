"""Global local search (⌘K): messages, conversations, people and
organizations across every channel. Telegram's directory search lives at
/api/telegram/search."""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.views import conversation_json
from backend.db import crud
from backend.db.database import get_db
from backend.db.models import PersonPosition
from backend.serialization import iso_utc
from backend.serializers import person_out

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
        for r in await crud.search_message_text(db, q, limit)
    ]
    conversations = [conversation_json(c) for c in await crud.search_conversations(db, q, limit)]

    people_rows = await crud.search_people(db, q, limit)
    ids = [p.id for p in people_rows]
    identities = await crud.identities_for_people(db, ids)
    positions = await crud.positions_for_people(db, ids)
    people = [
        {**person_out(p, identities.get(p.id, [])), "positions": [_position_out(x) for x in positions.get(p.id, [])]}
        for p in people_rows
    ]

    organizations = [
        {"id": o.id, "name": o.name, "context": (o.context[:100] if o.context else None)}
        for o in await crud.search_organizations(db, q, limit)
    ]
    return {
        "messages": messages,
        "conversations": conversations,
        "people": people,
        "organizations": organizations,
    }
