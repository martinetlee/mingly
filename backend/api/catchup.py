"""Catch Up triage API -- snooze, remind-later, mark-handled, add note."""

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, asc, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import CatchUpItem, CatchUpSummary, Contact, Message
from backend.db import crud

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class SnoozeRequest(BaseModel):
    hours: int = 24


class NoteRequest(BaseModel):
    note: str


class RelationshipStatusRequest(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _item_dict(item: CatchUpItem) -> dict:
    return {
        "id": item.id,
        "chat_telegram_id": item.chat_telegram_id,
        "contact_id": item.contact_id,
        "status": item.status,
        "note": item.note,
        "snoozed_until": item.snoozed_until.isoformat() + "Z" if item.snoozed_until else None,
        "handled_at": item.handled_at.isoformat() + "Z" if item.handled_at else None,
        "created_at": item.created_at.isoformat() + "Z" if item.created_at else None,
        "updated_at": item.updated_at.isoformat() + "Z" if item.updated_at else None,
    }


async def _get_or_create_item(
    db: AsyncSession, chat_telegram_id: int,
) -> CatchUpItem:
    result = await db.execute(
        select(CatchUpItem).where(CatchUpItem.chat_telegram_id == chat_telegram_id)
    )
    item = result.scalar_one_or_none()
    if item:
        return item
    item = CatchUpItem(
        chat_telegram_id=chat_telegram_id,
        status="pending",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/items")
async def list_catchup_items(db: AsyncSession = Depends(get_db)):
    """Return active (pending or un-snoozed) catchup items."""
    now = datetime.utcnow()
    result = await db.execute(
        select(CatchUpItem).where(
            or_(
                CatchUpItem.status == "pending",
                and_(
                    CatchUpItem.status == "snoozed",
                    CatchUpItem.snoozed_until <= now,
                ),
            )
        ).order_by(desc(CatchUpItem.updated_at))
    )
    return [_item_dict(i) for i in result.scalars().all()]


@router.get("/all")
async def list_all_catchup_items(db: AsyncSession = Depends(get_db)):
    """Return all catchup items regardless of status."""
    result = await db.execute(
        select(CatchUpItem).order_by(desc(CatchUpItem.updated_at))
    )
    return [_item_dict(i) for i in result.scalars().all()]


@router.post("/{chat_telegram_id}/snooze")
async def snooze_item(
    chat_telegram_id: int,
    req: SnoozeRequest,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "snoozed"
    item.snoozed_until = datetime.utcnow() + timedelta(hours=req.hours)
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.post("/{chat_telegram_id}/handled")
async def mark_handled(
    chat_telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "handled"
    item.handled_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.post("/{chat_telegram_id}/reopen")
async def reopen_item(
    chat_telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "pending"
    item.snoozed_until = None
    item.handled_at = None
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.post("/{chat_telegram_id}/note")
async def add_note(
    chat_telegram_id: int,
    req: NoteRequest,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.note = req.note.strip() or None
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return _item_dict(item)


@router.post("/{chat_telegram_id}/relationship-status")
async def update_relationship_from_catchup(
    chat_telegram_id: int,
    req: RelationshipStatusRequest,
    db: AsyncSession = Depends(get_db),
):
    if req.status not in crud.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(crud.VALID_STATUSES))}",
        )
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == chat_telegram_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found for this chat")
    contact.relationship_status = req.status
    contact.relationship_status_manual = True
    await db.commit()
    await db.refresh(contact)
    return {
        "contact_id": contact.id,
        "relationship_status": contact.relationship_status,
    }


@router.get("/{chat_telegram_id}/context")
async def get_catchup_context(
    chat_telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return stored catch-up context for a chat. If a pre-computed summary
    exists, serve it along with the count of new messages since the summary
    was generated. Falls back to a minimal response if no summary exists yet."""

    summary_row = (await db.execute(
        select(CatchUpSummary).where(
            CatchUpSummary.chat_telegram_id == chat_telegram_id
        )
    )).scalar_one_or_none()

    if not summary_row:
        return {
            "summary": "Summary not yet generated. Click refresh to generate now.",
            "summary_messages": [],
            "recent_messages": [],
            "last_activity_at": None,
            "generated_at": None,
            "new_messages_since_summary": 0,
        }

    # Count current total messages to determine how many are new
    total_now_q = (
        select(func.count(Message.id))
        .where(Message.chat_id == chat_telegram_id)
    )
    total_now = (await db.execute(total_now_q)).scalar_one()
    new_messages = max(0, total_now - summary_row.total_messages_at_generation)

    return {
        "summary": summary_row.summary,
        "summary_messages": json.loads(summary_row.summary_messages_json) if summary_row.summary_messages_json else [],
        "recent_messages": json.loads(summary_row.recent_messages_json) if summary_row.recent_messages_json else [],
        "last_activity_at": summary_row.last_activity_at.isoformat() + "Z" if summary_row.last_activity_at else None,
        "generated_at": summary_row.generated_at.isoformat() + "Z" if summary_row.generated_at else None,
        "new_messages_since_summary": new_messages,
    }


@router.post("/generate-summaries")
async def trigger_generate_summaries():
    """Manually trigger catch-up summary generation for all active items."""
    from backend.catchup_summary_scheduler import generate_all_summaries

    count = await generate_all_summaries()
    return {"success": True, "summaries_generated": count}
