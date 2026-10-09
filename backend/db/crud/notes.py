"""Person notes: context / due-diligence notes about a person, written by the
user or an MCP agent. Citations point at stored messages as
``[{conversation_id, message_id}]`` (internal ids)."""

import json
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Message, PersonNote

__all__ = [
    "NOTE_KINDS",
    "NOTE_AUTHORS",
    "NOTE_TITLE_MAX",
    "NOTE_BODY_MAX",
    "NOTE_CITATIONS_MAX",
    "normalize_citations",
    "missing_citations",
    "validate_note_fields",
    "list_person_notes",
    "count_person_notes",
    "get_person_note",
    "create_person_note",
    "update_person_note",
    "delete_person_note",
    "note_citations",
]

NOTE_KINDS = ("context", "due_diligence", "risk", "relationship", "todo")
NOTE_AUTHORS = ("user", "agent")
NOTE_TITLE_MAX = 200
NOTE_BODY_MAX = 20000
NOTE_CITATIONS_MAX = 50

_CITATION_SHAPE = "{conversation_id, message_id}"


def normalize_citations(raw) -> list:
    """[{conversation_id, message_id}] with int ids, deduplicated, order kept.
    Raises ValueError on a malformed list."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"citations must be a list of {_CITATION_SHAPE}")
    if len(raw) > NOTE_CITATIONS_MAX:
        raise ValueError(f"At most {NOTE_CITATIONS_MAX} citations per note")
    out, seen = [], set()
    for c in raw:
        if not isinstance(c, dict):
            raise ValueError(f"Each citation must be an object {_CITATION_SHAPE}")
        values = (c.get("conversation_id"), c.get("message_id"))
        if any(isinstance(v, bool) for v in values):
            raise ValueError("Each citation needs integer conversation_id and message_id")
        try:
            key = (int(values[0]), int(values[1]))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError("Each citation needs integer conversation_id and message_id")
        if key not in seen:
            seen.add(key)
            out.append({"conversation_id": key[0], "message_id": key[1]})
    return out


async def missing_citations(db: AsyncSession, citations: list) -> list:
    """Citations whose message isn't stored in that conversation."""
    if not citations:
        return []
    stored = dict((await db.execute(
        select(Message.id, Message.conversation_id).where(Message.id.in_({c["message_id"] for c in citations}))
    )).all())
    return [c for c in citations if stored.get(c["message_id"]) != c["conversation_id"]]


def validate_note_fields(kind: Optional[str], title: Optional[str], body: Optional[str]) -> None:
    """Raise ValueError for a bad kind / empty or oversized title / oversized body
    (None = not being set)."""
    if kind is not None and kind not in NOTE_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(NOTE_KINDS)}")
    if title is not None:
        if not title.strip():
            raise ValueError("title cannot be empty")
        if len(title) > NOTE_TITLE_MAX:
            raise ValueError(f"title is longer than {NOTE_TITLE_MAX} characters")
    if body is not None and len(body) > NOTE_BODY_MAX:
        raise ValueError(f"body is longer than {NOTE_BODY_MAX} characters")


async def _checked_citations(db: AsyncSession, raw) -> list:
    citations = normalize_citations(raw)
    missing = await missing_citations(db, citations)
    if missing:
        listed = ", ".join(f"{c['conversation_id']}/{c['message_id']}" for c in missing[:10])
        raise ValueError(f"Cited messages not found locally (conversation_id/message_id): {listed}")
    return citations


async def list_person_notes(db: AsyncSession, person_id: int) -> list[PersonNote]:
    """Newest first."""
    return list((await db.execute(
        select(PersonNote).where(PersonNote.person_id == person_id)
        .order_by(desc(PersonNote.created_at), desc(PersonNote.id))
    )).scalars().all())


async def count_person_notes(db: AsyncSession, person_ids: Iterable[int]) -> dict[int, int]:
    """{person_id: note count} for people with at least one note."""
    ids = list(set(person_ids))
    out: dict[int, int] = {}
    for i in range(0, len(ids), 500):
        out.update(dict((await db.execute(
            select(PersonNote.person_id, func.count()).where(PersonNote.person_id.in_(ids[i:i + 500]))
            .group_by(PersonNote.person_id)
        )).all()))
    return out


async def get_person_note(db: AsyncSession, note_id: int) -> Optional[PersonNote]:
    return await db.get(PersonNote, note_id)


async def create_person_note(
    db: AsyncSession,
    person_id: int,
    *,
    kind: str,
    title: str,
    body: str = "",
    citations=None,
    author: str = "user",
    agent_label: Optional[str] = None,
) -> PersonNote:
    """Validates fields and that every citation is stored locally (ValueError). Commits."""
    if author not in NOTE_AUTHORS:
        raise ValueError("author must be 'user' or 'agent'")
    validate_note_fields(kind, title, body)
    checked = await _checked_citations(db, citations)
    now = datetime.utcnow()
    note = PersonNote(
        person_id=person_id, kind=kind, title=title.strip(), body=body or "",
        citations_json=json.dumps(checked) if checked else None,
        author=author, agent_label=(agent_label or "").strip()[:100] or None,
        created_at=now, updated_at=now,
    )
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return note


async def update_person_note(
    db: AsyncSession,
    note: PersonNote,
    *,
    kind: Optional[str] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
    citations=None,
) -> PersonNote:
    """Partial update (None = unchanged; citations=[] clears). Commits."""
    validate_note_fields(kind, title, body)
    if citations is not None:
        checked = await _checked_citations(db, citations)
        note.citations_json = json.dumps(checked) if checked else None
    if kind is not None:
        note.kind = kind
    if title is not None:
        note.title = title.strip()
    if body is not None:
        note.body = body
    note.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(note)
    return note


async def delete_person_note(db: AsyncSession, note: PersonNote) -> None:
    await db.delete(note)
    await db.commit()


def note_citations(note: PersonNote) -> list:
    try:
        return json.loads(note.citations_json) if note.citations_json else []
    except ValueError:
        return []
