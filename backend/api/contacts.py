from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import Contact, ContactPosition, Message, Organization
from backend.db import crud
from backend.api.validators import validate_linkedin_url
from backend.telegram.client import get_client, ensure_connected

router = APIRouter()


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
    organization: str
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
        "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
        "last_outgoing_at": c.last_outgoing_at.isoformat() if c.last_outgoing_at else None,
        "last_incoming_at": c.last_incoming_at.isoformat() if c.last_incoming_at else None,
        "synced_at": c.synced_at,
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
        d["positions"] = [
            {
                "id": p.id,
                "organization": p.organization,
                "title": p.title,
                "is_active": p.is_active,
                "ended_at": p.ended_at.isoformat() if p.ended_at else None,
            }
            for p in positions
        ]
    return d


async def _get_positions(db: AsyncSession, contact_id: int = None, telegram_id: int = None) -> List[ContactPosition]:
    """Fetch positions by telegram_id (preferred) or contact_id (legacy lookup via Contact)."""
    if telegram_id is not None:
        result = await db.execute(
            select(ContactPosition)
            .where(ContactPosition.telegram_id == telegram_id)
            .order_by(ContactPosition.organization)
        )
    elif contact_id is not None:
        # Legacy: resolve contact_id to telegram_id first
        contact = (await db.execute(
            select(Contact.telegram_id).where(Contact.id == contact_id)
        )).scalar_one_or_none()
        if not contact:
            return []
        result = await db.execute(
            select(ContactPosition)
            .where(ContactPosition.telegram_id == contact)
            .order_by(ContactPosition.organization)
        )
    else:
        return []
    return list(result.scalars().all())


# --- Endpoints ---

@router.post("/sync")
async def sync_contacts(db: AsyncSession = Depends(get_db)):
    """Import contacts from Telegram into the local DB."""
    client = get_client()
    if not client:
        raise HTTPException(status_code=503, detail="Telegram client not connected")
    await ensure_connected()

    from telethon.tl.functions.contacts import GetContactsRequest
    from datetime import datetime

    result = await client(GetContactsRequest(hash=0))
    users = result.users if hasattr(result, "users") else []

    imported = 0
    for u in users:
        if getattr(u, "bot", False):
            continue
        existing = (await db.execute(
            select(Contact).where(Contact.telegram_id == u.id)
        )).scalar_one_or_none()
        if existing:
            existing.first_name = u.first_name
            existing.last_name = u.last_name
            existing.username = u.username
            if u.phone and not existing.phone:
                existing.phone = u.phone
            existing.synced_at = datetime.utcnow()
        else:
            db.add(Contact(
                telegram_id=u.id,
                first_name=u.first_name,
                last_name=u.last_name,
                username=u.username,
                phone=u.phone,
                synced_at=datetime.utcnow(),
            ))
            imported += 1
    await db.commit()
    return {"imported": imported, "total": len(users)}


@router.get("")
async def list_contacts(
    search: str = "",
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    q = select(Contact)
    if search:
        pattern = f"%{search}%"
        from sqlalchemy import or_
        q = q.where(or_(
            Contact.first_name.ilike(pattern),
            Contact.last_name.ilike(pattern),
            Contact.username.ilike(pattern),
        ))
    q = q.order_by(Contact.first_name, Contact.last_name).limit(limit).offset(offset)
    result = await db.execute(q)
    contacts = list(result.scalars().all())

    # Batch-load positions by telegram_id
    if contacts:
        telegram_ids = [c.telegram_id for c in contacts]
        pos_result = await db.execute(
            select(ContactPosition).where(ContactPosition.telegram_id.in_(telegram_ids))
        )
        all_positions = list(pos_result.scalars().all())
        pos_map: Dict[int, List[ContactPosition]] = {}
        for p in all_positions:
            pos_map.setdefault(p.telegram_id, []).append(p)
    else:
        pos_map = {}

    return [_contact_dict(c, pos_map.get(c.telegram_id, [])) for c in contacts]


@router.get("/by-telegram/{telegram_id}")
async def get_contact_by_telegram(telegram_id: int, db: AsyncSession = Depends(get_db)):
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    positions = await _get_positions(db, telegram_id=contact.telegram_id)
    return _contact_dict(contact, positions)


@router.post("/by-telegram/{telegram_id}/important")
async def set_contact_important_by_telegram(
    telegram_id: int,
    data: ImportantRequest,
    db: AsyncSession = Depends(get_db),
):
    """Get-or-create a contact by telegram_id and set its importance flag."""
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    if not contact:
        # Auto-create a minimal contact record
        contact = Contact(telegram_id=telegram_id)
        db.add(contact)
        await db.flush()
    contact.is_important = data.is_important
    await db.commit()
    await db.refresh(contact)
    positions = await _get_positions(db, telegram_id=contact.telegram_id)
    return _contact_dict(contact, positions)


@router.post("")
async def create_contact(
    data: ContactCreate,
    db: AsyncSession = Depends(get_db),
):
    existing = (await db.execute(
        select(Contact).where(Contact.telegram_id == data.telegram_id)
    )).scalar_one_or_none()
    if existing:
        positions = await _get_positions(db, telegram_id=existing.telegram_id)
        return _contact_dict(existing, positions)
    contact = Contact(
        telegram_id=data.telegram_id,
        first_name=data.first_name,
        last_name=data.last_name,
        username=data.username,
    )
    db.add(contact)
    await db.commit()
    await db.refresh(contact)
    return _contact_dict(contact, [])


@router.get("/{contact_id}")
async def get_contact(contact_id: int, db: AsyncSession = Depends(get_db)):
    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    positions = await _get_positions(db, telegram_id=contact.telegram_id)
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
    positions = await _get_positions(db, telegram_id=contact.telegram_id)
    return _contact_dict(contact, positions)


# --- Positions ---

@router.post("/{contact_id}/positions")
async def add_position(
    contact_id: int,
    data: PositionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Add a position. contact_id here is the Telegram ID of the person."""
    telegram_id = contact_id  # route param is the telegram_id
    org_name = data.organization.strip()
    # Auto-create Organization if it doesn't exist
    existing_org = (await db.execute(
        select(Organization).where(Organization.name == org_name)
    )).scalar_one_or_none()
    if not existing_org:
        db.add(Organization(name=org_name))

    ended_at_val = None
    if not data.is_active and data.ended_at and data.ended_at.strip():
        try:
            ended_at_val = datetime.fromisoformat(data.ended_at.strip())
        except ValueError:
            pass  # invalid date → leave as None

    pos = ContactPosition(
        telegram_id=telegram_id,
        organization=org_name,
        title=data.title.strip() if data.title else None,
        is_active=data.is_active,
        ended_at=ended_at_val,
    )
    db.add(pos)
    try:
        await db.commit()
        await db.refresh(pos)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Position already exists")
    return {
        "id": pos.id,
        "organization": pos.organization,
        "title": pos.title,
        "is_active": pos.is_active,
        "ended_at": pos.ended_at.isoformat() if pos.ended_at else None,
    }


@router.put("/{contact_id}/positions/{position_id}")
async def update_position(
    contact_id: int,
    position_id: int,
    data: PositionUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Update a position. contact_id here is the Telegram ID of the person."""
    telegram_id = contact_id
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
        # Parse ISO date string; empty string clears the date
        if data.ended_at.strip():
            try:
                pos.ended_at = datetime.fromisoformat(data.ended_at.strip())
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid ended_at date format")
        else:
            pos.ended_at = None
    elif data.is_active is True:
        # Reactivating: clear ended_at
        pos.ended_at = None
    await db.commit()
    await db.refresh(pos)
    return {
        "id": pos.id,
        "organization": pos.organization,
        "title": pos.title,
        "is_active": pos.is_active,
        "ended_at": pos.ended_at.isoformat() if pos.ended_at else None,
    }


@router.delete("/{contact_id}/positions/{position_id}")
async def remove_position(
    contact_id: int,
    position_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Remove a position. contact_id here is the Telegram ID of the person."""
    telegram_id = contact_id
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


@router.get("/{contact_id}/positions")
async def get_positions_for_telegram_id(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get all positions for a telegram_id. contact_id param is actually telegram_id."""
    positions = await _get_positions(db, telegram_id=contact_id)
    return [
        {
            "id": p.id,
            "organization": p.organization,
            "title": p.title,
            "is_active": p.is_active,
            "ended_at": p.ended_at.isoformat() if p.ended_at else None,
        }
        for p in positions
    ]


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
        primary = {"title": p.title, "organization": p.organization}
    return {
        "telegram_id": c.telegram_id,
        "name": f"{c.first_name or ''} {c.last_name or ''}".strip() or str(c.telegram_id),
        "username": c.username,
        "is_important": c.is_important,
        "contact_id": c.id,
        "relationship_status": getattr(c, "relationship_status", None),
        "last_interaction_at": getattr(c, "last_interaction_at", None),
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
    row = (await db.execute(
        select(Message.sender_name)
        .where(Message.sender_id == telegram_id, Message.sender_name.isnot(None))
        .order_by(desc(Message.date))
        .limit(1)
    )).first()
    name = row[0] if row else str(telegram_id)
    return {
        "telegram_id": telegram_id,
        "name": name,
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

    # Batch-load positions by telegram_id
    pos_map: Dict[int, List[ContactPosition]] = {}
    if contacts:
        tids = [c.telegram_id for c in contacts.values()]
        pos_result = await db.execute(
            select(ContactPosition).where(ContactPosition.telegram_id.in_(tids))
        )
        for p in pos_result.scalars().all():
            pos_map.setdefault(p.telegram_id, []).append(p)

    # For missing IDs, try Message.sender_name fallback
    missing = [tid for tid in ids if tid not in contacts]
    name_map: Dict[int, str] = {}
    if missing:
        from sqlalchemy import literal_column
        # Get most recent sender_name for each missing telegram_id
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

    # Build results
    out: Dict[int, dict] = {}
    for tid in ids:
        if tid in contacts:
            c = contacts[tid]
            out[tid] = _person_summary(c, pos_map.get(c.telegram_id, []))
        else:
            out[tid] = {
                "telegram_id": tid,
                "name": name_map.get(tid, str(tid)),
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
    return out
