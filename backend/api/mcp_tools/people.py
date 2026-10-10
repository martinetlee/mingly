"""People and organization tools."""

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.mcp_tools.common import (
    clamp,
    conversation_states,
    int_arg,
    iso,
    mcp_conversation,
    require_person,
    visible,
)
from backend.api.organizations import list_organizations as _list_organizations
from backend.api.people import people_payload
from backend.api.person_notes import note_dict
from backend.db import crud
from backend.db.models import Conversation, Message, Organization
from backend.serializers import channel_of


async def tool_search_people(db: AsyncSession, args: dict, ctx: dict) -> dict:
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    page = await crud.list_people(
        db,
        search=str(args.get("query") or "").strip(),
        contacts_only=args.get("contacts_only") is True,
        important=args.get("important_only") is True,
        limit=clamp(args.get("limit"), 50),
        offset=offset,
    )
    return {"people": await people_payload(db, page["people"]), "total": page["total"]}


_GROUPS_LIMIT = 50


async def tool_get_person(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person = await require_person(db, int_arg(args, "person_id"))
    payload = (await people_payload(db, [person]))[0]
    notes = await crud.list_person_notes(db, person.id)
    positions = await crud.get_person_positions(db, person.id)

    org_ids = [p.organization_id for p in positions if p.organization_id is not None]
    orgs = []
    if org_ids:
        orgs = [
            {"id": o.id, "name": o.name, "context": o.context}
            for o in (await db.execute(select(Organization).where(Organization.id.in_(org_ids)))).scalars().all()
        ]

    dm_ids = await crud.direct_conversation_ids_for_person(db, person.id)
    dm_convs = [c for c in (await crud.get_conversations(db, dm_ids)).values() if visible(c)]
    dm_convs.sort(key=lambda c: dm_ids.index(c.id))
    states = await conversation_states(db, [c.id for c in dm_convs])
    dms = []
    for c in dm_convs:
        d = mcp_conversation(c)
        d.update(states.get(c.id, {}))
        dms.append(d)

    identity_ids = [i["id"] for i in payload["identities"]]
    group_rows = []
    if identity_ids:
        in_groups = (
            select(Message.conversation_id, func.count().label("n"), func.max(Message.date).label("last"))
            .where(Message.sender_identity_id.in_(identity_ids))
            .group_by(Message.conversation_id)
            .subquery()
        )
        group_rows = (await db.execute(
            select(Conversation, in_groups.c.n, in_groups.c.last)
            .join(in_groups, in_groups.c.conversation_id == Conversation.id)
            .where(Conversation.is_deactivated == False, Conversation.kind != "direct")  # noqa: E712
            .order_by(desc(in_groups.c.last), Conversation.id)
        )).all()

    payload.update({
        "person_id": payload.pop("id"),
        "organizations": orgs,
        "notes": [note_dict(n) for n in notes],
        "dms": dms,
        "groups": [
            {"conversation_id": c.id, "title": c.title, "kind": c.kind, "channel": channel_of(c),
             "messages_from_person": n, "last_message_at": iso(last)}
            for c, n, last in group_rows[:_GROUPS_LIMIT]
        ],
        "groups_total": len(group_rows),
    })
    payload.pop("avatar_url", None)
    for ident in payload["identities"]:
        ident.pop("avatar_url", None)
    return payload


async def tool_list_organizations(db: AsyncSession, args: dict, ctx: dict) -> dict:
    orgs = await _list_organizations(search=str(args.get("search") or ""), db=db)
    return {"organizations": orgs}
