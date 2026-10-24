"""The user's setup and what Mingly derives for them: connected accounts,
calendar, the needs-reply / catch-up queue and briefings."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.calendar import events_out, MAX_RANGE_DAYS
from backend.api.catchup import contexts_for, item_dict
from backend.api.mcp_tools.common import (
    clamp,
    date_arg,
    int_arg,
    iso,
    mcp_conversation,
    require_person,
    ToolError,
)
from backend.briefing.pipeline import run_dict
from backend.connectors.manager import manager
from backend.db import crud
from backend.user_orgs import resolve_user_orgs


async def tool_list_accounts(db: AsyncSession, args: dict, ctx: dict) -> dict:
    await manager.load_accounts()
    rows = {r.id: r for r in await crud.get_accounts(db)}
    self_identities = await crud.get_identities(db, [r.self_identity_id for r in rows.values() if r.self_identity_id])
    accounts = []
    for c in manager.all():
        if not c.is_configured():
            continue
        row = rows.get(c.account_id) if c.account_id is not None else None
        me = self_identities.get(row.self_identity_id) if row is not None and row.self_identity_id else None
        accounts.append({
            "account_id": c.account_id,
            "channel": c.channel,
            "display_name": c.display_name,
            "label": row.label if row is not None else None,
            "self_person_id": me.person_id if me is not None else None,
            "status": c.status().to_dict(),
            "capabilities": sorted(cap.value for cap in c.capabilities),
            "history_note": c.history_note(),
        })
    return {
        "accounts": accounts,
        "user_organizations": await resolve_user_orgs(db),
        "note": (
            "Accounts the user connected; conversations carry account_id and channel. user_organizations "
            "are the user's own employers: people with positions there are colleagues."
        ),
    }


async def tool_list_calendar_events(db: AsyncSession, args: dict, ctx: dict) -> dict:
    start, end = date_arg(args, "start"), date_arg(args, "end")
    if start is None or end is None:
        raise ToolError("start and end are required")
    if end <= start:
        raise ToolError("end must be after start")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise ToolError(f"The range is limited to {MAX_RANGE_DAYS} days")
    person_id = None
    if args.get("person_id") is not None:
        person_id = (await require_person(db, int_arg(args, "person_id"))).id
    # All-day events match by date: every day the range touches
    last_day = end.date() if end.time() == datetime.min.time() else end.date() + timedelta(days=1)
    events = await crud.list_calendar_events(
        db, start, end, day_start=start.date(), day_end=last_day,
        person_id=person_id, limit=clamp(args.get("limit"), 200, maximum=1000),
    )
    return {
        "events": await events_out(db, events),
        "note": "All-day events give start_at / end_at as dates; others as UTC instants. Attendees carry person_id.",
    }


async def tool_get_catch_up(db: AsyncSession, args: dict, ctx: dict) -> dict:
    limit = clamp(args.get("limit"), 25)
    active, handled = await crud.get_needs_reply(db, limit=limit, handled_limit=limit)
    contexts = await contexts_for(db, [r[0].id for r in active + handled])

    def entry(row) -> dict:
        conv, sender_name, text, date, item = row
        return {
            "conversation": mcp_conversation(conv, with_last_message=False),
            "last_message": {"sender_name": sender_name, "text": text, "date": iso(date)},
            "triage": item_dict(item) if item is not None else None,
            "summary": contexts.get(conv.id),
        }

    return {
        "needs_reply": [entry(r) for r in active],
        "recently_handled": [entry(r) for r in handled],
        "note": (
            "needs_reply: tracked conversations and DMs with important people whose latest message is "
            "incoming and unanswered, minus ones the user marked handled or snoozed (triage). summary is "
            "Mingly's generated catch-up summary (degraded = written without the AI model)."
        ),
    }


async def tool_list_briefings(db: AsyncSession, args: dict, ctx: dict) -> dict:
    runs = (await crud.list_briefing_runs(db))[:clamp(args.get("limit"), 10)]
    return {
        "briefings": [
            {"briefing_id": r.id, "generated_at": iso(r.generated_at), "days": r.days,
             "status": getattr(r, "status", "done") or "done"}
            for r in runs
        ],
    }


async def tool_get_briefing(db: AsyncSession, args: dict, ctx: dict) -> dict:
    if args.get("briefing_id") is not None:
        run = await crud.get_briefing_run(db, int_arg(args, "briefing_id"))
        if run is None:
            raise ToolError(f"Briefing {args['briefing_id']} not found")
    else:
        run = await crud.latest_finished_briefing_run(db)
        if run is None:
            return {"briefing": None, "note": "No briefing has been generated yet."}
    out = run_dict(run, await crud.get_briefing_run_entries(db, run.id))
    out["briefing_id"] = out.pop("id")
    return {
        "briefing": out,
        "note": (
            "Entries summarize conversations over the run's days; sources are the {conversation_id, "
            "message_id} each summary rests on. category important_contact entries are about a person "
            "(person_id) across conversations."
        ),
    }
