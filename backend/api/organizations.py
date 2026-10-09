from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Conversation, Message, Organization, PersonPosition
from backend.llm.base import ChatMessage, LLMError
from backend.llm.resolve import LLMUnavailable, complete_text
from backend.serialization import iso_utc
from backend import user_orgs
from backend.db.results import affected_rows

router = APIRouter()


class OrgCreate(BaseModel):
    name: str


class OrgUpdate(BaseModel):
    name: Optional[str] = None
    context: Optional[str] = None
    # Renaming onto another org's name merges this org into it — only when set
    merge: bool = False


class OrgMerge(BaseModel):
    into_id: int


# --- Helpers ---

async def _get_org(db: AsyncSession, org_id: int) -> Organization:
    org = await db.get(Organization, org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


async def _org_detail(db: AsyncSession, org: Organization) -> dict:
    """Full org: current and former members, related group conversations."""
    positions = list((await db.execute(
        select(PersonPosition).where(PersonPosition.organization_id == org.id).order_by(PersonPosition.id)
    )).scalars().all())
    people = await crud.get_people(db, {p.person_id for p in positions})
    identities = await crud.identities_for_people(db, people)
    members = []
    former_members = []
    for pos in positions:
        person = people.get(pos.person_id)
        username = next((i.username for i in identities.get(pos.person_id, []) if i.username), None)
        entry = {
            "person_id": pos.person_id,
            "position_id": pos.id,
            "name": (person.name if person is not None else None) or username or f"Person {pos.person_id}",
            "username": username,
            "title": pos.title,
            "ended_at": iso_utc(pos.ended_at),
        }
        (members if pos.is_active else former_members).append(entry)
    for group in (members, former_members):
        group.sort(key=lambda m: (m["name"].lower(), m["position_id"]))

    # Group conversations whose title contains the org name (heuristic)
    related = list((await db.execute(
        select(Conversation)
        .where(
            crud.contains_ci(Conversation.title, org.name),
            Conversation.kind.in_(["group", "channel"]),
            Conversation.is_deactivated == False,  # noqa: E712
        )
        .order_by(desc(Conversation.last_activity_at), Conversation.title)
        .limit(10)
    )).scalars().all())

    # Latest message per related conversation, in one query
    latest: dict[int, Message] = {}
    if related:
        ranked = (
            select(
                Message.id,
                func.row_number().over(
                    partition_by=Message.conversation_id, order_by=(desc(Message.date), desc(Message.id)),
                ).label("rn"),
            )
            .where(Message.conversation_id.in_([c.id for c in related]))
            .subquery()
        )
        for msg in (await db.execute(
            select(Message).join(ranked, ranked.c.id == Message.id).where(ranked.c.rn == 1)
        )).scalars().all():
            latest[msg.conversation_id] = msg

    return {
        "id": org.id,
        "name": org.name,
        "context": org.context,
        "members": members,
        "former_members": former_members,
        "related_conversations": [
            {
                "conversation_id": conv.id,
                "conversation_title": conv.title,
                "conversation_kind": conv.kind,
                "last_message": {
                    "text": (msg.text or "")[:200],
                    "sender_name": msg.sender_name,
                    "date": iso_utc(msg.date),
                } if msg is not None else None,
            }
            for conv, msg in ((c, latest.get(c.id)) for c in related)
        ],
        "created_at": iso_utc(org.created_at),
    }


def _name_or_400(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Organization name cannot be empty")
    return name


def _exists_409(existing: Organization) -> HTTPException:
    """Structured 409 so the UI can offer 'merge into <existing>'."""
    return HTTPException(status_code=409, detail={
        "code": "organization_exists",
        "message": f'An organization named "{existing.name}" already exists',
        "existing_id": existing.id,
        "existing_name": existing.name,
    })


# --- Endpoints ---

@router.get("")
async def list_organizations(
    search: str = "",
    db: AsyncSession = Depends(get_db),
):
    return [
        {
            "id": org.id,
            "name": org.name,
            "context": org.context,
            "member_count": active,
            "former_member_count": former,
        }
        for org, active, former in await crud.list_organizations_with_counts(db, search)
    ]


@router.post("")
async def create_organization(data: OrgCreate, db: AsyncSession = Depends(get_db)):
    name = _name_or_400(data.name)
    existing = await crud.find_organization_by_name(db, name)
    if existing:
        raise _exists_409(existing)
    try:
        org, _created = await crud.get_or_create_organization(db, name)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await crud.find_organization_by_name(db, name)
        if existing:
            raise _exists_409(existing)
        raise HTTPException(status_code=409, detail="Organization already exists")
    return await _org_detail(db, org)


@router.post("/auto-discover")
async def auto_discover_organizations(db: AsyncSession = Depends(get_db)):
    """Create organizations for detached position names that have none yet
    (case/whitespace-insensitive) and attach those positions."""
    names = await crud.detached_position_names(db)
    created = 0
    for name in names:
        _org, was_created = await crud.get_or_create_organization(db, name)
        if not was_created:
            await crud.attach_detached_positions(db, _org)
        created += int(was_created)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Organizations changed concurrently; try again")
    return {"created": created, "total": len(names)}


@router.get("/{org_id}")
async def get_organization(org_id: int, db: AsyncSession = Depends(get_db)):
    return await _org_detail(db, await _get_org(db, org_id))


@router.put("/{org_id}")
async def update_organization(
    org_id: int,
    data: OrgUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Rename and/or set context. Renaming onto another org's name returns a
    structured 409 unless `merge` is true, in which case this org is merged
    into that one and the merged org (different id!) is returned."""
    org = await _get_org(db, org_id)
    result_org = org
    old_name = org.name
    after_commit = None  # user-orgs prefs follow the DB change once committed
    if data.name is not None:
        new_name = _name_or_400(data.name)
        clash = (await db.execute(
            select(Organization).where(
                crud.name_key(Organization.name) == crud.name_key(new_name), Organization.id != org.id,
            ).limit(1)
        )).scalar_one_or_none()
        if clash is not None:
            if not data.merge:
                raise _exists_409(clash)
            await crud.merge_organizations(db, org, clash)
            result_org = clash
            after_commit = lambda: user_orgs.on_org_merged(org_id, old_name, clash.id, clash.name)  # noqa: E731
        elif new_name != org.name:
            await crud.rename_organization(db, org, new_name)
            after_commit = lambda: user_orgs.on_org_renamed(org_id, old_name, new_name)  # noqa: E731
    if data.context is not None:
        result_org.context = data.context.strip() or None
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Organization changed concurrently; reload and retry")
    if after_commit is not None:
        after_commit()
    return await _org_detail(db, result_org)


@router.post("/{org_id}/merge")
async def merge_organization(org_id: int, data: OrgMerge, db: AsyncSession = Depends(get_db)):
    """Merge org `org_id` into `into_id`; returns the merged org plus counts."""
    if data.into_id == org_id:
        raise HTTPException(status_code=400, detail="Cannot merge an organization into itself")
    source = await _get_org(db, org_id)
    target = await _get_org(db, data.into_id)
    source_name = source.name
    counts = await crud.merge_organizations(db, source, target)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="Organization changed concurrently; reload and retry")
    user_orgs.on_org_merged(org_id, source_name, target.id, target.name)
    return {**await _org_detail(db, target), **counts}


@router.delete("/{org_id}")
async def delete_organization(
    org_id: int,
    mode: str = Query("detach", pattern="^(detach|delete)$"),
    db: AsyncSession = Depends(get_db),
):
    """mode=detach (default): members keep the position as history, no longer
    linked to an org. mode=delete: members' positions here are deleted."""
    org = await _get_org(db, org_id)
    name = org.name
    counts = await crud.delete_organization(db, org, mode)
    await db.commit()
    user_orgs.on_org_deleted(org_id, name)
    return {"success": True, "mode": mode, **counts}


@router.post("/{org_id}/generate-context")
async def generate_org_context(org_id: int, db: AsyncSession = Depends(get_db)):
    """Use the configured LLM to auto-generate a context summary for this organization.

    Compare-and-set: the result is saved only if the context is unchanged
    since generation started, so a manual edit made meanwhile is never lost
    (409 in that case)."""
    org = await _get_org(db, org_id)
    context_before = org.context
    detail = await _org_detail(db, org)
    # Release the read transaction; the LLM call can take a minute
    await db.commit()

    members_text = "\n".join(
        f"- {m['name']}{' (@' + m['username'] + ')' if m['username'] else ''}"
        f"{': ' + m['title'] if m['title'] else ''}"
        for m in detail["members"]
    ) or "No members yet."

    related_text = ""
    for rc in detail["related_conversations"]:
        related_text += f"\n- {rc['conversation_title']} ({rc['conversation_kind']})"
        if rc.get("last_message") and rc["last_message"].get("text"):
            lm = rc["last_message"]
            sender = lm.get("sender_name") or "Unknown"
            related_text += f": last message from {sender}: {lm['text'][:150]}"
    related_text = related_text.strip() or "No related groups found."

    prompt = (
        f"Organization: {detail['name']}\n\n"
        f"Members:\n{members_text}\n\n"
        f"Related groups and latest messages:\n{related_text}\n\n"
        "Write a concise 2-4 sentence context summary for this organization. "
        "Describe what the organization does, who the key people are, and any "
        "relevant recent activity. Be factual and professional."
    )

    try:
        generated = await complete_text(
            [
                ChatMessage(role="system", content="You are a helpful assistant that writes concise organization context summaries based on available data."),
                ChatMessage(role="user", content=prompt),
            ],
            max_tokens=300,
            temperature=0.4,
        )
    except LLMUnavailable:
        raise HTTPException(status_code=503, detail="No LLM configured. Set up an AI model in Settings.")
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"LLM error: {str(e)[:200]}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=503, detail=f"Could not reach LLM: {e}")
    if not generated:
        raise HTTPException(status_code=502, detail="LLM returned an empty response.")

    unchanged = (
        Organization.context.is_(None) if context_before is None else Organization.context == context_before
    )
    res = await db.execute(
        update(Organization).where(Organization.id == org_id, unchanged).values(context=generated)
    )
    await db.commit()
    if not affected_rows(res):
        if await db.get(Organization, org_id) is None:
            raise HTTPException(status_code=404, detail="Organization was deleted while generating")
        raise HTTPException(
            status_code=409,
            detail="The context was edited while generating; the generated text was discarded.",
        )
    org = await _get_org(db, org_id)
    await db.refresh(org)
    return await _org_detail(db, org)
