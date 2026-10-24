"""People and organization tools."""

from datetime import datetime

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.calendar import timeline_meetings
from backend.api.mcp_tools.common import (
    clamp,
    conversation_states,
    decode_cursor,
    encode_cursor,
    int_arg,
    iso,
    mcp_conversation,
    mcp_messages,
    mcp_person,
    person_summaries,
    require_person,
    ToolError,
    visible,
)
from backend.api.organizations import list_organizations as _list_organizations, org_detail
from backend.api.people import people_payload
from backend.api.person_notes import note_dict
from backend.db import crud
from backend.db.models import Conversation, Message, MessageParticipant, Organization
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
    return {"people": [mcp_person(p) for p in await people_payload(db, page["people"])], "total": page["total"]}


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
    group_rows, email_rows = [], []
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
            .where(
                Conversation.is_deactivated == False,  # noqa: E712
                Conversation.kind.in_(("group", "channel")),
            )
            .order_by(desc(in_groups.c.last), Conversation.id)
        )).all()
        # Email threads they're on (sender or to/cc/bcc) beyond their one-to-one DMs
        on_thread = (
            select(Message.conversation_id, func.count(func.distinct(Message.id)).label("n"),
                   func.max(Message.date).label("last"))
            .outerjoin(MessageParticipant, MessageParticipant.message_id == Message.id)
            .where((Message.sender_identity_id.in_(identity_ids)) | (MessageParticipant.identity_id.in_(identity_ids)))
            .group_by(Message.conversation_id)
            .subquery()
        )
        email_rows = (await db.execute(
            select(Conversation, on_thread.c.n, on_thread.c.last)
            .join(on_thread, on_thread.c.conversation_id == Conversation.id)
            .where(
                Conversation.is_deactivated == False,  # noqa: E712
                Conversation.kind == "email_thread",
                Conversation.id.notin_(dm_ids or [0]),
            )
            .order_by(desc(on_thread.c.last), Conversation.id)
        )).all()

    out = mcp_person(payload)
    out.update({
        "organizations": orgs,
        "person_notes": [note_dict(n) for n in notes],
        "dms": dms,
        "groups": [
            {"conversation_id": c.id, "title": c.title, "kind": c.kind, "channel": channel_of(c),
             "messages_from_person": n, "last_message_at": iso(last)}
            for c, n, last in group_rows[:_GROUPS_LIMIT]
        ],
        "groups_total": len(group_rows),
        "email_threads": [
            {"conversation_id": c.id, "title": c.title, "channel": channel_of(c),
             "messages_with_person": n, "last_message_at": iso(last)}
            for c, n, last in email_rows[:_GROUPS_LIMIT]
        ],
        "email_threads_total": len(email_rows),
    })
    return out


async def tool_list_organizations(db: AsyncSession, args: dict, ctx: dict) -> dict:
    orgs = await _list_organizations(search=str(args.get("search") or ""), db=db)
    return {"organizations": orgs}


async def tool_get_organization(db: AsyncSession, args: dict, ctx: dict) -> dict:
    org = await db.get(Organization, int_arg(args, "organization_id"))
    if org is None:
        raise ToolError(f"Organization {args.get('organization_id')} not found")
    out = await org_detail(db, org)
    out["organization_id"] = out.pop("id")
    return out


async def tool_get_person_timeline(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person = await require_person(db, int_arg(args, "person_id"))
    limit = clamp(args.get("limit"), 50)
    scope = {"p": person.id}
    key = decode_cursor(args.get("cursor"), "timeline", scope)
    before, before_id = (key[0], key[1]) if key else (None, None)
    rows, has_more = await crud.person_timeline(db, person.id, limit=limit, before=before, before_id=before_id)
    convs = await crud.get_conversations(db, {r[0].conversation_id for r in rows})
    items = []
    for row, msg in zip(rows, await mcp_messages(db, rows)):
        conv = convs.get(row[0].conversation_id)
        items.append((row[0].date, {
            "kind": "message",
            "conversation": {"conversation_id": conv.id, "title": conv.title, "kind": conv.kind,
                             "channel": channel_of(conv)} if conv is not None else None,
            "message": msg,
        }))
    oldest = rows[-1][0] if has_more and rows else None
    items += await timeline_meetings(db, person.id, before=before, oldest=oldest.date if oldest else None)
    items.sort(key=lambda it: it[0] or datetime.min, reverse=True)
    return {
        "items": [it for _, it in items],
        "next_cursor": encode_cursor("timeline", [oldest.date, oldest.id], scope) if oldest is not None else None,
        "note": (
            "Newest first, every channel: their one-to-one conversations (both directions), what they sent "
            "in groups, emails they're on, and meetings (kind 'meeting') they attended."
        ),
    }


async def tool_list_merge_suggestions(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person_id = None
    if args.get("person_id") is not None:
        person_id = (await require_person(db, int_arg(args, "person_id"))).id
    suggestions = await crud.list_merge_suggestions(db, person_id=person_id)
    people = await person_summaries(db, list({p for s in suggestions for p in (s.person_a_id, s.person_b_id)}))
    return {
        "suggestions": [
            {"suggestion_id": s.id, "reason": s.reason, "score": s.score, "created_at": iso(s.created_at),
             "person_a": people[s.person_a_id], "person_b": people[s.person_b_id]}
            for s in suggestions if s.person_a_id in people and s.person_b_id in people
        ],
        "note": (
            "Pairs of people Mingly thinks may be the same human (never merged automatically); the user "
            "accepts or dismisses them in Mingly. suggest_merge adds one (write tools)."
        ),
    }
