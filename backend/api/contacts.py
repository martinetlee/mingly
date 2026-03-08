from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, delete, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import Contact, ContactPosition
from backend.telegram.client import get_client, ensure_connected

router = APIRouter()


# --- Pydantic models ---

class ContactUpdate(BaseModel):
    twitter_handle: Optional[str] = None
    linkedin_url: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None


class PositionCreate(BaseModel):
    organization: str
    title: Optional[str] = None


# --- Helpers ---

def _contact_dict(c: Contact, positions: list[ContactPosition] | None = None) -> dict:
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
        "synced_at": c.synced_at,
    }
    if positions is not None:
        d["positions"] = [
            {"id": p.id, "organization": p.organization, "title": p.title}
            for p in positions
        ]
    return d


async def _get_positions(db: AsyncSession, contact_id: int) -> list[ContactPosition]:
    result = await db.execute(
        select(ContactPosition)
        .where(ContactPosition.contact_id == contact_id)
        .order_by(ContactPosition.organization)
    )
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

    # Batch-load positions
    if contacts:
        contact_ids = [c.id for c in contacts]
        pos_result = await db.execute(
            select(ContactPosition).where(ContactPosition.contact_id.in_(contact_ids))
        )
        all_positions = list(pos_result.scalars().all())
        pos_map: dict[int, list[ContactPosition]] = {}
        for p in all_positions:
            pos_map.setdefault(p.contact_id, []).append(p)
    else:
        pos_map = {}

    return [_contact_dict(c, pos_map.get(c.id, [])) for c in contacts]


@router.get("/{contact_id}")
async def get_contact(contact_id: int, db: AsyncSession = Depends(get_db)):
    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    positions = await _get_positions(db, contact.id)
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
        contact.linkedin_url = data.linkedin_url.strip() or None
    if data.phone is not None:
        contact.phone = data.phone.strip() or None
    if data.notes is not None:
        contact.notes = data.notes.strip() or None
    await db.commit()
    await db.refresh(contact)
    positions = await _get_positions(db, contact.id)
    return _contact_dict(contact, positions)


# --- Positions ---

@router.post("/{contact_id}/positions")
async def add_position(
    contact_id: int,
    data: PositionCreate,
    db: AsyncSession = Depends(get_db),
):
    contact = (await db.execute(
        select(Contact).where(Contact.id == contact_id)
    )).scalar_one_or_none()
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    pos = ContactPosition(
        contact_id=contact_id,
        organization=data.organization.strip(),
        title=data.title.strip() if data.title else None,
    )
    db.add(pos)
    try:
        await db.commit()
        await db.refresh(pos)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Position already exists")
    return {"id": pos.id, "organization": pos.organization, "title": pos.title}


@router.delete("/{contact_id}/positions/{position_id}")
async def remove_position(
    contact_id: int,
    position_id: int,
    db: AsyncSession = Depends(get_db),
):
    pos = (await db.execute(
        select(ContactPosition).where(
            ContactPosition.id == position_id,
            ContactPosition.contact_id == contact_id,
        )
    )).scalar_one_or_none()
    if not pos:
        raise HTTPException(status_code=404, detail="Position not found")
    await db.delete(pos)
    await db.commit()
    return {"success": True}
