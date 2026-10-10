"""Write tools (enabled in Settings › MCP): notes, person fields, positions, tags."""

from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.mcp_tools.common import (
    agent_label,
    int_arg,
    require_person,
    require_visible_conversation,
    tag_dict,
    ToolError,
)
from backend.api.people import broadcast_person_changed, people_payload, position_dict
from backend.api.person_notes import broadcast_notes_changed, note_dict
from backend.db import crud


async def tool_add_person_note(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person = await require_person(db, int_arg(args, "person_id"))
    try:
        note = await crud.create_person_note(
            db, person.id,
            kind=args.get("kind"), title=args.get("title") or "", body=args.get("body") or "",
            citations=args.get("citations"), author="agent", agent_label=agent_label(args, ctx),
        )
    except ValueError as exc:
        raise ToolError(str(exc))
    await broadcast_notes_changed(person.id)
    return note_dict(note)


async def tool_update_person_note(db: AsyncSession, args: dict, ctx: dict) -> dict:
    note = await crud.get_person_note(db, int_arg(args, "note_id"))
    if note is None:
        raise ToolError("Note not found")
    if note.author != "agent":
        raise ToolError("Only agent-written notes can be updated; add a new note instead")
    try:
        note = await crud.update_person_note(
            db, note,
            kind=args.get("kind"), title=args.get("title"), body=args.get("body"),
            citations=args.get("citations"),
        )
    except ValueError as exc:
        raise ToolError(str(exc))
    await broadcast_notes_changed(note.person_id)
    return note_dict(note)


async def tool_set_person_fields(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person_id = int_arg(args, "person_id")
    fields = ("is_important", "relationship_status", "twitter_handle", "linkedin_url")
    if all(args.get(f) is None for f in fields):
        raise ToolError(f"Give at least one of: {', '.join(fields)}")
    person = await require_person(db, person_id)
    status = args.get("relationship_status")
    if status is not None and status != "auto" and status not in crud.VALID_STATUSES:
        raise ToolError(f"relationship_status must be 'auto' or one of: {', '.join(sorted(crud.VALID_STATUSES))}")
    try:
        crud.apply_person_fields(
            person,
            twitter_handle=args.get("twitter_handle"),
            linkedin_url=args.get("linkedin_url"),
            is_important=args.get("is_important"),
        )
        if status is not None and status != "auto":
            crud.set_manual_relationship_status(person, status)
    except ValueError as exc:
        await db.rollback()
        raise ToolError(str(exc))
    if status == "auto":
        person.relationship_status_manual = False
    await db.commit()
    if status == "auto":
        person = await crud.refresh_person_status(db, person.id) or person
    await db.refresh(person)
    await broadcast_person_changed(person.id)
    return (await people_payload(db, [person]))[0]


async def tool_add_position(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person = await require_person(db, int_arg(args, "person_id"))
    try:
        pos = await crud.add_person_position(
            db, person.id,
            organization=args.get("organization") or "",
            title=args.get("title"),
            is_active=args.get("is_active", True) is not False,
        )
    except (LookupError, ValueError) as exc:
        raise ToolError(str(exc))
    await broadcast_person_changed(person.id)
    return position_dict(pos)


async def tool_tag_conversation(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await require_visible_conversation(db, args.get("conversation_id"))
    tag, err = await crud.find_or_create_tag(db, str(args.get("tag_name") or ""))
    if tag is None:
        raise ToolError(err or "Could not create tag")
    try:
        await crud.add_tag_to_conversation(db, conv.id, tag.id)
    except crud.StageTagLocked:
        raise ToolError("Board column tags can't be assigned directly")
    return {"conversation_id": conv.id, "tag": tag_dict(tag)}
