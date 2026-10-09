"""Person notes: context / due-diligence notes about a person.

Mounted at /api/people (next to the people router). Notes are written by the
user here or by an MCP agent (backend/api/mcp_tools.py); every write
broadcasts ``person_notes_changed`` so open Profile pages refresh.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import get_db
from backend.db.models import PersonNote
from backend.serialization import iso_utc
from backend.ws.manager import ws_manager

router = APIRouter()


class Citation(BaseModel):
    conversation_id: int
    message_id: int


class NoteCreate(BaseModel):
    kind: str
    title: str
    body: str = ""
    citations: Optional[List[Citation]] = None


class NoteUpdate(BaseModel):
    kind: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    citations: Optional[List[Citation]] = None


def note_dict(n: PersonNote) -> dict:
    return {
        "id": n.id,
        "person_id": n.person_id,
        "kind": n.kind,
        "title": n.title,
        "body": n.body,
        "citations": crud.note_citations(n),
        "author": n.author,
        "agent_label": n.agent_label,
        "created_at": iso_utc(n.created_at),
        "updated_at": iso_utc(n.updated_at),
    }


async def broadcast_notes_changed(person_id: int) -> None:
    await ws_manager.broadcast({"type": "person_notes_changed", "person_id": person_id})


def _citations(raw: Optional[List[Citation]]):
    return None if raw is None else [c.model_dump() for c in raw]


async def _require_person(db: AsyncSession, person_id: int) -> None:
    from backend.api.people import person_or_404  # 404 carries merged_into for merged-away people

    await person_or_404(db, person_id)


async def _get_note(db: AsyncSession, person_id: int, note_id: int) -> PersonNote:
    note = await crud.get_person_note(db, note_id)
    if note is None or note.person_id != person_id:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.get("/{person_id}/notes")
async def list_notes(person_id: int, db: AsyncSession = Depends(get_db)):
    """Newest first."""
    await _require_person(db, person_id)
    return [note_dict(n) for n in await crud.list_person_notes(db, person_id)]


@router.post("/{person_id}/notes")
async def create_note(person_id: int, data: NoteCreate, db: AsyncSession = Depends(get_db)):
    await _require_person(db, person_id)
    try:
        note = await crud.create_person_note(
            db, person_id,
            kind=data.kind, title=data.title, body=data.body,
            citations=_citations(data.citations), author="user",
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_notes_changed(person_id)
    return note_dict(note)


@router.put("/{person_id}/notes/{note_id}")
async def update_note(person_id: int, note_id: int, data: NoteUpdate, db: AsyncSession = Depends(get_db)):
    """The user may edit any note, including agent-written ones (author is kept)."""
    note = await _get_note(db, person_id, note_id)
    try:
        note = await crud.update_person_note(
            db, note,
            kind=data.kind, title=data.title, body=data.body,
            citations=_citations(data.citations),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_notes_changed(person_id)
    return note_dict(note)


@router.delete("/{person_id}/notes/{note_id}")
async def delete_note(person_id: int, note_id: int, db: AsyncSession = Depends(get_db)):
    note = await _get_note(db, person_id, note_id)
    await crud.delete_person_note(db, note)
    await broadcast_notes_changed(person_id)
    return {"success": True}
