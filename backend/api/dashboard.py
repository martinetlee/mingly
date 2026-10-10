"""Home dashboard (/api/dashboard): today's stats, the needs-reply queue,
important people and the latest briefing."""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.catchup import contexts_for, item_dict
from backend.api.people import people_payload
from backend.api.views import conversation_json
from backend.briefing.pipeline import entry_dict
from backend.db import crud
from backend.db.database import get_db
from backend.serialization import iso_utc

router = APIRouter()


def _needs_reply_item(row, contexts: dict) -> dict:
    conv, sender_name, text, date, item = row
    return {
        "conversation": conversation_json(conv),
        "last_message": {
            "sender_name": sender_name,
            "text": text,
            "date": iso_utc(date),
        },
        "triage": item_dict(item) if item is not None else None,
        "context": contexts.get(conv.id),
    }


@router.get("")
async def get_dashboard(db: AsyncSession = Depends(get_db)):
    # Unread today: incoming messages of the last 24h in tracked conversations
    # or direct conversations with important people
    unread_today = await crud.count_relevant_incoming_since(db, datetime.utcnow() - timedelta(hours=24))

    # Needs reply: triage (handled / snoozed) is applied server-side before the
    # limit, so the queue can't empty out while conversations are still waiting.
    active_rows, handled_rows = await crud.get_needs_reply(db, limit=10)
    contexts = await contexts_for(db, [r[0].id for r in active_rows + handled_rows])

    # Important people, statuses recomputed live so a stale stored needs_reply never shows
    attention_people = await people_payload(db, await crud.get_important_people(db, limit=10), live_status=True)

    latest_briefing = None
    latest_run = await crud.latest_finished_briefing_run(db)
    if latest_run:
        entries = (await crud.get_briefing_run_entries(db, latest_run.id))[:5]
        latest_briefing = {
            "id": latest_run.id,
            "generated_at": iso_utc(latest_run.generated_at),
            "days": latest_run.days,
            "entries": [entry_dict(e) for e in entries],
        }

    return {
        "stats": {"unread_today": unread_today},
        "needs_reply": [_needs_reply_item(r, contexts) for r in active_rows],
        "recently_handled": [_needs_reply_item(r, contexts) for r in handled_rows],
        "attention_people": attention_people,
        "latest_briefing": latest_briefing,
    }
