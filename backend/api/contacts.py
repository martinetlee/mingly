from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, desc, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import Contact, ContactPosition, Message, Organization
from backend.db import crud
from backend.api.validators import validate_linkedin_url
from backend.serialization import iso_utc
from backend.telegram.runtime import runtime

router = APIRouter()
# Mounted at /api/people: person-scoped routes keyed by telegram_id (works for
# any Telegram user, saved contact or not). Local DB only — never Telegram.
people_router = APIRouter()


# --- Pydantic models ---

class ContactUpdate(BaseModel):
    twitter_handle: Optional[str] = None
    linkedin_url: Optional[str] = None
    phone: Optional[str] = None
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
    ended_at: Optional[str] = None  # ISO date string or null


class ContactCreate(BaseModel):
    telegram_id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None


# --- Helpers ---

def _position_dict(p: ContactPosition) -> dict:
    return {
        "id": p.id,
        "organization_id": p.organization_id,  # null = detached (org deleted, history kept)
        "organization": p.organization,
        "title": p.title,
        "is_active": p.is_active,
        "ended_at": iso_utc(p.ended_at),
    }


def _contact_dict(c: Contact, positions: Optional[List[ContactPosition]] = None) -> dict:
    d = {
        "id": c.id,
        "telegram_id": c.telegram_id,
        "first_name": c.first_name,
        "last_name": c.last_name,
        "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or str(c.telegram_id),
        "username": c.username,
        "phone": c.phone,
        "twitter_handle": c.twitter_handle,
        "linkedin_url": c.linkedin_url,
        "notes": c.notes,
        "is_important": c.is_important,
        "relationship_status": c.relationship_status,
        "relationship_status_manual": c.relationship_status_manual,
        "last_interaction_at": iso_utc(c.last_interaction_at),
        "last_outgoing_at": iso_utc(c.last_outgoing_at),
        "last_incoming_at": iso_utc(c.last_incoming_at),
        "synced_at": iso_utc(c.synced_at),
    }
    # Add status explanation
    status_info = crud.explain_relationship_status(
        status=c.relationship_status,
        is_manual=c.relationship_status_manual,
        last_interaction=c.last_interaction_at,
        last_outgoing=c.last_outgoing_at,
        last_incoming=c.last_incoming_at,
        is_important=c.is_important,
    )
    d.update(status_info)
    if positions is not None:
        d["positions"] = [_position_dict(p) for p in positions]
    return d


def _positions_query():
    # Current first, then by org name — the first active one is the "primary" position
    return select(ContactPosition).order_by(
        ContactPosition.is_active.desc(), func.lower(ContactPosition.organization), ContactPosition.id
    )


async def _get_positions(db: AsyncSession, telegram_id: int) -> List[ContactPosition]:
    result = await db.execute(_positions_query().where(ContactPosition.telegram_id == telegram_id))
    return list(result.scalars().all())


async def _positions_by_person(db: AsyncSession, telegram_ids: List[int]) -> Dict[int, List[ContactPosition]]:
    pos_map: Dict[int, List[ContactPosition]] = {}
    if telegram_ids:
        result = await db.execute(_positions_query().where(ContactPosition.telegram_id.in_(telegram_ids)))
        for p in result.scalars().all():
            pos_map.setdefault(p.telegram_id, []).append(p)
    return pos_map


def _parse_date(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid ended_at date format")


# --- Endpoints ---

@router.post("/sync")
async def sync_contacts(db: AsyncSession = Depends(get_db)):
    """Import contacts from Telegram into the local DB."""
    client = await runtime.client()  # TelegramUnavailable = 503

    from telethon.tl.functions.contacts import GetContactsRequest

    result = await client(GetContactsRequest(hash=0))
    users = [u for u in (getattr(result, "users", None) or []) if not getattr(u, "bot", False)]

    existing_by_id = {
        c.telegram_id: c for c in (await db.execute(
            select(Contact).where(Contact.telegram_id.in_([u.id for u in users]))
        )).scalars().all()
    } if users else {}

    now = datetime.utcnow()
    imported = 0
    for u in users:
        existing = existing_by_id.get(u.id)
        if existing:
            existing.first_name = u.first_name
            existing.last_name = u.last_name
            existing.username = u.username
            if u.phone and not existing.phone:
                existing.phone = u.phone
            existing.synced_at = now
        else:
            db.add(Contact(
                telegram_id=u.id,
                first_name=u.first_name,
                last_name=u.last_name,
                username=u.username,
                phone=u.phone,
                synced_at=now,
            ))
            imported += 1
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Contacts changed during sync; run it again")
    return {"imported": imported, "total": len(users)}


@router.get("")
async def list_contacts(
    search: str = "",
    status: str = "",
    important: str = "",
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Paginated contacts, filtered server-side.

    Returns {items, total, limit, offset, status_counts, important_count}:
    `total` counts matches of search+status+important; `status_counts` counts
    matches of search+important per relationship status (tab badges inside the
    current view); `important_count` counts important matches of `search`
    alone (Important tab badge). `important=true` (or 1) filters to important
    contacts; anything else means no filter."""
    if status and status != "all" and status not in crud.VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(crud.VALID_STATUSES))}")
    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    filters = []
    if search.strip():
        term = search.strip()
        filters.append(or_(
            crud.contains_ci(Contact.first_name, term),
            crud.contains_ci(Contact.last_name, term),
            crud.contains_ci(Contact.username, term),
        ))
    important_count = (await db.execute(
        select(func.count()).select_from(Contact).where(*filters, Contact.is_important.is_(True))
    )).scalar() or 0
    if important.strip().lower() in ("true", "1"):
        filters.append(Contact.is_important.is_(True))
    status_counts = {
        (st or "none"): n for st, n in (await db.execute(
            select(Contact.relationship_status, func.count()).where(*filters)
            .group_by(Contact.relationship_status)
        )).all()
    }
    if status and status != "all":
        filters.append(Contact.relationship_status == status)

    total = (await db.execute(select(func.count()).select_from(Contact).where(*filters))).scalar() or 0
    contacts = list((await db.execute(
        select(Contact).where(*filters)
        .order_by(Contact.first_name, Contact.last_name, Contact.id)
        .limit(limit).offset(offset)
    )).scalars().all())

    pos_map = await _positions_by_person(db, [c.telegram_id for c in contacts])
    return {
        "items": [_contact_dict(c, pos_map.get(c.telegram_id, [])) for c in contacts],
        "total": total,
        "limit": limit,
        "offset": offset,
        "status_counts": status_counts,
        "important_count": important_count,
    }


@router.get("/by-telegram/{telegram_id}")
async def get_contact_by_telegram(telegram_id: int, db: AsyncSession = Depends(get_db)):
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    positions = await _get_positions(db, contact.telegram_id)
    return _contact_dict(contact, positions)


async def _get_or_create_contact(db: AsyncSession, telegram_id: int, **fields) -> Contact:
    """Race-safe get-or-create on the unique telegram_id."""
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    if contact:
        return contact
    contact = Contact(telegram_id=telegram_id, **fields)
    db.add(contact)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        contact = (await db.execute(
            select(Contact).where(Contact.telegram_id == telegram_id)
        )).scalar_one()
    return contact


@router.post("/by-telegram/{telegram_id}/important")
async def set_contact_important_by_telegram(
    telegram_id: int,
    data: ImportantRequest,
    db: AsyncSession = Depends(get_db),
):
    """Get-or-create a contact by telegram_id and set its importance flag."""
    contact = await _get_or_create_contact(db, telegram_id)
    contact.is_important = data.is_important
    await db.commit()
    await db.refresh(contact)
    positions = await _get_positions(db, contact.telegram_id)
    return _contact_dict(contact, positions)


@router.post("")
async def create_contact(
    data: ContactCreate,
    db: AsyncSession = Depends(get_db),
):
    """Get-or-create: re-adding an existing person returns the existing contact.
    Positions recorded before the contact existed are included."""
    contact = await _get_or_create_contact(
        db, data.telegram_id,
        first_name=data.first_name, last_name=data.last_name, username=data.username,
    )
    await db.refresh(contact)
    positions = await _get_positions(db, contact.telegram_id)
    return _contact_dict(contact, positions)


@router.get("/{contact_id}")
async def get_contact(contact_id: int, db: AsyncSession = Depends(get_db)):
    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    positions = await _get_positions(db, contact.telegram_id)
    return _contact_dict(contact, positions)


@router.put("/{contact_id}")
async def update_contact(
    contact_id: int,
    data: ContactUpdate,
    db: AsyncSession = Depends(get_db),
):
    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if data.twitter_handle is not None:
        contact.twitter_handle = data.twitter_handle.strip() or None
    if data.linkedin_url is not None:
        raw = data.linkedin_url.strip()
        if raw:
            try:
                contact.linkedin_url = validate_linkedin_url(raw)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
        else:
            contact.linkedin_url = None
    if data.phone is not None:
        contact.phone = data.phone.strip() or None
    if data.notes is not None:
        contact.notes = data.notes.strip() or None
    if data.is_important is not None:
        contact.is_important = data.is_important
    await db.commit()
    await db.refresh(contact)
    positions = await _get_positions(db, contact.telegram_id)
    return _contact_dict(contact, positions)


# --- People (local-only person view) ---

def _unknown_person(telegram_id: int, name: Optional[str]) -> dict:
    return {
        "telegram_id": telegram_id,
        "name": name or str(telegram_id),
        "username": None,
        "is_important": False,
        "contact_id": None,
        "relationship_status": None,
        "last_interaction_at": None,
        "primary_position": None,
        "phone": None,
        "twitter_handle": None,
        "linkedin_url": None,
        "has_notes": False,
    }


async def _latest_sender_name(db: AsyncSession, telegram_id: int) -> Optional[str]:
    row = (await db.execute(
        select(Message.sender_name)
        .where(Message.sender_id == telegram_id, Message.sender_name.isnot(None))
        .order_by(desc(Message.date))
        .limit(1)
    )).first()
    return row[0] if row else None


@people_router.get("/{telegram_id}")
async def get_person(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Everything Mingly stores locally about a person — works offline.

    {telegram_id, name, contact: Contact|null (with positions), positions}.
    `positions` is present even when the person isn't a saved contact."""
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    positions = await _get_positions(db, telegram_id)
    if contact:
        name = _contact_dict(contact)["name"]
    else:
        name = await _latest_sender_name(db, telegram_id) or str(telegram_id)
    return {
        "telegram_id": telegram_id,
        "name": name,
        "contact": _contact_dict(contact, positions) if contact else None,
        "positions": [_position_dict(p) for p in positions],
    }


@people_router.get("/{telegram_id}/positions")
async def list_person_positions(telegram_id: int, db: AsyncSession = Depends(get_db)):
    return [_position_dict(p) for p in await _get_positions(db, telegram_id)]


@people_router.post("/{telegram_id}/positions")
async def add_person_position(
    telegram_id: int,
    data: PositionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a position. The organization is given by id or by name (matched
    case/whitespace-insensitively; created if new)."""
    ended_at_val = None
    if not data.is_active and data.ended_at and data.ended_at.strip():
        ended_at_val = _parse_date(data.ended_at)

    if data.organization_id is not None:
        org = await db.get(Organization, data.organization_id)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
    else:
        try:
            org, _created = await crud.get_or_create_organization(db, data.organization or "")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    duplicate = (await db.execute(select(ContactPosition.id).where(
        ContactPosition.telegram_id == telegram_id, ContactPosition.organization_id == org.id,
    ))).first()
    if duplicate:
        await db.commit()  # keep a newly created org / re-attached history
        raise HTTPException(status_code=409, detail=f"Already has a position at {org.name}")

    pos = ContactPosition(
        telegram_id=telegram_id,
        organization_id=org.id,
        organization=org.name,
        title=(data.title or "").strip() or None,
        is_active=data.is_active,
        ended_at=ended_at_val,
    )
    db.add(pos)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Position already exists")
    await db.refresh(pos)
    return _position_dict(pos)


@people_router.put("/{telegram_id}/positions/{position_id}")
async def update_person_position(
    telegram_id: int,
    position_id: int,
    data: PositionUpdate,
    db: AsyncSession = Depends(get_db),
):
    pos = (await db.execute(
        select(ContactPosition).where(
            ContactPosition.id == position_id,
            ContactPosition.telegram_id == telegram_id,
        )
    )).scalar_one_or_none()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    if data.title is not None:
        pos.title = data.title.strip() or None
    if data.is_active is not None:
        pos.is_active = data.is_active
    if data.ended_at is not None:
        # Empty string clears the date
        pos.ended_at = _parse_date(data.ended_at) if data.ended_at.strip() else None
    elif data.is_active is True:
        # Reactivating: clear ended_at
        pos.ended_at = None
    await db.commit()
    await db.refresh(pos)
    return _position_dict(pos)


@people_router.delete("/{telegram_id}/positions/{position_id}")
async def delete_person_position(
    telegram_id: int,
    position_id: int,
    db: AsyncSession = Depends(get_db),
):
    pos = (await db.execute(
        select(ContactPosition).where(
            ContactPosition.id == position_id,
            ContactPosition.telegram_id == telegram_id,
        )
    )).scalar_one_or_none()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    await db.delete(pos)
    await db.commit()
    return {"success": True}


# Deprecated aliases: /api/contacts/{telegram_id}/positions[/...] — the path
# segment is a TELEGRAM id here, unlike the sibling /api/contacts/{contact_id}
# routes. Use /api/people/{telegram_id}/positions instead.
router.add_api_route("/{telegram_id}/positions", list_person_positions, methods=["GET"], deprecated=True)
router.add_api_route("/{telegram_id}/positions", add_person_position, methods=["POST"], deprecated=True)
router.add_api_route("/{telegram_id}/positions/{position_id}", update_person_position, methods=["PUT"], deprecated=True)
router.add_api_route("/{telegram_id}/positions/{position_id}", delete_person_position, methods=["DELETE"], deprecated=True)


# --- Relationship Status Endpoints ---

class StatusRequest(BaseModel):
    status: str


@router.post("/{contact_id}/relationship-status")
async def set_relationship_status(
    contact_id: int,
    req: StatusRequest,
    db: AsyncSession = Depends(get_db),
):
    if req.status not in crud.VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of: {', '.join(sorted(crud.VALID_STATUSES))}")
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    contact.relationship_status = req.status
    contact.relationship_status_manual = True
    await db.commit()
    await db.refresh(contact)
    positions = await _get_positions(db, telegram_id=contact.telegram_id)
    return _contact_dict(contact, positions)


@router.post("/{contact_id}/relationship-status/auto")
async def reset_relationship_status(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
):
    contact = (await db.execute(select(Contact).where(Contact.id == contact_id))).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    contact.relationship_status_manual = False
    await db.commit()
    updated = await crud.refresh_contact_status(db, contact.id)
    if not updated:
        raise HTTPException(status_code=404, detail="Contact not found")
    positions = await _get_positions(db, telegram_id=updated.telegram_id)
    return _contact_dict(updated, positions)


@router.post("/refresh-statuses")
async def refresh_all_statuses(db: AsyncSession = Depends(get_db)):
    count = await crud.refresh_all_contact_statuses(db)
    return {"refreshed": count}


# --- Resolve (PersonCard) ---

def _person_summary(c: Contact, positions: List[ContactPosition]) -> dict:
    """Build a person summary dict for the resolve endpoint."""
    active_positions = [p for p in positions if p.is_active]
    primary = None
    if active_positions:
        p = active_positions[0]
        primary = {"title": p.title, "organization": p.organization, "organization_id": p.organization_id}
    return {
        "telegram_id": c.telegram_id,
        "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or str(c.telegram_id),
        "username": c.username,
        "is_important": c.is_important,
        "contact_id": c.id,
        "relationship_status": getattr(c, "relationship_status", None),
        "last_interaction_at": iso_utc(c.last_interaction_at),
        "primary_position": primary,
        "phone": c.phone,
        "twitter_handle": c.twitter_handle,
        "linkedin_url": c.linkedin_url,
        "has_notes": bool(c.notes),
    }


@router.get("/resolve/{telegram_id}")
async def resolve_person(telegram_id: int, db: AsyncSession = Depends(get_db)):
    """Resolve a telegram_id to a person summary for PersonCard."""
    # Try Contact table first
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    if contact:
        positions = await _get_positions(db, telegram_id=contact.telegram_id)
        return _person_summary(contact, positions)

    # Fallback: look up sender_name from messages
    return _unknown_person(telegram_id, await _latest_sender_name(db, telegram_id))


class BatchResolveRequest(BaseModel):
    telegram_ids: List[int]


@router.post("/resolve-batch")
async def resolve_persons_batch(
    data: BatchResolveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Batch resolve multiple telegram_ids to person summaries."""
    if not data.telegram_ids:
        return {}
    ids = list(set(data.telegram_ids))[:200]  # cap at 200

    # Load contacts
    result = await db.execute(
        select(Contact).where(Contact.telegram_id.in_(ids))
    )
    contacts = {c.telegram_id: c for c in result.scalars().all()}
    pos_map = await _positions_by_person(db, list(contacts))

    # For missing IDs, use each one's most recent Message.sender_name
    missing = [tid for tid in ids if tid not in contacts]
    name_map: Dict[int, str] = {}
    if missing:
        subq = (
            select(
                Message.sender_id,
                Message.sender_name,
                func.row_number().over(
                    partition_by=Message.sender_id,
                    order_by=desc(Message.date),
                ).label("rn"),
            )
            .where(Message.sender_id.in_(missing), Message.sender_name.isnot(None))
            .subquery()
        )
        rows = (await db.execute(
            select(subq.c.sender_id, subq.c.sender_name).where(subq.c.rn == 1)
        )).all()
        for sender_id, sender_name in rows:
            name_map[sender_id] = sender_name

    return {
        tid: (_person_summary(contacts[tid], pos_map.get(tid, [])) if tid in contacts
              else _unknown_person(tid, name_map.get(tid)))
        for tid in ids
    }
