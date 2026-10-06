"""Catch Up triage API -- snooze, remind-later, mark-handled, add note."""

import json
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, desc, and_, or_, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import CatchUpItem, CatchUpSummary, Contact, Message
from backend.db import crud
from backend.serialization import iso_utc

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------

class SnoozeRequest(BaseModel):
    hours: int = 24


class NoteRequest(BaseModel):
    # An empty/blank note is ignored (never silently erases the stored note).
    # To remove the note send {"clear": true} or {"note": null}.
    note: Optional[str] = None
    clear: bool = False


class RelationshipStatusRequest(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def item_dict(item: CatchUpItem) -> dict:
    return {
        "id": item.id,
        "chat_telegram_id": item.chat_telegram_id,
        "contact_id": item.contact_id,
        "status": item.status,
        "note": item.note,
        "snoozed_until": iso_utc(item.snoozed_until),
        "handled_at": iso_utc(item.handled_at),
        "created_at": iso_utc(item.created_at),
        "updated_at": iso_utc(item.updated_at),
    }


async def _get_or_create_item(
    db: AsyncSession, chat_telegram_id: int,
) -> CatchUpItem:
    # INSERT OR IGNORE: two concurrent first actions on a chat can't collide
    # on the unique chat_telegram_id.
    now = datetime.utcnow()
    await db.execute(
        sqlite_insert(CatchUpItem)
        .values(chat_telegram_id=chat_telegram_id, status="pending", created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=["chat_telegram_id"])
    )
    return (await db.execute(
        select(CatchUpItem).where(CatchUpItem.chat_telegram_id == chat_telegram_id)
    )).scalar_one()


async def _save(db: AsyncSession, item: CatchUpItem) -> dict:
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return item_dict(item)


async def contexts_for(db: AsyncSession, chat_ids: list[int]) -> dict[int, dict]:
    """Stored catch-up context for many chats in two queries."""
    if not chat_ids:
        return {}
    summaries = {
        s.chat_telegram_id: s
        for s in (await db.execute(
            select(CatchUpSummary).where(CatchUpSummary.chat_telegram_id.in_(chat_ids))
        )).scalars().all()
    }
    totals = dict((await db.execute(
        select(Message.chat_id, func.count(Message.id))
        .where(Message.chat_id.in_(list(summaries)))
        .group_by(Message.chat_id)
    )).all()) if summaries else {}
    out: dict[int, dict] = {}
    for cid in chat_ids:
        s = summaries.get(cid)
        if s is None:
            out[cid] = {
                "summary": None,
                "summary_messages": [],
                "recent_messages": [],
                "last_activity_at": None,
                "generated_at": None,
                "new_messages_since_summary": 0,
                "degraded": False,
            }
            continue
        out[cid] = {
            "summary": s.summary,
            "summary_messages": json.loads(s.summary_messages_json) if s.summary_messages_json else [],
            "recent_messages": json.loads(s.recent_messages_json) if s.recent_messages_json else [],
            "last_activity_at": iso_utc(s.last_activity_at),
            "generated_at": iso_utc(s.generated_at),
            "new_messages_since_summary": max(0, totals.get(cid, 0) - (s.total_messages_at_generation or 0)),
            # True = basic rule-based summary written while the AI model was unavailable
            "degraded": bool(s.is_degraded),
        }
    return out


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
    return [item_dict(i) for i in result.scalars().all()]


@router.get("/all")
async def list_all_catchup_items(db: AsyncSession = Depends(get_db)):
    """Return all catchup items regardless of status."""
    result = await db.execute(
        select(CatchUpItem).order_by(desc(CatchUpItem.updated_at))
    )
    return [item_dict(i) for i in result.scalars().all()]


@router.get("/contexts")
async def get_catchup_contexts(
    chat_ids: str = Query("", description="Comma-separated chat telegram ids"),
    db: AsyncSession = Depends(get_db),
):
    """Bulk version of /{chat_telegram_id}/context: {chat_id: context}.
    ``summary`` is null for chats with no generated summary yet."""
    try:
        ids = [int(x) for x in chat_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="chat_ids must be comma-separated integers")
    ctx = await contexts_for(db, ids[:200])
    return {str(k): v for k, v in ctx.items()}


class GenerateSummariesRequest(BaseModel):
    chat_ids: Optional[List[int]] = None


@router.post("/generate-summaries", status_code=202)
async def trigger_generate_summaries(req: GenerateSummariesRequest = GenerateSummariesRequest()):
    """Start catch-up summary generation in the background (single-flight with
    the scheduler). Explicit ``chat_ids`` are regenerated even if unchanged;
    otherwise the current needs-reply chats are refreshed.

    Returns the job status immediately; poll GET /generate-summaries/status
    or listen for the ``catchup_summaries_updated`` WebSocket event."""
    from backend.catchup_summary_scheduler import start_generation

    state, started = start_generation(req.chat_ids, force=req.chat_ids is not None)
    return {**state, "started": started}


@router.get("/generate-summaries/status")
async def generate_summaries_status():
    from backend.catchup_summary_scheduler import get_job_status
    return get_job_status()


@router.post("/{chat_telegram_id}/snooze")
async def snooze_item(
    chat_telegram_id: int,
    req: SnoozeRequest,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "snoozed"
    item.snoozed_until = datetime.utcnow() + timedelta(hours=req.hours)
    return await _save(db, item)


@router.post("/{chat_telegram_id}/handled")
async def mark_handled(
    chat_telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "handled"
    item.handled_at = datetime.utcnow()
    return await _save(db, item)


@router.post("/{chat_telegram_id}/reopen")
async def reopen_item(
    chat_telegram_id: int,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    item.status = "pending"
    item.snoozed_until = None
    item.handled_at = None
    return await _save(db, item)


@router.post("/{chat_telegram_id}/note")
async def add_note(
    chat_telegram_id: int,
    req: NoteRequest,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, chat_telegram_id)
    text = (req.note or "").strip()
    if req.clear or ("note" in req.model_fields_set and req.note is None):
        item.note = None
    elif text:
        item.note = text
    else:
        await db.commit()  # blank note: no change
        return item_dict(item)
    return await _save(db, item)


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
    ctx = (await contexts_for(db, [chat_telegram_id]))[chat_telegram_id]
    if ctx["summary"] is None:
        ctx["summary"] = "Summary not yet generated. Click refresh to generate now."
    return ctx
