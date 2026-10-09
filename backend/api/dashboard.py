from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.catchup import contexts_for, item_dict
from backend.api.people import people_payload
from backend.briefing.pipeline import entry_dict
from backend.db import crud
from backend.db.database import get_db
from backend.db.models import BriefingEntry, BriefingRun, Conversation, Identity, Message, Person
from backend.serialization import iso_utc
from backend.serializers import conversation_out

router = APIRouter()


def _needs_reply_item(row, contexts: dict) -> dict:
    conv, sender_name, text, date, item = row
    return {
        "conversation": conversation_out(conv),
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
    # --- Stats ---
    # Unread today: incoming messages of the last 24h in tracked conversations
    # or direct conversations with important people
    cutoff_24h = datetime.utcnow() - timedelta(hours=24)
    important_peers = (
        select(Identity.id).join(Person, Person.id == Identity.person_id)
        .where(Person.is_important == True)  # noqa: E712
    )
    relevant = or_(
        Conversation.is_tracked == True,  # noqa: E712
        and_(crud.one_to_one_filter(), Conversation.peer_identity_id.in_(important_peers)),
    )
    unread_today = (await db.execute(
        select(func.count()).select_from(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(
            Message.date >= cutoff_24h,
            Message.is_outgoing == False,  # noqa: E712
            Message.kind == "message",
            Conversation.is_hidden == False,  # noqa: E712
            Conversation.is_deactivated == False,  # noqa: E712
            relevant,
        )
    )).scalar() or 0

    # --- Needs Reply ---
    # Triage (handled / snoozed) is applied server-side before the limit, so
    # the queue can't empty out while conversations are still waiting.
    active_rows, handled_rows = await crud.get_needs_reply(db, limit=10)
    contexts = await contexts_for(db, [r[0].id for r in active_rows + handled_rows])

    # --- Attention people: important people, statuses recomputed live so a
    # stale stored needs_reply never shows ---
    attention_people = await people_payload(db, await crud.get_important_people(db, limit=10), live_status=True)

    # --- Latest Briefing ---
    latest_run = (await db.execute(
        select(BriefingRun)
        .where(BriefingRun.status.in_(("done", "partial")))
        .order_by(desc(BriefingRun.generated_at)).limit(1)
    )).scalar_one_or_none()
    latest_briefing = None
    if latest_run:
        entries = (await db.execute(
            select(BriefingEntry)
            .where(BriefingEntry.run_id == latest_run.id)
            .order_by(BriefingEntry.id)
            .limit(5)
        )).scalars().all()
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
