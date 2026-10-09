"""People: the CRM record of every person Mingly knows, across channels.

Mounted at /api/people. Everything here is local data keyed by internal
``person_id`` (works offline); only ``POST /sync`` talks to a channel.
Every write broadcasts ``person_changed`` so open views refresh. Merging,
undoing a merge and detaching identities go through ``backend.people.linking``.
"""

import json
from datetime import datetime
from typing import Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Person, PersonMerge, PersonMergeSuggestion, PersonPosition
from backend.people import linking
from backend.serialization import iso_utc
from backend.serializers import channel_of, conversation_out, messages_out, person_out
from backend.ws.manager import ws_manager

router = APIRouter()


# --- Pydantic models ---

class PersonUpdate(BaseModel):
    display_name: Optional[str] = None  # "" = clear the override (name derives from identities)
    twitter_handle: Optional[str] = None
    linkedin_url: Optional[str] = None
    notes: Optional[str] = None
    is_important: Optional[bool] = None


class ImportantRequest(BaseModel):
    is_important: bool


class PositionCreate(BaseModel):
    organization: Optional[str] = None  # name; matched case-insensitively, created if new
    organization_id: Optional[int] = None  # alternative to `organization`
    title: Optional[str] = None
    is_active: bool = True
    ended_at: Optional[str] = None  # ISO date string, only used when is_active=False


class PositionUpdate(BaseModel):
    title: Optional[str] = None
    is_active: Optional[bool] = None
    ended_at: Optional[str] = None  # ISO date string or ""


class StatusRequest(BaseModel):
    status: str


class MergeRequest(BaseModel):
    other_person_id: int  # merged into the person in the path, then removed


# --- Payloads ---

def position_dict(p: PersonPosition) -> dict:
    return {
        "id": p.id,
        "person_id": p.person_id,
        "organization_id": p.organization_id,  # null = detached (org deleted, history kept)
        "organization": p.organization,
        "title": p.title,
        "is_active": p.is_active,
        "ended_at": iso_utc(p.ended_at),
    }


def primary_position(positions: List[PersonPosition]) -> Optional[dict]:
    """The first current position (positions come current-first, by org name)."""
    p = next((p for p in positions if p.is_active), None)
    if p is None:
        return None
    return {"title": p.title, "organization": p.organization, "organization_id": p.organization_id}


def status_fields(person: Person, computed: Optional[tuple] = None) -> dict:
    """Relationship status + explanation. ``computed`` (from
    crud.compute_relationship_status) overrides the stored auto status."""
    status, last_interaction, last_outgoing, last_incoming = (
        computed if computed is not None and not person.relationship_status_manual else (
            person.relationship_status, person.last_interaction_at,
            person.last_outgoing_at, person.last_incoming_at,
        )
    )
    return {
        "relationship_status": status,
        "relationship_status_manual": bool(person.relationship_status_manual),
        "last_interaction_at": iso_utc(last_interaction),
        "last_outgoing_at": iso_utc(last_outgoing),
        "last_incoming_at": iso_utc(last_incoming),
        **crud.explain_relationship_status(
            status=status,
            is_manual=bool(person.relationship_status_manual),
            last_interaction=last_interaction,
            last_outgoing=last_outgoing,
            last_incoming=last_incoming,
            is_important=bool(person.is_important),
        ),
    }


async def people_payload(db: AsyncSession, people: Iterable[Person], *, live_status: bool = False) -> list[dict]:
    """Person payloads (serializers.person_out + positions, primary_position,
    note_count, status explanation) in a constant number of queries.
    ``live_status`` recomputes auto statuses instead of using the stored ones."""
    people = list(people)
    ids = [p.id for p in people]
    identities = await crud.identities_for_people(db, ids)
    positions = await crud.positions_for_people(db, ids)
    note_counts = await crud.count_person_notes(db, ids)
    computed: dict[int, tuple] = {}
    if live_status:
        stats = await crud.interaction_stats(db, ids)
        computed = {pid: crud.status_from_stats(stats.get(pid)) for pid in ids}
    out = []
    for p in people:
        pos = positions.get(p.id, [])
        out.append({
            **person_out(p, identities.get(p.id, [])),
            **status_fields(p, computed.get(p.id)),
            "positions": [position_dict(x) for x in pos],
            "primary_position": primary_position(pos),
            "note_count": note_counts.get(p.id, 0),
        })
    return out


async def person_payload(db: AsyncSession, person: Person) -> dict:
    """One person, with their direct conversations on every channel."""
    payload = (await people_payload(db, [person]))[0]
    payload["direct_conversation_ids"] = await crud.direct_conversation_ids_for_person(db, person.id)
    return payload


async def broadcast_person_changed(person_id: int) -> None:
    await ws_manager.broadcast({"type": "person_changed", "person_id": person_id})


async def person_or_404(db: AsyncSession, person_id: int) -> Person:
    person = await crud.get_person(db, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


async def _changed(db: AsyncSession, person: Person) -> dict:
    """Commit, broadcast and return the fresh payload."""
    await db.commit()
    await db.refresh(person)
    await broadcast_person_changed(person.id)
    return await person_payload(db, person)


def _parse_date(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid date: {raw!r}")


def _check_status(status: str) -> None:
    if status not in crud.VALID_STATUSES:
        raise HTTPException(
            status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(crud.VALID_STATUSES))}",
        )


# --- Collection routes ---

@router.get("")
async def list_people(
    search: str = "",
    status: str = "",
    important: bool = False,
    contacts_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Paginated people, filtered server-side.

    Returns {items, total, limit, offset, status_counts, important_count}:
    ``total`` counts matches of every filter; ``status_counts`` counts matches
    of search+important+contacts_only per relationship status ("none" =
    undetermined; tab badges); ``important_count`` counts important matches of
    search+contacts_only (Important tab badge). ``contacts_only`` keeps people
    in a channel address book."""
    if status and status != "all":
        _check_status(status)
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    page = await crud.list_people(
        db, search=search, status=status, important=important, contacts_only=contacts_only,
        limit=limit, offset=offset,
    )
    return {
        "items": await people_payload(db, page["people"]),
        "total": page["total"],
        "limit": limit,
        "offset": offset,
        "status_counts": page["status_counts"],
        "important_count": page["important_count"],
    }


@router.post("/sync")
async def sync_address_books(db: AsyncSession = Depends(get_db)):
    """Import every connector's address book: its entries become identities
    marked ``is_contact`` (with their people); entries no longer in it are
    unmarked. Telegram is the only channel with an address book today."""
    from telethon.tl.functions.contacts import GetContactsRequest

    from backend.telegram import ids
    from backend.telegram.runtime import runtime

    client = await runtime.client()  # TelegramUnavailable = 503
    result = await client(GetContactsRequest(hash=0))
    users = list(getattr(result, "users", None) or [])

    before = await crud.identity_ids_by_external(db, ids.CHANNEL, [str(u.id) for u in users])
    was_contact = {
        iid for iid, ident in (await crud.get_identities(db, before.values())).items() if ident.is_contact
    }
    contact_ids = []
    for u in users:
        contact_ids.append((await ids.ensure_user_identity(db, u, is_contact=True)).id)
    await crud.set_address_book(db, ids.CHANNEL, contact_ids)
    await db.commit()
    return {"imported": len(set(contact_ids) - was_contact), "total": len(users)}


@router.post("/refresh-statuses")
async def refresh_all_statuses(db: AsyncSession = Depends(get_db)):
    return {"refreshed": await crud.refresh_all_person_statuses(db)}


# --- Linking: merge suggestions, merges, detach ---

async def suggestions_payload(db: AsyncSession, suggestions: List[PersonMergeSuggestion]) -> list[dict]:
    """[{id, reason, score, created_at, person_a, person_b}] with full Person payloads."""
    ids = {pid for s in suggestions for pid in (s.person_a_id, s.person_b_id)}
    people = await crud.get_people(db, ids)
    payloads = {p["id"]: p for p in await people_payload(db, people.values())}
    return [
        {
            "id": s.id,
            "reason": s.reason,
            "score": s.score,
            "created_at": iso_utc(s.created_at),
            "person_a": payloads[s.person_a_id],
            "person_b": payloads[s.person_b_id],
        }
        for s in suggestions if s.person_a_id in payloads and s.person_b_id in payloads
    ]


def merge_dict(m: PersonMerge) -> dict:
    snap = json.loads(m.snapshot_json)
    return {
        "id": m.id,
        "into_person_id": m.into_person_id,
        "from_person_id": m.from_person_id,
        "from_name": snap.get("name"),
        "identities": snap.get("identities", []),  # [{id, channel, label}] that moved
        "automatic": bool(m.automatic),
        "created_at": iso_utc(m.created_at),
        "undone_at": iso_utc(m.undone_at),
    }


async def _linked(db: AsyncSession, person_ids: Iterable[int]) -> None:
    """Commit a linking change and broadcast person_changed for every person it touched."""
    await db.commit()
    for pid in dict.fromkeys(person_ids):
        await broadcast_person_changed(pid)


@router.get("/merge-suggestions")
async def list_merge_suggestions(db: AsyncSession = Depends(get_db)):
    """Pending "maybe the same human" pairs, most likely first."""
    return await suggestions_payload(db, await crud.list_merge_suggestions(db))


@router.post("/merge-suggestions/refresh")
async def refresh_merge_suggestions(db: AsyncSession = Depends(get_db)):
    """Recompute suggestions across everyone. Returns {created}."""
    created = await linking.refresh_all_suggestions(db)
    await db.commit()
    return {"created": created}


@router.post("/merge-suggestions/{suggestion_id}/dismiss")
async def dismiss_merge_suggestion(suggestion_id: int, db: AsyncSession = Depends(get_db)):
    suggestion = await crud.get_merge_suggestion(db, suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    suggestion.status = "dismissed"
    await _linked(db, (suggestion.person_a_id, suggestion.person_b_id))
    return {"success": True}


@router.post("/merges/{merge_id}/undo")
async def undo_merge(merge_id: int, db: AsyncSession = Depends(get_db)):
    """Undo a merge. Returns the restored person (with a new id)."""
    try:
        person = await linking.undo(db, merge_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except linking.LinkingError as e:
        raise HTTPException(status_code=409, detail=str(e))
    record = await crud.get_person_merge(db, merge_id)
    await _linked(db, (record.into_person_id, person.id))
    await db.refresh(person)
    return await person_payload(db, person)


# --- One person ---

@router.get("/{person_id}")
async def get_person(person_id: int, db: AsyncSession = Depends(get_db)):
    """Everything Mingly stores locally about a person — works offline."""
    return await person_payload(db, await person_or_404(db, person_id))


@router.get("/{person_id}/conversations")
async def person_conversations(person_id: int, db: AsyncSession = Depends(get_db)):
    """The person's direct conversations on every channel, most recent first."""
    await person_or_404(db, person_id)
    ids = await crud.direct_conversation_ids_for_person(db, person_id)
    convs = await crud.get_conversations(db, ids)
    return [conversation_out(convs[i]) for i in ids if i in convs]


@router.get("/{person_id}/timeline")
async def person_timeline(
    person_id: int,
    limit: int = 50,
    before: Optional[str] = None,
    before_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
):
    """Cross-channel timeline, newest first: the person's one-to-one
    conversations (both directions), what they sent in groups, emails
    they're a participant of — ``{kind: "message", message, conversation}``
    — and meetings they attend that have started, ``{kind: "meeting",
    event}``. Paged by messages: page with ``before`` (ISO date of the oldest
    *message* item shown) and ``before_id`` (its id, breaks date ties); a
    page carries the meetings between its oldest message and ``before``."""
    from backend.api.calendar import timeline_meetings

    await person_or_404(db, person_id)
    limit = max(1, min(limit, 200))
    before_dt = _parse_date(before) if before else None
    rows, has_more = await crud.person_timeline(db, person_id, limit=limit, before=before_dt, before_id=before_id)
    convs = await crud.get_conversations(db, {r[0].conversation_id for r in rows})
    items = []
    for row, msg in zip(rows, await messages_out(db, rows)):
        conv = convs.get(row[0].conversation_id)
        items.append((row[0].date, {
            "kind": "message",
            "message": msg,
            "conversation": {
                "id": conv.id, "title": conv.title, "kind": conv.kind, "channel": channel_of(conv),
            } if conv is not None else None,
        }))
    oldest = rows[-1][0].date if has_more and rows else None
    items += await timeline_meetings(db, person_id, before=before_dt, oldest=oldest)
    items.sort(key=lambda it: it[0] or datetime.min, reverse=True)
    return {"items": [it for _, it in items], "has_more": has_more}


@router.put("/{person_id}")
async def update_person(person_id: int, data: PersonUpdate, db: AsyncSession = Depends(get_db)):
    person = await person_or_404(db, person_id)
    try:
        crud.apply_person_fields(
            person,
            display_name=data.display_name,
            twitter_handle=data.twitter_handle,
            linkedin_url=data.linkedin_url,
            notes=data.notes,
            is_important=data.is_important,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await _changed(db, person)


@router.post("/{person_id}/important")
async def set_important(person_id: int, data: ImportantRequest, db: AsyncSession = Depends(get_db)):
    person = await person_or_404(db, person_id)
    person.is_important = data.is_important
    return await _changed(db, person)


# --- Positions ---

@router.get("/{person_id}/positions")
async def list_positions(person_id: int, db: AsyncSession = Depends(get_db)):
    await person_or_404(db, person_id)
    return [position_dict(p) for p in await crud.get_person_positions(db, person_id)]


@router.post("/{person_id}/positions")
async def add_position(person_id: int, data: PositionCreate, db: AsyncSession = Depends(get_db)):
    """Add a position. The organization is given by id or by name (matched
    case/whitespace-insensitively; created if new)."""
    await person_or_404(db, person_id)
    ended_at = None
    if not data.is_active and data.ended_at and data.ended_at.strip():
        ended_at = _parse_date(data.ended_at)
    try:
        pos = await crud.add_person_position(
            db, person_id,
            organization=data.organization,
            organization_id=data.organization_id,
            title=data.title,
            is_active=data.is_active,
            ended_at=ended_at,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except crud.PositionConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await broadcast_person_changed(person_id)
    return position_dict(pos)


async def _position_or_404(db: AsyncSession, person_id: int, position_id: int) -> PersonPosition:
    pos = await crud.get_person_position(db, person_id, position_id)
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not found")
    return pos


@router.put("/{person_id}/positions/{position_id}")
async def update_position(
    person_id: int, position_id: int, data: PositionUpdate, db: AsyncSession = Depends(get_db)
):
    pos = await _position_or_404(db, person_id, position_id)
    if data.title is not None:
        pos.title = data.title.strip() or None
    if data.is_active is not None:
        pos.is_active = data.is_active
    if data.ended_at is not None:
        # Empty string clears the date
        pos.ended_at = _parse_date(data.ended_at) if data.ended_at.strip() else None
    elif data.is_active is True:
        pos.ended_at = None  # reactivating
    await db.commit()
    await db.refresh(pos)
    await broadcast_person_changed(person_id)
    return position_dict(pos)


@router.delete("/{person_id}/positions/{position_id}")
async def delete_position(person_id: int, position_id: int, db: AsyncSession = Depends(get_db)):
    pos = await _position_or_404(db, person_id, position_id)
    await db.delete(pos)
    await db.commit()
    await broadcast_person_changed(person_id)
    return {"success": True}


# --- Relationship status ---

@router.post("/{person_id}/relationship-status")
async def set_relationship_status(person_id: int, req: StatusRequest, db: AsyncSession = Depends(get_db)):
    _check_status(req.status)
    person = await person_or_404(db, person_id)
    crud.set_manual_relationship_status(person, req.status)
    return await _changed(db, person)


@router.post("/{person_id}/relationship-status/auto")
async def reset_relationship_status(person_id: int, db: AsyncSession = Depends(get_db)):
    person = await person_or_404(db, person_id)
    person.relationship_status_manual = False
    await db.commit()
    person = await crud.refresh_person_status(db, person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    await broadcast_person_changed(person_id)
    return await person_payload(db, person)


# --- Linking (one person) ---

@router.get("/{person_id}/merge-suggestions")
async def person_merge_suggestions(person_id: int, db: AsyncSession = Depends(get_db)):
    """Pending suggestions involving this person (same shape as /merge-suggestions)."""
    await person_or_404(db, person_id)
    return await suggestions_payload(db, await crud.list_merge_suggestions(db, person_id))


@router.get("/{person_id}/merges")
async def person_merges(person_id: int, db: AsyncSession = Depends(get_db)):
    """Merges into this person that can still be undone, newest first."""
    await person_or_404(db, person_id)
    return [merge_dict(m) for m in await crud.merges_into_person(db, person_id)]


@router.post("/{person_id}/merge")
async def merge_person(person_id: int, data: MergeRequest, db: AsyncSession = Depends(get_db)):
    """Merge ``other_person_id`` into this person. Returns the surviving
    person plus ``merge_id`` (for POST /merges/{merge_id}/undo)."""
    try:
        record = await linking.merge(db, person_id, data.other_person_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except linking.LinkingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await linking.suggest_for_person(db, person_id)
    await _linked(db, (person_id, data.other_person_id))
    person = await person_or_404(db, person_id)
    await db.refresh(person)
    return {**await person_payload(db, person), "merge_id": record.id}


@router.post("/{person_id}/identities/{identity_id}/detach")
async def detach_identity(person_id: int, identity_id: int, db: AsyncSession = Depends(get_db)):
    """Move one of the person's identities to a new person. Returns the new person."""
    await person_or_404(db, person_id)
    try:
        person = await linking.detach(db, identity_id, person_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except linking.LinkingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    await _linked(db, (person_id, person.id))
    await db.refresh(person)
    return await person_payload(db, person)
