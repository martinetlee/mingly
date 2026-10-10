"""Catch Up triage API -- snooze, remind-later, mark-handled, add note."""

import json
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import get_db
from backend.db.models import CatchUpItem
from backend.serialization import iso_utc
from backend.catchup_summary_scheduler import get_job_status, start_generation

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
        "conversation_id": item.conversation_id,
        "person_id": item.person_id,
        "status": item.status,
        "note": item.note,
        "snoozed_until": iso_utc(item.snoozed_until),
        "handled_at": iso_utc(item.handled_at),
        "created_at": iso_utc(item.created_at),
        "updated_at": iso_utc(item.updated_at),
    }


async def _peer_person_id(db: AsyncSession, conversation_id: int) -> Optional[int]:
    """The person on the other side of a direct conversation (404 if the
    conversation does not exist)."""
    conv = await crud.get_conversation(db, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv.peer_identity.person_id if conv.peer_identity is not None else None


async def _get_or_create_item(db: AsyncSession, conversation_id: int) -> CatchUpItem:
    return await crud.get_or_create_catchup_item(db, conversation_id, await _peer_person_id(db, conversation_id))


async def _save(db: AsyncSession, item: CatchUpItem) -> dict:
    item.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(item)
    return item_dict(item)


async def contexts_for(db: AsyncSession, conversation_ids: list[int]) -> dict[int, dict]:
    """Stored catch-up context for many conversations in two queries."""
    if not conversation_ids:
        return {}
    summaries, totals = await crud.catchup_summaries(db, conversation_ids)
    out: dict[int, dict] = {}
    for cid in conversation_ids:
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
    return [item_dict(i) for i in await crud.list_active_catchup_items(db, datetime.utcnow())]


@router.get("/all")
async def list_all_catchup_items(db: AsyncSession = Depends(get_db)):
    """Return all catchup items regardless of status."""
    return [item_dict(i) for i in await crud.list_catchup_items(db)]


@router.get("/contexts")
async def get_catchup_contexts(
    conversation_ids: str = Query("", description="Comma-separated conversation ids"),
    db: AsyncSession = Depends(get_db),
):
    """Bulk version of /{conversation_id}/context: {conversation_id: context}.
    ``summary`` is null for conversations with no generated summary yet."""
    try:
        ids = [int(x) for x in conversation_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="conversation_ids must be comma-separated integers")
    ctx = await contexts_for(db, ids[:200])
    return {str(k): v for k, v in ctx.items()}


class GenerateSummariesRequest(BaseModel):
    conversation_ids: Optional[List[int]] = None


@router.post("/generate-summaries", status_code=202)
async def trigger_generate_summaries(req: GenerateSummariesRequest = GenerateSummariesRequest()):
    """Start catch-up summary generation in the background (single-flight with
    the scheduler). Explicit ``conversation_ids`` are regenerated even if
    unchanged; otherwise the current needs-reply conversations are refreshed.

    Returns the job status immediately; poll GET /generate-summaries/status
    or listen for the ``catchup_summaries_updated`` WebSocket event."""

    state, started = start_generation(req.conversation_ids, force=req.conversation_ids is not None)
    return {**state, "started": started}


@router.get("/generate-summaries/status")
async def generate_summaries_status():
    return get_job_status()


@router.post("/{conversation_id}/snooze")
async def snooze_item(
    conversation_id: int,
    req: SnoozeRequest,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, conversation_id)
    item.status = "snoozed"
    item.snoozed_until = datetime.utcnow() + timedelta(hours=req.hours)
    return await _save(db, item)


@router.post("/{conversation_id}/handled")
async def mark_handled(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, conversation_id)
    item.status = "handled"
    item.handled_at = datetime.utcnow()
    return await _save(db, item)


@router.post("/{conversation_id}/reopen")
async def reopen_item(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, conversation_id)
    item.status = "pending"
    item.snoozed_until = None
    item.handled_at = None
    return await _save(db, item)


@router.post("/{conversation_id}/note")
async def add_note(
    conversation_id: int,
    req: NoteRequest,
    db: AsyncSession = Depends(get_db),
):
    item = await _get_or_create_item(db, conversation_id)
    text = (req.note or "").strip()
    if req.clear or ("note" in req.model_fields_set and req.note is None):
        item.note = None
    elif text:
        item.note = text
    else:
        await db.commit()  # blank note: no change
        return item_dict(item)
    return await _save(db, item)


@router.post("/{conversation_id}/relationship-status")
async def update_relationship_from_catchup(
    conversation_id: int,
    req: RelationshipStatusRequest,
    db: AsyncSession = Depends(get_db),
):
    if req.status not in crud.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(sorted(crud.VALID_STATUSES))}",
        )
    person_id = await _peer_person_id(db, conversation_id)
    person = await crud.get_person(db, person_id) if person_id is not None else None
    if person is None:
        raise HTTPException(status_code=404, detail="No person for this conversation")
    crud.set_manual_relationship_status(person, req.status)
    await db.commit()
    await db.refresh(person)
    return {
        "person_id": person.id,
        "relationship_status": person.relationship_status,
    }


@router.get("/{conversation_id}/context")
async def get_catchup_context(
    conversation_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Return stored catch-up context for a conversation. If a pre-computed summary
    exists, serve it along with the count of new messages since the summary
    was generated. Falls back to a minimal response if no summary exists yet."""
    ctx = (await contexts_for(db, [conversation_id]))[conversation_id]
    if ctx["summary"] is None:
        ctx["summary"] = "Summary not yet generated. Click refresh to generate now."
    return ctx
