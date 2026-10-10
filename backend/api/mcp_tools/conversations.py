"""Conversation and message tools: DMs, conversations, messages, search, tags."""

from typing import Any, List, Tuple

from sqlalchemy import func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.mcp_tools.common import (
    after,
    clamp,
    COMPLETE,
    conversation_states,
    decode_cursor,
    direct_filter,
    encode_cursor,
    EPOCH,
    incomplete_note,
    mcp_conversation,
    mcp_msg,
    order_by,
    person_summaries,
    require_visible_conversation,
    review_up_to_date,
    search_hit,
    tag_dict,
    ToolError,
    visible,
    with_state,
)
from backend.db import crud
from backend.db.models import Conversation, ConversationTag, Message
from backend.serializers import channel_of


DM_FILTERS = ("all", "unreviewed", "incomplete")


async def tool_list_dms(db: AsyncSession, args: dict, ctx: dict) -> dict:
    filt = args.get("filter") or "all"
    include_archived = args.get("include_archived", True) is not False
    include_bots = args.get("include_bots", False) is True
    channel = args.get("channel") or None
    limit = clamp(args.get("limit"), 50)
    scope = {"f": filt, "a": include_archived, "b": include_bots, "ch": channel}
    key = decode_cursor(args.get("cursor"), "dms", scope)

    q = with_state(select(Conversation)).where(direct_filter(), crud.channel_filter(channel))
    if not include_archived:
        q = q.where(Conversation.is_archived == False)  # noqa: E712
    if not include_bots:
        q = q.where(func.coalesce(Conversation.is_bot, False) == False)  # noqa: E712
    if filt == "unreviewed":
        q = q.where(not_(review_up_to_date()))
    elif filt == "incomplete":
        q = q.where(not_(COMPLETE))
    if key is not None:
        q = q.where(Conversation.id > key[0])
    convs = list((await db.execute(q.order_by(Conversation.id).limit(limit + 1))).scalars().all())
    more = len(convs) > limit
    convs = convs[:limit]

    states = await conversation_states(db, [c.id for c in convs])
    people = await person_summaries(
        db, [c.peer_identity.person_id for c in convs if c.peer_identity is not None]
    )
    dms = []
    for c in convs:
        d = mcp_conversation(c, with_last_message=False)
        d["person"] = people.get(c.peer_identity.person_id) if c.peer_identity is not None else None
        d.update(states.get(c.id, {}))
        dms.append(d)
    return {
        "dms": dms,
        "next_cursor": encode_cursor("dms", [convs[-1].id], scope) if more else None,
        "filter": filt,
        "note": (
            "Ordered by when Mingly first saw each DM, so paging never skips one; new DMs "
            "appear at the end. Notes-to-self is not listed; bots only with include_bots=true."
        ),
    }


CONVERSATION_SORTS = ("recent", "unread", "alpha", "stable")


KINDS = ("any", "direct", "group", "channel", "email_thread")


def _sort_cols(sort_by: str) -> List[Tuple[Any, bool]]:
    pinned = (func.coalesce(Conversation.is_pinned, False), True)
    activity = (func.coalesce(Conversation.last_activity_at, EPOCH), True)
    if sort_by == "stable":
        return [(Conversation.id, False)]
    if sort_by == "alpha":
        return [pinned, (func.coalesce(Conversation.title, ""), False), (Conversation.id, False)]
    if sort_by == "unread":
        return [pinned, (func.coalesce(Conversation.unread_count, 0), True), activity, (Conversation.id, True)]
    return [pinned, activity, (Conversation.id, True)]


def _sort_key(c: Conversation, sort_by: str) -> list:
    pinned = 1 if c.is_pinned else 0
    activity = c.last_activity_at or EPOCH
    if sort_by == "stable":
        return [c.id]
    if sort_by == "alpha":
        return [pinned, c.title or "", c.id]
    if sort_by == "unread":
        return [pinned, c.unread_count or 0, activity, c.id]
    return [pinned, activity, c.id]


async def tool_list_conversations(db: AsyncSession, args: dict, ctx: dict) -> dict:
    sort_by = args.get("sort_by") or "recent"
    if sort_by not in CONVERSATION_SORTS:
        raise ToolError(f"sort_by must be one of: {', '.join(CONVERSATION_SORTS)}")
    kind = args.get("kind") or "any"
    channel = args.get("channel") or None
    tag_id = args.get("tag_id")
    include_archived = args.get("include_archived", True) is not False
    limit = clamp(args.get("limit"), 50)
    scope = {"s": sort_by, "k": kind, "ch": channel, "g": tag_id, "a": include_archived}
    key = decode_cursor(args.get("cursor"), "conversations", scope)

    cols = _sort_cols(sort_by)
    q = select(Conversation).where(
        Conversation.is_deactivated == False, crud.channel_filter(channel)  # noqa: E712
    )
    if kind != "any":
        q = q.where(Conversation.kind == kind)
    if not include_archived:
        q = q.where(Conversation.is_archived == False)  # noqa: E712
    if tag_id is not None:
        q = q.where(Conversation.id.in_(
            select(ConversationTag.conversation_id).where(ConversationTag.tag_id == tag_id)
        ))
    if key is not None:
        q = q.where(after(cols, key))
    convs = list((await db.execute(q.order_by(*order_by(cols)).limit(limit + 1))).scalars().all())
    more = len(convs) > limit
    convs = convs[:limit]
    states = await conversation_states(db, [c.id for c in convs])
    out = []
    for c in convs:
        d = mcp_conversation(c)
        d["coverage"] = states.get(c.id, {}).get("coverage")
        out.append(d)
    return {
        "conversations": out,
        "next_cursor": encode_cursor("conversations", _sort_key(convs[-1], sort_by), scope) if more else None,
        "note": None if sort_by == "stable" else (
            "Activity-based order can move conversations between pages while you page; "
            "use sort_by='stable' to visit every conversation exactly once."
        ),
    }


async def tool_search_conversations(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    convs, total = await crud.search_conversations_by_title(
        db, query, limit=clamp(args.get("limit"), 20), channel=args.get("channel") or None,
    )
    return {"conversations": [mcp_conversation(c) for c in convs if visible(c)], "total": total}


async def tool_get_conversation(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await require_visible_conversation(db, args.get("conversation_id"))
    tags = await crud.get_tags_for_conversation(db, conv.id)
    d = mcp_conversation(conv)
    d["tags"] = [tag_dict(t) for t in tags]
    d.update((await conversation_states(db, [conv.id])).get(conv.id, {}))
    return d


async def tool_get_messages(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await require_visible_conversation(db, args.get("conversation_id"))
    order = args.get("order") or "desc"
    limit = clamp(args.get("limit"), 50)
    scope = {"c": conv.id, "o": order}
    key = decode_cursor(args.get("cursor"), "msgs", scope)
    after_id = args.get("after_message_id")
    if after_id is not None:
        if key is not None:
            raise ToolError("Pass either cursor or after_message_id, not both")
        if order != "asc":
            raise ToolError("after_message_id requires order='asc'")
        # Resume after a stored message (e.g. review.reviewed_through_message_id).
        anchor = (await db.execute(
            select(Message.date, Message.seq, Message.id)
            .where(Message.conversation_id == conv.id, Message.id == after_id)
        )).first()
        if anchor is None:
            raise ToolError(
                f"Message {after_id} is not stored in conversation {conv.id}; use the "
                "review's reviewed_through_message_id or read from the start"
            )
        key = [anchor.date, anchor.seq, anchor.id]

    descending = order == "desc"
    cols = [(Message.date, descending), (Message.seq, descending), (Message.id, descending)]
    q = crud.with_reply_preview().where(Message.conversation_id == conv.id)
    if key is not None:
        q = q.where(after(cols, key))
    rows = list((await db.execute(q.order_by(*order_by(cols)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    state = (await conversation_states(db, [conv.id])).get(conv.id, {})
    coverage = state.get("coverage") or {}
    complete = bool(coverage.get("history_complete"))

    last = rows[-1][0] if rows else None
    result = {
        "conversation": {
            "conversation_id": conv.id, "title": conv.title, "kind": conv.kind, "channel": channel_of(conv),
        },
        "order": order,
        "messages": [mcp_msg(row) for row in rows],
        "next_cursor": encode_cursor("msgs", [last.date, last.seq, last.id], scope) if more and last else None,
        "history_complete": complete,
        "coverage": coverage,
        "review": state.get("review"),
    }
    notes = []
    if not complete:
        notes.append(incomplete_note(conv.id))
    if not more and order == "asc":
        notes.append(
            f"End of stored history (newest message_id {coverage.get('newest_message_id')}). "
            "Record findings, then mark_reviewed through that id."
        )
    if order == "desc":
        notes.append("For a complete read use order='asc': messages arriving while you page are appended at the end.")
    result["note"] = " ".join(notes) or None
    return result


async def tool_search_messages(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    limit = clamp(args.get("limit"), 30)
    channel = args.get("channel") or None
    conversation_id = None
    if args.get("conversation_id") is not None:
        conversation_id = (await require_visible_conversation(db, args.get("conversation_id"))).id
    scope = {"q": query, "c": conversation_id, "ch": channel}
    key = decode_cursor(args.get("cursor"), "search", scope)

    match = [
        or_(crud.contains_ci(Message.text, query), crud.contains_ci(Message.subject, query)),
        Conversation.is_deactivated == False,  # noqa: E712
        crud.channel_filter(channel),
    ]
    if conversation_id is not None:
        match.append(Message.conversation_id == conversation_id)
    joined = select(Message, Conversation).join(Conversation, Message.conversation_id == Conversation.id)
    total = (await db.execute(
        select(func.count()).select_from(Message)
        .join(Conversation, Message.conversation_id == Conversation.id).where(*match)
    )).scalar() or 0
    base = joined.where(*match)
    cols = [(Message.date, True), (Message.id, True)]
    if key is not None:
        base = base.where(after(cols, key))
    rows = list((await db.execute(base.order_by(*order_by(cols)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1][0] if rows else None
    return {
        "messages": [search_hit(m, conv) for m, conv in rows],
        "total": total,
        "next_cursor": encode_cursor("search", [last.date, last.id], scope) if more and last else None,
    }


async def tool_list_tags(db: AsyncSession, args: dict, ctx: dict) -> dict:
    rows = await crud.get_all_tags_with_counts(db)
    return {"tags": [tag_dict(t, count) for t, count in rows]}


async def tool_get_conversations_by_tag(db: AsyncSession, args: dict, ctx: dict) -> dict:
    tag_name = str(args.get("tag_name") or "").strip()
    if not tag_name:
        raise ToolError("tag_name is required")
    channel = args.get("channel") or None
    convs = await crud.get_conversations_by_tag_name(db, tag_name)
    return {"conversations": [
        mcp_conversation(c) for c in convs if visible(c) and (channel is None or channel_of(c) == channel)
    ]}
