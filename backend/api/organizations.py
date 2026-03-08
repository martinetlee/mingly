from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db
from backend.db.models import Organization, Contact, ContactPosition, Message, Chat
from backend.llm.config import load_registry_config

router = APIRouter()


class OrgCreate(BaseModel):
    name: str


class OrgUpdate(BaseModel):
    name: Optional[str] = None
    context: Optional[str] = None


# --- Helpers ---

async def _org_detail(db: AsyncSession, org: Organization) -> dict:
    """Build full org dict with members and recent group messages."""
    # Members: contacts who have a position at this org (split into current and former)
    pos_result = await db.execute(
        select(ContactPosition, Contact)
        .join(Contact, ContactPosition.contact_id == Contact.id)
        .where(ContactPosition.organization == org.name)
        .order_by(Contact.first_name)
    )
    members = []
    former_members = []
    for pos, contact in pos_result.all():
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or str(contact.telegram_id)
        entry = {
            "contact_id": contact.id,
            "telegram_id": contact.telegram_id,
            "name": name,
            "username": contact.username,
            "title": pos.title,
            "ended_at": pos.ended_at.isoformat() if pos.ended_at else None,
        }
        if pos.is_active:
            members.append(entry)
        else:
            former_members.append(entry)

    # Find group chats whose name contains the org name (heuristic)
    chat_result = await db.execute(
        select(Chat)
        .where(Chat.name.ilike(f"%{org.name}%"), Chat.type.in_(["group", "channel"]))
        .limit(10)
    )
    related_chats = list(chat_result.scalars().all())

    # Latest message per related chat
    latest_messages = []
    for chat in related_chats:
        msg_result = await db.execute(
            select(Message)
            .where(Message.chat_id == chat.telegram_id)
            .order_by(desc(Message.date))
            .limit(1)
        )
        msg = msg_result.scalar_one_or_none()
        latest_messages.append({
            "chat_id": chat.telegram_id,
            "chat_name": chat.name,
            "chat_type": chat.type,
            "last_message": {
                "text": (msg.text or "")[:200] if msg else None,
                "sender_name": msg.sender_name if msg else None,
                "date": msg.date.isoformat() if msg else None,
            } if msg else None,
        })

    return {
        "id": org.id,
        "name": org.name,
        "context": org.context,
        "members": members,
        "former_members": former_members,
        "related_chats": latest_messages,
        "created_at": org.created_at.isoformat() if org.created_at else None,
    }


# --- Endpoints ---

@router.get("")
async def list_organizations(
    search: str = "",
    db: AsyncSession = Depends(get_db),
):
    q = select(Organization)
    if search:
        q = q.where(Organization.name.ilike(f"%{search}%"))
    q = q.order_by(Organization.name)
    result = await db.execute(q)
    orgs = list(result.scalars().all())

    # Lightweight list: org + member count
    out = []
    for org in orgs:
        count_result = await db.execute(
            select(func.count(ContactPosition.id))
            .where(ContactPosition.organization == org.name)
        )
        member_count = count_result.scalar() or 0
        out.append({
            "id": org.id,
            "name": org.name,
            "context": org.context,
            "member_count": member_count,
        })
    return out


@router.post("")
async def create_organization(data: OrgCreate, db: AsyncSession = Depends(get_db)):
    name = data.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    existing = (await db.execute(
        select(Organization).where(Organization.name == name)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Organization already exists")
    org = Organization(name=name)
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return await _org_detail(db, org)


@router.get("/{org_id}")
async def get_organization(org_id: int, db: AsyncSession = Depends(get_db)):
    org = (await db.execute(
        select(Organization).where(Organization.id == org_id)
    )).scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return await _org_detail(db, org)


@router.put("/{org_id}")
async def update_organization(
    org_id: int,
    data: OrgUpdate,
    db: AsyncSession = Depends(get_db),
):
    org = (await db.execute(
        select(Organization).where(Organization.id == org_id)
    )).scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    old_name = org.name
    if data.name is not None:
        new_name = data.name.strip()
        if new_name and new_name != old_name:
            # Rename org and update all ContactPosition references
            org.name = new_name
            positions = (await db.execute(
                select(ContactPosition).where(ContactPosition.organization == old_name)
            )).scalars().all()
            for p in positions:
                p.organization = new_name
    if data.context is not None:
        org.context = data.context.strip() or None
    await db.commit()
    await db.refresh(org)
    return await _org_detail(db, org)


@router.delete("/{org_id}")
async def delete_organization(org_id: int, db: AsyncSession = Depends(get_db)):
    org = (await db.execute(
        select(Organization).where(Organization.id == org_id)
    )).scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    # Remove positions referencing this org
    positions = (await db.execute(
        select(ContactPosition).where(ContactPosition.organization == org.name)
    )).scalars().all()
    for p in positions:
        await db.delete(p)
    await db.delete(org)
    await db.commit()
    return {"success": True}


@router.post("/auto-discover")
async def auto_discover_organizations(db: AsyncSession = Depends(get_db)):
    """Create Organization entries for all unique org names in ContactPosition that don't have one yet."""
    result = await db.execute(
        select(ContactPosition.organization).distinct()
    )
    org_names = [row[0] for row in result.all() if row[0]]

    created = 0
    for name in org_names:
        existing = (await db.execute(
            select(Organization).where(Organization.name == name)
        )).scalar_one_or_none()
        if not existing:
            db.add(Organization(name=name))
            created += 1
    await db.commit()
    return {"created": created, "total": len(org_names)}


@router.post("/{org_id}/generate-context")
async def generate_org_context(org_id: int, db: AsyncSession = Depends(get_db)):
    """Use the configured LLM to auto-generate a context summary for this organization."""
    org = (await db.execute(
        select(Organization).where(Organization.id == org_id)
    )).scalar_one_or_none()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Check LLM config
    cfg = load_registry_config()
    if not cfg.orchestrator_name:
        raise HTTPException(status_code=503, detail="No LLM configured. Set up an AI model in Settings.")
    inst = next((i for i in cfg.instances if i.name == cfg.orchestrator_name), None)
    if not inst:
        raise HTTPException(status_code=503, detail="Configured LLM instance not found.")

    base_url = inst.base_url.rstrip("/")
    model = inst.model

    # Gather org data
    detail = await _org_detail(db, org)
    members_text = "\n".join(
        f"- {m['name']}{' (@' + m['username'] + ')' if m['username'] else ''}"
        f"{': ' + m['title'] if m['title'] else ''}"
        for m in detail["members"]
    ) or "No members yet."

    related_text = ""
    for rc in detail["related_chats"]:
        related_text += f"\n- {rc['chat_name']} ({rc['chat_type']})"
        if rc.get("last_message") and rc["last_message"].get("text"):
            lm = rc["last_message"]
            sender = lm.get("sender_name") or "Unknown"
            related_text += f": last message from {sender}: {lm['text'][:150]}"
    related_text = related_text.strip() or "No related groups found."

    prompt = (
        f"Organization: {org.name}\n\n"
        f"Members:\n{members_text}\n\n"
        f"Related Telegram groups and latest messages:\n{related_text}\n\n"
        "Write a concise 2-4 sentence context summary for this organization. "
        "Describe what the organization does, who the key people are, and any "
        "relevant recent activity. Be factual and professional."
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a helpful assistant that writes concise organization context summaries based on available data."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 300,
                    "temperature": 0.4,
                    "stream": False,
                },
            )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"LLM error: {resp.text[:200]}")

        data = resp.json()
        generated = data["choices"][0]["message"].get("content", "").strip()
        if not generated:
            raise HTTPException(status_code=502, detail="LLM returned empty response.")

        # Save to org
        org.context = generated
        await db.commit()
        await db.refresh(org)
        return await _org_detail(db, org)

    except httpx.RequestError as e:
        raise HTTPException(status_code=503, detail=f"Could not reach LLM: {e}")
