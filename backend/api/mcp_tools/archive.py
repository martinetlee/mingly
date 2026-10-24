"""History archive tools: status, fetching a conversation's history, review marks."""

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.mcp_tools import common
from backend.api.mcp_tools.common import (
    agent_label,
    ARCHIVE_STOPPED_NOTE,
    COMPLETE,
    conversation_states,
    direct_filter,
    incomplete_note,
    int_arg,
    MAX_FETCH_WAIT_SECONDS,
    require_visible_conversation,
    review_up_to_date,
    TELEGRAM_OFFLINE_NOTE,
    ToolError,
    with_state,
)
from backend.connectors.manager import manager
from backend.db import crud
from backend.db.models import Conversation
from backend.serializers import channel_of
from backend.telegram.archive import ARCHIVED_CHANNELS


async def tool_get_archive_status(db: AsyncSession, args: dict, ctx: dict) -> dict:
    status = await common.get_archive().refresh_status()  # get_status() counts are cached, zero before the first run
    connected = await common.telegram_connected()
    not_bot = func.coalesce(Conversation.is_bot, False) == False  # noqa: E712
    row = (await db.execute(
        with_state(select(
            func.count(),
            func.sum(case((and_(not_bot, COMPLETE), 1), else_=0)),
            func.sum(case((and_(not_bot, review_up_to_date()), 1), else_=0)),
            func.sum(case((not_bot, 0), else_=1)),
        )).where(direct_filter())
    )).one()
    total, complete, up_to_date, bots = (int(v or 0) for v in row)
    people = total - bots
    return {
        "telegram_connected": connected,
        "archive": status,
        "dms": {
            "total_people": people,
            "bots_excluded": bots,
            "history_complete": complete,
            "history_incomplete": people - complete,
            "reviewed_up_to_date": up_to_date,
            "unreviewed": people - up_to_date,
        },
        "note": ("" if connected else TELEGRAM_OFFLINE_NOTE + " ") + (
            "'archive' is the Telegram DM archive; 'dms' covers DMs on every channel. Only Telegram history "
            "can be fetched, so only Telegram DMs can be incomplete; other channels' DMs hold everything "
            "Mingly can get, which may not reach back to the start (each conversation's history_note says "
            "how far: Signal from when Mingly was linked, SMS and Gmail a backfill window). "
            "A DM counts as reviewed only when its history is complete and it was "
            "marked reviewed through its newest stored message. Use list_dms(filter='incomplete') "
            "and fetch_history to close gaps, list_dms(filter='unreviewed') for the work queue."
        ),
    }


async def tool_fetch_history(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await require_visible_conversation(db, args.get("conversation_id"))
    conversation_id = conv.id
    if channel_of(conv) not in ARCHIVED_CHANNELS:
        # Nothing to fetch: what's stored is all Mingly can get.
        st = (await conversation_states(db, [conversation_id])).get(conversation_id, {})
        history_note = manager.history_note_for(conv)
        return {
            "conversation_id": conversation_id,
            "history_complete": True,
            "coverage": st.get("coverage") or {},
            "history_note": history_note,
            "note": (
                f"{channel_of(conv)} history isn't fetched on demand: what Mingly stores is all it can get"
                + (f" ({history_note})" if history_note else "")
                + ". Read it with get_messages(order='asc')."
            ),
        }
    try:
        wait = float(args.get("wait_seconds", 20))
    except (TypeError, ValueError):
        raise ToolError("wait_seconds must be a number")
    wait = max(0.0, min(wait, float(MAX_FETCH_WAIT_SECONDS)))
    # End the read transaction: holding a SQLite snapshot across the wait
    # would hide the rows the archive writes meanwhile.
    await db.rollback()
    archive = common.get_archive()
    connected = await common.telegram_connected()
    state = archive.get_status().get("state")
    if not connected or state == "stopped":
        # Nothing can arrive while offline: answer now rather than make the
        # agent wait out (and retry) a timeout.
        st = (await conversation_states(db, [conversation_id])).get(conversation_id, {})
        coverage = st.get("coverage") or {}
        return {
            "conversation_id": conversation_id,
            "history_complete": bool(coverage.get("history_complete")),
            "telegram_connected": connected,
            "coverage": coverage,
            "archive_state": state,
            "note": TELEGRAM_OFFLINE_NOTE if not connected else ARCHIVE_STOPPED_NOTE,
        }
    try:
        await archive.request_conversation(conversation_id, priority=True)
        if wait > 0:
            # Returns the current coverage row at the timeout, complete or not
            await archive.wait_for_conversation(conversation_id, wait)
    except Exception as exc:  # Telegram not connected, vault locked, …
        raise ToolError(f"Could not fetch history for conversation {conversation_id}: {exc}")
    state = (await conversation_states(db, [conversation_id])).get(conversation_id, {})
    coverage = state.get("coverage") or {}
    complete = bool(coverage.get("history_complete"))
    if complete:
        note = "History is complete; read it with get_messages(order='asc')."
    elif coverage.get("archive_error"):
        note = f"The walk stopped before completing: {coverage['archive_error']}. Call fetch_history again to retry."
    else:
        note = "Still archiving in the background; call fetch_history again (or poll get_archive_status)."
    return {
        "conversation_id": conversation_id,
        "history_complete": complete,
        "telegram_connected": True,
        "coverage": coverage,
        "archive_state": archive.get_status().get("state"),
        "note": note,
    }


async def tool_mark_reviewed(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await require_visible_conversation(db, args.get("conversation_id"))
    through = int_arg(args, "through_message_id") if args.get("through_message_id") is not None else None
    state = (await conversation_states(db, [conv.id])).get(conv.id, {})
    if not (state.get("coverage") or {}).get("history_complete"):
        raise ToolError(incomplete_note(conv.id) + " A review can only be recorded on a complete history.")
    try:
        await crud.mark_conversation_reviewed(db, conv.id, through, agent_label(args, ctx))
    except ValueError as exc:
        raise ToolError(str(exc))
    state = (await conversation_states(db, [conv.id])).get(conv.id, {})
    review = state.get("review") or {}
    newest = (state.get("coverage") or {}).get("newest_message_id")
    return {
        "conversation_id": conv.id,
        "review": review,
        "note": None if review.get("up_to_date") else (
            f"Not up to date: messages newer than {review.get('reviewed_through_message_id')} are stored "
            f"(newest {newest}). Read them with get_messages(order='asc', "
            f"after_message_id={review.get('reviewed_through_message_id')})."
        ),
    }
