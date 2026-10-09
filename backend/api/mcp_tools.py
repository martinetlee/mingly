"""
Tool registry for the MCP endpoint.

Each tool returns a plain dict that the transport layer (backend/api/mcp.py)
serializes into an MCP text-content result. Every tool is tagged with an
``access`` of "read" or "write"; mcp.py refuses write tools unless the user
enabled them (Settings › MCP, pref ``mcp_write_enabled``).

Ids are Mingly's internal ids (``conversation_id``, ``message_id``,
``person_id``); conversations from every channel are listed, and tools that
list conversations take an optional ``channel`` filter.

Visibility: deactivated conversations (groups migrated to a supergroup) are
excluded everywhere. Hidden, archived and briefing conversations are included
and flagged (``is_hidden`` / ``is_archived`` / ``is_briefing``): those flags
declutter the app's inbox, and an agent asked to cover "everyone I've DMed"
must not silently skip anyone. The scam LLM columns (scam_llm_prompt /
scam_llm_response / scam_trace_json) are never serialized.

Paging: list tools use opaque keyset cursors over a total order, so pages
never shift when rows are added or removed between calls. Pass the returned
``next_cursor`` back unchanged; ``next_cursor: null`` means the end.
"""
import base64
import binascii
import json
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import and_, case, desc, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.models import (
    AgentReview, Conversation, ConversationTag, HistoryCoverage, Message, Organization,
)
from backend.serialization import iso_utc
from backend.serializers import channel_of, conversation_out, message_out
from backend.api.organizations import list_organizations as _list_organizations
from backend.api.people import broadcast_person_changed, people_payload, position_dict
from backend.api.person_notes import broadcast_notes_changed, note_dict

MAX_LIMIT = 100
MAX_FETCH_WAIT_SECONDS = 60
_EPOCH = datetime(1970, 1, 1)


class ToolError(Exception):
    """Tool-level failure reported to the client as isError content."""


def _clamp(value: Any, default: int, maximum: int = MAX_LIMIT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


def _visible(conv) -> bool:
    return conv is not None and not conv.is_deactivated


def _iso(dt) -> Optional[str]:
    return iso_utc(dt)


# ── Cursors ──────────────────────────────────────────────────────────────────

def _jsonable(v):
    return {"dt": v.isoformat()} if isinstance(v, datetime) else v


def _from_jsonable(v):
    if isinstance(v, dict) and set(v) == {"dt"}:
        return datetime.fromisoformat(v["dt"])
    return v


def _encode_cursor(kind: str, key: list, scope: dict) -> str:
    raw = json.dumps({"t": kind, "s": scope, "k": [_jsonable(v) for v in key]}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(raw: Any, kind: str, scope: dict) -> Optional[list]:
    """Key of a cursor issued by the same tool with the same scope (filters,
    order). None when no cursor was given."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ToolError("cursor must be the string returned as next_cursor")
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode())
        key = [_from_jsonable(v) for v in data["k"]]
        ok = data["t"] == kind and data["s"] == scope
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError):
        ok = False
    if not ok:
        raise ToolError("Invalid cursor: pass back next_cursor unchanged, with the same filters/order")
    return key


def _after(cols: List[Tuple[Any, bool]], key: list):
    """Rows strictly after `key` in the lexicographic order of `cols`
    [(expr, descending)]. Every expr must be non-NULL."""
    if len(key) != len(cols):
        raise ToolError("Invalid cursor: pass back next_cursor unchanged, with the same filters/order")
    clauses = []
    for i, (expr, descending) in enumerate(cols):
        prefix = [cols[j][0] == key[j] for j in range(i)]
        clauses.append(and_(*prefix, expr < key[i] if descending else expr > key[i]))
    return or_(*clauses)


def _order_by(cols: List[Tuple[Any, bool]]) -> list:
    return [desc(e) if d else e for e, d in cols]


# ── Coverage / review state ──────────────────────────────────────────────────

def _with_state(q):
    return (
        q.outerjoin(HistoryCoverage, HistoryCoverage.conversation_id == Conversation.id)
        .outerjoin(AgentReview, AgentReview.conversation_id == Conversation.id)
    )


_COMPLETE = func.coalesce(HistoryCoverage.history_complete, False) == True  # noqa: E712


async def _message_ids_at_max_seq(db: AsyncSession, per_conversation_max) -> Dict[int, int]:
    """{conversation_id: message id} of the message at each conversation's
    seq given by the ``(conversation_id, s)`` subquery."""
    sub = per_conversation_max.subquery()
    rows = (await db.execute(
        select(Message.conversation_id, Message.id)
        .join(sub, and_(sub.c.conversation_id == Message.conversation_id, sub.c.s == Message.seq))
        .order_by(Message.id)
    )).all()
    return {cid: mid for cid, mid in rows}


async def _conversation_states(db: AsyncSession, conversation_ids: List[int]) -> Dict[int, dict]:
    """{conversation_id: {"coverage": {...}, "review": {...}}}."""
    if not conversation_ids:
        return {}
    rows = (await db.execute(
        _with_state(select(Conversation.id, HistoryCoverage, AgentReview, crud.review_up_to_date().label("utd")))
        .where(Conversation.id.in_(conversation_ids))
    )).all()
    in_ids = Message.conversation_id.in_(conversation_ids)
    stats = {
        r.conversation_id: r for r in (await db.execute(
            select(
                Message.conversation_id,
                func.count().label("n"),
                func.min(Message.date).label("first"),
                func.max(Message.date).label("last"),
            ).where(in_ids).group_by(Message.conversation_id)
        )).all()
    }
    newest = await _message_ids_at_max_seq(db, (
        select(Message.conversation_id, func.max(Message.seq).label("s"))
        .where(in_ids).group_by(Message.conversation_id)
    ))
    # The newest stored message within the review (survives deletions).
    reviewed_through = await _message_ids_at_max_seq(db, (
        select(Message.conversation_id, func.max(Message.seq).label("s"))
        .join(AgentReview, AgentReview.conversation_id == Message.conversation_id)
        .where(in_ids, Message.seq <= AgentReview.reviewed_through_seq)
        .group_by(Message.conversation_id)
    ))
    out = {}
    for cid, cov, review, utd in rows:
        s = stats.get(cid)
        out[cid] = {
            "coverage": {
                "history_complete": bool(cov and cov.history_complete),
                "message_count": s.n if s else 0,
                "first_message_at": _iso(s.first) if s else None,
                "last_message_at": _iso(s.last) if s else None,
                "newest_message_id": newest.get(cid),
                "archive_error": cov.error if cov else None,
                "last_walk_at": _iso(cov.last_walk_at) if cov else None,
                "last_topup_at": _iso(cov.last_topup_at) if cov else None,
            },
            "review": {
                "reviewed_through_message_id": reviewed_through.get(cid) if review else None,
                "reviewed_at": _iso(review.reviewed_at) if review else None,
                "agent_label": review.agent_label if review else None,
                "up_to_date": bool(utd),
            },
        }
    return out


# ── Serializers ──────────────────────────────────────────────────────────────

# Conversation fields an agent gets (serializers.conversation_out minus
# session-gated media URLs and UI-only state).
_CONVERSATION_KEYS = (
    "id", "account_id", "channel", "kind", "title", "username", "peer", "is_pinned", "is_archived",
    "is_hidden", "is_briefing", "is_tracked", "is_muted", "unread_count", "members_count", "is_bot",
    "is_deleted_account", "last_activity_at",
)


def _mcp_conversation(c: Conversation, with_last_message: bool = True) -> dict:
    full = conversation_out(c)
    d = {"conversation_id" if k == "id" else k: full.get(k) for k in _CONVERSATION_KEYS}
    if c.kind != "direct":
        d.pop("is_bot")
        d.pop("is_deleted_account")
    if with_last_message:
        d["last_message"] = full.get("last_message")
    return d


# Message fields an agent gets (serializers.message_out minus attachment URLs,
# HTML and scam state).
_MESSAGE_KEYS = (
    "id", "conversation_id", "seq", "kind", "sender", "subject", "text", "date", "is_outgoing", "meta",
    "fwd_from_name", "reactions", "reply_to_message_id", "reply_to_preview", "topic_id", "is_pinned",
    "is_edited",
)


def _mcp_msg(row) -> dict:
    full = message_out(row)
    d = {"message_id" if k == "id" else k: full.get(k) for k in _MESSAGE_KEYS}
    d["attachments"] = [
        {"kind": a["kind"], "filename": a["filename"], "mime_type": a["mime_type"], "size": a["size"]}
        for a in full["attachments"]
    ]
    return d


def _search_hit(m: Message, conv: Conversation) -> dict:
    d = _mcp_msg(m)
    d["conversation_title"] = conv.title
    d["channel"] = channel_of(conv)
    return d


def _tag_dict(t, conversation_count: Optional[int] = None) -> dict:
    d = {"id": t.id, "name": t.name, "color": t.color, "category": t.category}
    if conversation_count is not None:
        d["conversation_count"] = conversation_count
    return d


async def _person_summaries(db: AsyncSession, person_ids: List[int]) -> Dict[int, dict]:
    """Compact person shape for list_dms, keyed by person id."""
    people = await crud.get_people(db, person_ids)
    identities = await crud.identities_for_people(db, list(people))
    positions = await crud.positions_for_people(db, list(people))
    notes = await crud.count_person_notes(db, list(people))
    out = {}
    for pid, p in people.items():
        out[pid] = {
            "person_id": pid,
            "name": p.name,
            "identities": [
                {"identity_id": i.id, "channel": i.channel, "username": i.username, "display_name": i.display_name}
                for i in identities.get(pid, [])
            ],
            "is_contact": bool(p.is_contact),
            "is_important": bool(p.is_important),
            "relationship_status": p.relationship_status,
            "twitter_handle": p.twitter_handle,
            "linkedin_url": p.linkedin_url,
            "has_person_notes_field": bool(p.notes),
            "positions": [
                {"organization": x.organization, "organization_id": x.organization_id,
                 "title": x.title, "is_active": x.is_active}
                for x in positions.get(pid, [])
            ],
            "notes_count": notes.get(pid, 0),
        }
    return out


# ── Lookups ──────────────────────────────────────────────────────────────────

def _int_arg(args: dict, key: str, label: Optional[str] = None) -> int:
    try:
        value = args.get(key)
        if isinstance(value, bool):
            raise TypeError
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{label or key} must be an integer")


async def _require_visible_conversation(db: AsyncSession, conversation_id: Any) -> Conversation:
    try:
        if isinstance(conversation_id, bool):
            raise TypeError
        cid = int(conversation_id)
    except (TypeError, ValueError):
        raise ToolError("conversation_id must be an integer conversation ID")
    conv = await crud.get_conversation(db, cid)
    if not _visible(conv):
        raise ToolError(f"Conversation {cid} not found")
    return conv


async def _require_person(db: AsyncSession, person_id: int):
    person = await crud.get_person(db, person_id)
    if person is None:
        raise ToolError(f"Person {person_id} not found")
    return person


def _archive():
    from backend.telegram import archive
    return archive


async def _telegram_connected() -> bool:
    from backend.telegram.runtime import runtime
    return await runtime.client(require=False) is not None


TELEGRAM_OFFLINE_NOTE = (
    "Telegram isn't connected — open Mingly and unlock it; archiving resumes automatically. "
    "Already-stored messages are still readable. Don't retry fetch_history until "
    "get_archive_status shows telegram_connected: true; tell the user instead."
)
ARCHIVE_STOPPED_NOTE = (
    "The archive isn't running (Mingly is still starting up, or it was stopped). Stored messages "
    "are still readable; check get_archive_status later instead of retrying in a loop."
)


def _direct_filter():
    """Direct conversations an agent reviews (never notes-to-self or deactivated ones)."""
    return and_(
        Conversation.kind == "direct",
        Conversation.is_deactivated == False,  # noqa: E712
        func.coalesce(Conversation.is_self, False) == False,  # noqa: E712
    )


# ── Read tools ───────────────────────────────────────────────────────────────

async def _tool_get_archive_status(db: AsyncSession, args: dict, ctx: dict) -> dict:
    status = await _archive().refresh_status()  # get_status() counts are cached, zero before the first run
    connected = await _telegram_connected()
    not_bot = func.coalesce(Conversation.is_bot, False) == False  # noqa: E712
    row = (await db.execute(
        _with_state(select(
            func.count(),
            func.sum(case((and_(not_bot, _COMPLETE), 1), else_=0)),
            func.sum(case((and_(not_bot, crud.review_up_to_date()), 1), else_=0)),
            func.sum(case((not_bot, 0), else_=1)),
        )).where(_direct_filter())
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
            "A DM counts as reviewed only when its history is complete and it was "
            "marked reviewed through its newest stored message. Use list_dms(filter='incomplete') "
            "and fetch_history to close gaps, list_dms(filter='unreviewed') for the work queue."
        ),
    }


_DM_FILTERS = ("all", "unreviewed", "incomplete")


async def _tool_list_dms(db: AsyncSession, args: dict, ctx: dict) -> dict:
    filt = args.get("filter") or "all"
    include_archived = args.get("include_archived", True) is not False
    include_bots = args.get("include_bots", False) is True
    channel = args.get("channel") or None
    limit = _clamp(args.get("limit"), 50)
    scope = {"f": filt, "a": include_archived, "b": include_bots, "ch": channel}
    key = _decode_cursor(args.get("cursor"), "dms", scope)

    q = _with_state(select(Conversation)).where(_direct_filter(), crud.channel_filter(channel))
    if not include_archived:
        q = q.where(Conversation.is_archived == False)  # noqa: E712
    if not include_bots:
        q = q.where(func.coalesce(Conversation.is_bot, False) == False)  # noqa: E712
    if filt == "unreviewed":
        q = q.where(not_(crud.review_up_to_date()))
    elif filt == "incomplete":
        q = q.where(not_(_COMPLETE))
    if key is not None:
        q = q.where(Conversation.id > key[0])
    convs = list((await db.execute(q.order_by(Conversation.id).limit(limit + 1))).scalars().all())
    more = len(convs) > limit
    convs = convs[:limit]

    states = await _conversation_states(db, [c.id for c in convs])
    people = await _person_summaries(
        db, [c.peer_identity.person_id for c in convs if c.peer_identity is not None]
    )
    dms = []
    for c in convs:
        d = _mcp_conversation(c, with_last_message=False)
        d["person"] = people.get(c.peer_identity.person_id) if c.peer_identity is not None else None
        d.update(states.get(c.id, {}))
        dms.append(d)
    return {
        "dms": dms,
        "next_cursor": _encode_cursor("dms", [convs[-1].id], scope) if more else None,
        "filter": filt,
        "note": (
            "Ordered by when Mingly first saw each DM, so paging never skips one; new DMs "
            "appear at the end. Notes-to-self is not listed; bots only with include_bots=true."
        ),
    }


_CONVERSATION_SORTS = ("recent", "unread", "alpha", "stable")
_KINDS = ("any", "direct", "group", "channel", "email_thread")


def _sort_cols(sort_by: str) -> List[Tuple[Any, bool]]:
    pinned = (func.coalesce(Conversation.is_pinned, False), True)
    activity = (func.coalesce(Conversation.last_activity_at, _EPOCH), True)
    if sort_by == "stable":
        return [(Conversation.id, False)]
    if sort_by == "alpha":
        return [pinned, (func.coalesce(Conversation.title, ""), False), (Conversation.id, False)]
    if sort_by == "unread":
        return [pinned, (func.coalesce(Conversation.unread_count, 0), True), activity, (Conversation.id, True)]
    return [pinned, activity, (Conversation.id, True)]


def _sort_key(c: Conversation, sort_by: str) -> list:
    pinned = 1 if c.is_pinned else 0
    activity = c.last_activity_at or _EPOCH
    if sort_by == "stable":
        return [c.id]
    if sort_by == "alpha":
        return [pinned, c.title or "", c.id]
    if sort_by == "unread":
        return [pinned, c.unread_count or 0, activity, c.id]
    return [pinned, activity, c.id]


async def _tool_list_conversations(db: AsyncSession, args: dict, ctx: dict) -> dict:
    sort_by = args.get("sort_by") or "recent"
    if sort_by not in _CONVERSATION_SORTS:
        raise ToolError(f"sort_by must be one of: {', '.join(_CONVERSATION_SORTS)}")
    kind = args.get("kind") or "any"
    channel = args.get("channel") or None
    tag_id = args.get("tag_id")
    include_archived = args.get("include_archived", True) is not False
    limit = _clamp(args.get("limit"), 50)
    scope = {"s": sort_by, "k": kind, "ch": channel, "g": tag_id, "a": include_archived}
    key = _decode_cursor(args.get("cursor"), "conversations", scope)

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
        q = q.where(_after(cols, key))
    convs = list((await db.execute(q.order_by(*_order_by(cols)).limit(limit + 1))).scalars().all())
    more = len(convs) > limit
    convs = convs[:limit]
    states = await _conversation_states(db, [c.id for c in convs])
    out = []
    for c in convs:
        d = _mcp_conversation(c)
        d["coverage"] = states.get(c.id, {}).get("coverage")
        out.append(d)
    return {
        "conversations": out,
        "next_cursor": _encode_cursor("conversations", _sort_key(convs[-1], sort_by), scope) if more else None,
        "note": None if sort_by == "stable" else (
            "Activity-based order can move conversations between pages while you page; "
            "use sort_by='stable' to visit every conversation exactly once."
        ),
    }


async def _tool_search_conversations(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    convs, total = await crud.search_conversations_by_title(
        db, query, limit=_clamp(args.get("limit"), 20), channel=args.get("channel") or None,
    )
    return {"conversations": [_mcp_conversation(c) for c in convs if _visible(c)], "total": total}


async def _tool_get_conversation(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await _require_visible_conversation(db, args.get("conversation_id"))
    tags = await crud.get_tags_for_conversation(db, conv.id)
    d = _mcp_conversation(conv)
    d["tags"] = [_tag_dict(t) for t in tags]
    d.update((await _conversation_states(db, [conv.id])).get(conv.id, {}))
    return d


def _incomplete_note(conversation_id: int) -> str:
    return (
        "This conversation's history is NOT fully archived yet, so older messages may be missing. "
        f"Call fetch_history(conversation_id={conversation_id}) until history_complete is true, then "
        "read again from the start (order='asc', no cursor)."
    )


async def _tool_get_messages(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await _require_visible_conversation(db, args.get("conversation_id"))
    order = args.get("order") or "desc"
    limit = _clamp(args.get("limit"), 50)
    scope = {"c": conv.id, "o": order}
    key = _decode_cursor(args.get("cursor"), "msgs", scope)
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
        q = q.where(_after(cols, key))
    rows = list((await db.execute(q.order_by(*_order_by(cols)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    state = (await _conversation_states(db, [conv.id])).get(conv.id, {})
    coverage = state.get("coverage") or {}
    complete = bool(coverage.get("history_complete"))

    last = rows[-1][0] if rows else None
    result = {
        "conversation": {
            "conversation_id": conv.id, "title": conv.title, "kind": conv.kind, "channel": channel_of(conv),
        },
        "order": order,
        "messages": [_mcp_msg(row) for row in rows],
        "next_cursor": _encode_cursor("msgs", [last.date, last.seq, last.id], scope) if more and last else None,
        "history_complete": complete,
        "coverage": coverage,
        "review": state.get("review"),
    }
    notes = []
    if not complete:
        notes.append(_incomplete_note(conv.id))
    if not more and order == "asc":
        notes.append(
            f"End of stored history (newest message_id {coverage.get('newest_message_id')}). "
            "Record findings, then mark_reviewed through that id."
        )
    if order == "desc":
        notes.append("For a complete read use order='asc': messages arriving while you page are appended at the end.")
    result["note"] = " ".join(notes) or None
    return result


async def _tool_search_messages(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    limit = _clamp(args.get("limit"), 30)
    channel = args.get("channel") or None
    conversation_id = None
    if args.get("conversation_id") is not None:
        conversation_id = (await _require_visible_conversation(db, args.get("conversation_id"))).id
    scope = {"q": query, "c": conversation_id, "ch": channel}
    key = _decode_cursor(args.get("cursor"), "search", scope)

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
        base = base.where(_after(cols, key))
    rows = list((await db.execute(base.order_by(*_order_by(cols)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1][0] if rows else None
    return {
        "messages": [_search_hit(m, conv) for m, conv in rows],
        "total": total,
        "next_cursor": _encode_cursor("search", [last.date, last.id], scope) if more and last else None,
    }


async def _tool_list_tags(db: AsyncSession, args: dict, ctx: dict) -> dict:
    rows = await crud.get_all_tags_with_counts(db)
    return {"tags": [_tag_dict(t, count) for t, count in rows]}


async def _tool_get_conversations_by_tag(db: AsyncSession, args: dict, ctx: dict) -> dict:
    tag_name = str(args.get("tag_name") or "").strip()
    if not tag_name:
        raise ToolError("tag_name is required")
    channel = args.get("channel") or None
    convs = await crud.get_conversations_by_tag_name(db, tag_name)
    return {"conversations": [
        _mcp_conversation(c) for c in convs if _visible(c) and (channel is None or channel_of(c) == channel)
    ]}


async def _tool_search_people(db: AsyncSession, args: dict, ctx: dict) -> dict:
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    page = await crud.list_people(
        db,
        search=str(args.get("query") or "").strip(),
        contacts_only=args.get("contacts_only") is True,
        important=args.get("important_only") is True,
        limit=_clamp(args.get("limit"), 50),
        offset=offset,
    )
    return {"people": await people_payload(db, page["people"]), "total": page["total"]}


_GROUPS_LIMIT = 50


async def _tool_get_person(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person = await _require_person(db, _int_arg(args, "person_id"))
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
    dm_convs = [c for c in (await crud.get_conversations(db, dm_ids)).values() if _visible(c)]
    dm_convs.sort(key=lambda c: dm_ids.index(c.id))
    states = await _conversation_states(db, [c.id for c in dm_convs])
    dms = []
    for c in dm_convs:
        d = _mcp_conversation(c)
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
             "messages_from_person": n, "last_message_at": _iso(last)}
            for c, n, last in group_rows[:_GROUPS_LIMIT]
        ],
        "groups_total": len(group_rows),
    })
    payload.pop("avatar_url", None)
    for ident in payload["identities"]:
        ident.pop("avatar_url", None)
    return payload


async def _tool_fetch_history(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await _require_visible_conversation(db, args.get("conversation_id"))
    conversation_id = conv.id
    if channel_of(conv) != "telegram":
        raise ToolError(
            f"Conversation {conversation_id} is on {channel_of(conv)}; only Telegram history is "
            "archived by fetch_history. Its stored messages are readable with get_messages."
        )
    try:
        wait = float(args.get("wait_seconds", 20))
    except (TypeError, ValueError):
        raise ToolError("wait_seconds must be a number")
    wait = max(0.0, min(wait, float(MAX_FETCH_WAIT_SECONDS)))
    # End the read transaction: holding a SQLite snapshot across the wait
    # would hide the rows the archive writes meanwhile.
    await db.rollback()
    archive = _archive()
    connected = await _telegram_connected()
    state = archive.get_status().get("state")
    if not connected or state == "stopped":
        # Nothing can arrive while offline: answer now rather than make the
        # agent wait out (and retry) a timeout.
        st = (await _conversation_states(db, [conversation_id])).get(conversation_id, {})
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
    state = (await _conversation_states(db, [conversation_id])).get(conversation_id, {})
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


async def _tool_list_organizations(db: AsyncSession, args: dict, ctx: dict) -> dict:
    orgs = await _list_organizations(search=str(args.get("search") or ""), db=db)
    return {"organizations": orgs}


# ── Write tools ──────────────────────────────────────────────────────────────

def _agent_label(args: dict, ctx: dict) -> Optional[str]:
    label = args.get("agent_label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return ctx.get("client_name") or "mcp agent"


async def _tool_add_person_note(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person = await _require_person(db, _int_arg(args, "person_id"))
    try:
        note = await crud.create_person_note(
            db, person.id,
            kind=args.get("kind"), title=args.get("title") or "", body=args.get("body") or "",
            citations=args.get("citations"), author="agent", agent_label=_agent_label(args, ctx),
        )
    except ValueError as exc:
        raise ToolError(str(exc))
    await broadcast_notes_changed(person.id)
    return note_dict(note)


async def _tool_update_person_note(db: AsyncSession, args: dict, ctx: dict) -> dict:
    note = await crud.get_person_note(db, _int_arg(args, "note_id"))
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


async def _tool_set_person_fields(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person_id = _int_arg(args, "person_id")
    fields = ("is_important", "relationship_status", "twitter_handle", "linkedin_url")
    if all(args.get(f) is None for f in fields):
        raise ToolError(f"Give at least one of: {', '.join(fields)}")
    person = await _require_person(db, person_id)
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


async def _tool_add_position(db: AsyncSession, args: dict, ctx: dict) -> dict:
    person = await _require_person(db, _int_arg(args, "person_id"))
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


async def _tool_tag_conversation(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await _require_visible_conversation(db, args.get("conversation_id"))
    tag, err = await crud.find_or_create_tag(db, str(args.get("tag_name") or ""))
    if tag is None:
        raise ToolError(err or "Could not create tag")
    try:
        await crud.add_tag_to_conversation(db, conv.id, tag.id)
    except crud.StageTagLocked:
        raise ToolError("Board column tags can't be assigned directly")
    return {"conversation_id": conv.id, "tag": _tag_dict(tag)}


async def _tool_mark_reviewed(db: AsyncSession, args: dict, ctx: dict) -> dict:
    conv = await _require_visible_conversation(db, args.get("conversation_id"))
    through = _int_arg(args, "through_message_id") if args.get("through_message_id") is not None else None
    state = (await _conversation_states(db, [conv.id])).get(conv.id, {})
    if not (state.get("coverage") or {}).get("history_complete"):
        raise ToolError(_incomplete_note(conv.id) + " A review can only be recorded on a complete history.")
    try:
        await crud.mark_conversation_reviewed(db, conv.id, through, _agent_label(args, ctx))
    except ValueError as exc:
        raise ToolError(str(exc))
    state = (await _conversation_states(db, [conv.id])).get(conv.id, {})
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


# ── Registry ─────────────────────────────────────────────────────────────────

def _schema(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


_LIMIT_PROP = {"type": "integer", "minimum": 1, "description": f"Max results per page (clamped to {MAX_LIMIT})"}
_CURSOR_PROP = {"type": "string", "description": "next_cursor from the previous page; omit for the first page"}
_CONVERSATION_ID_PROP = {"type": "integer", "description": "Mingly conversation ID (as returned by conversation tools)"}
_PERSON_ID_PROP = {"type": "integer", "description": "Mingly person ID (a DM's person.person_id, or from search_people)"}
_CHANNEL_PROP = {"type": "string", "description": "Only conversations on this channel, e.g. 'telegram'"}
_AGENT_LABEL_PROP = {"type": "string", "maxLength": 100, "description": "Who is writing (defaults to the MCP client name)"}
_CITATIONS_PROP = {
    "type": "array",
    "maxItems": crud.NOTE_CITATIONS_MAX,
    "description": "Messages backing the note; each must be stored locally",
    "items": {
        "type": "object",
        "properties": {"conversation_id": {"type": "integer"}, "message_id": {"type": "integer"}},
        "required": ["conversation_id", "message_id"],
        "additionalProperties": False,
    },
}
_NOTE_KIND_PROP = {"type": "string", "enum": list(crud.NOTE_KINDS)}

_READ = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS: Dict[str, Dict[str, Any]] = {
    "get_archive_status": {
        "description": (
            "Progress of the background DM archive (state, FloodWait, DMs complete/pending/failed) "
            "and how many DMs are reviewed. Start every review here."
        ),
        "inputSchema": _schema({}),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_archive_status,
    },
    "list_dms": {
        "description": (
            "Every direct conversation (DM) on every channel, including archived and hidden ones, with the "
            "person (identities, CRM fields, positions, note count), archive coverage and review state. "
            "filter='unreviewed' is the review work queue; filter='incomplete' lists DMs whose history "
            "isn't fully archived. Stable keyset paging."
        ),
        "inputSchema": _schema({
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
            "filter": {"type": "string", "enum": list(_DM_FILTERS)},
            "channel": _CHANNEL_PROP,
            "include_archived": {"type": "boolean", "description": "Default true"},
            "include_bots": {"type": "boolean", "description": "Default false"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_list_dms,
    },
    "list_conversations": {
        "description": (
            "List conversations (direct/group/channel/email_thread, any channel) with archive coverage, newest "
            "activity first by default. Includes archived/hidden ones (flagged). sort_by='stable' guarantees "
            "each conversation once."
        ),
        "inputSchema": _schema({
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
            "sort_by": {"type": "string", "enum": list(_CONVERSATION_SORTS)},
            "kind": {"type": "string", "enum": list(_KINDS)},
            "channel": _CHANNEL_PROP,
            "include_archived": {"type": "boolean", "description": "Default true"},
            "tag_id": {"type": "integer", "description": "Only conversations carrying this tag (see list_tags)"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_list_conversations,
    },
    "search_conversations": {
        "description": "Search conversations by title (case- and space-insensitive).",
        "inputSchema": _schema({
            "query": {"type": "string", "minLength": 1},
            "channel": _CHANNEL_PROP,
            "limit": _LIMIT_PROP,
        }, ["query"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_search_conversations,
    },
    "get_conversation": {
        "description": "Get one conversation by ID with its tags, last message, archive coverage and review state.",
        "inputSchema": _schema({"conversation_id": _CONVERSATION_ID_PROP}, ["conversation_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_conversation,
    },
    "get_messages": {
        "description": (
            "Read a conversation's stored messages (incl. service events, polls, forwards) with keyset paging. "
            "order='asc' reads oldest→newest and is the one to use for complete reviews; "
            "after_message_id resumes after a stored message (asc only). Says when history isn't complete."
        ),
        "inputSchema": _schema({
            "conversation_id": _CONVERSATION_ID_PROP,
            "cursor": _CURSOR_PROP,
            "order": {"type": "string", "enum": ["asc", "desc"], "description": "Default desc (newest first)"},
            "after_message_id": {"type": "integer", "description": "Start after this message (order='asc')"},
            "limit": _LIMIT_PROP,
        }, ["conversation_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_messages,
    },
    "search_messages": {
        "description": (
            "Search message text and subjects (case-insensitive), newest first, across all conversations "
            "or one; page through every hit with next_cursor."
        ),
        "inputSchema": _schema({
            "query": {"type": "string", "minLength": 1},
            "conversation_id": _CONVERSATION_ID_PROP,
            "channel": _CHANNEL_PROP,
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
        }, ["query"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_search_messages,
    },
    "list_tags": {
        "description": "List all conversation tags with the number of conversations carrying each.",
        "inputSchema": _schema({}),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_list_tags,
    },
    "get_conversations_by_tag": {
        "description": "List conversations carrying a tag whose name matches (case-insensitive).",
        "inputSchema": _schema({
            "tag_name": {"type": "string", "minLength": 1},
            "channel": _CHANNEL_PROP,
        }, ["tag_name"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_conversations_by_tag,
    },
    "search_people": {
        "description": (
            "List people (one per human, merging their identities on every channel), optionally filtered "
            "by name/username/phone, with CRM fields and job positions."
        ),
        "inputSchema": _schema({
            "query": {"type": "string", "description": "Substring match on names, usernames and phones"},
            "contacts_only": {"type": "boolean", "description": "Only people in a channel address book"},
            "important_only": {"type": "boolean", "description": "Only people marked important"},
            "limit": _LIMIT_PROP,
            "offset": {"type": "integer", "minimum": 0, "description": "Pagination offset"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_search_people,
    },
    "get_person": {
        "description": (
            "Everything stored about a person: their identities on every channel, CRM fields, positions and "
            "organizations, the notes timeline, their DMs with coverage/review state, and groups they posted in."
        ),
        "inputSchema": _schema({"person_id": _PERSON_ID_PROP}, ["person_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_person,
    },
    "fetch_history": {
        "description": (
            f"Ask the archive to fetch a Telegram conversation's full history now (DM or group), waiting up to "
            f"wait_seconds (max {MAX_FETCH_WAIT_SECONDS}). Returns coverage; call again until history_complete."
        ),
        "inputSchema": _schema({
            "conversation_id": _CONVERSATION_ID_PROP,
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": MAX_FETCH_WAIT_SECONDS},
        }, ["conversation_id"]),
        "access": "read",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
        "handler": _tool_fetch_history,
    },
    "list_organizations": {
        "description": "List organizations with member counts, optionally filtered by name.",
        "inputSchema": _schema({
            "search": {"type": "string", "description": "Substring match on organization name"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_list_organizations,
    },
    "add_person_note": {
        "description": (
            "Write a note about a person (markdown body) shown on their Mingly profile. kind: context, "
            "due_diligence, risk, relationship or todo. Cite the messages it rests on. Requires write access."
        ),
        "inputSchema": _schema({
            "person_id": _PERSON_ID_PROP,
            "kind": _NOTE_KIND_PROP,
            "title": {"type": "string", "minLength": 1, "maxLength": crud.NOTE_TITLE_MAX},
            "body": {"type": "string", "maxLength": crud.NOTE_BODY_MAX},
            "citations": _CITATIONS_PROP,
            "agent_label": _AGENT_LABEL_PROP,
        }, ["person_id", "kind", "title", "body"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        "handler": _tool_add_person_note,
    },
    "update_person_note": {
        "description": "Edit an agent-written note (user-written notes are read-only to agents). Requires write access.",
        "inputSchema": _schema({
            "note_id": {"type": "integer"},
            "kind": _NOTE_KIND_PROP,
            "title": {"type": "string", "minLength": 1, "maxLength": crud.NOTE_TITLE_MAX},
            "body": {"type": "string", "maxLength": crud.NOTE_BODY_MAX},
            "citations": _CITATIONS_PROP,
        }, ["note_id"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
        "handler": _tool_update_person_note,
    },
    "set_person_fields": {
        "description": (
            "Set a person's important flag, relationship status ('auto' returns it to automatic), "
            "Twitter handle or LinkedIn URL. Free-text findings belong in add_person_note (the person's own "
            "notes field stays the user's). Requires write access."
        ),
        "inputSchema": _schema({
            "person_id": _PERSON_ID_PROP,
            "is_important": {"type": "boolean"},
            "relationship_status": {"type": "string", "enum": ["auto"] + sorted(crud.VALID_STATUSES)},
            "twitter_handle": {"type": "string", "maxLength": 100},
            "linkedin_url": {"type": "string", "maxLength": 300},
        }, ["person_id"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
        "handler": _tool_set_person_fields,
    },
    "add_position": {
        "description": (
            "Record a person's role at an organization (matched by name, created if new). "
            "Requires write access."
        ),
        "inputSchema": _schema({
            "person_id": _PERSON_ID_PROP,
            "organization": {"type": "string", "minLength": 1, "maxLength": 200},
            "title": {"type": "string", "maxLength": 200},
            "is_active": {"type": "boolean", "description": "Current role (default true)"},
        }, ["person_id", "organization"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        "handler": _tool_add_position,
    },
    "tag_conversation": {
        "description": "Add a tag (created if new) to a conversation. Requires write access.",
        "inputSchema": _schema({
            "conversation_id": _CONVERSATION_ID_PROP,
            "tag_name": {"type": "string", "minLength": 1, "maxLength": 100},
        }, ["conversation_id", "tag_name"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "handler": _tool_tag_conversation,
    },
    "mark_reviewed": {
        "description": (
            "Record that you reviewed a conversation through a stored message (its newest message_id after "
            "reading to the end; omit through_message_id for a conversation with no messages). Only allowed "
            "once history is complete. Newer messages put it back in list_dms(filter='unreviewed'). "
            "Requires write access."
        ),
        "inputSchema": _schema({
            "conversation_id": _CONVERSATION_ID_PROP,
            "through_message_id": {"type": "integer", "minimum": 1},
            "agent_label": _AGENT_LABEL_PROP,
        }, ["conversation_id"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "handler": _tool_mark_reviewed,
    },
}


def tool_descriptors() -> List[dict]:
    """The tools/list payload."""
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": spec["inputSchema"],
            "annotations": spec["annotations"],
        }
        for name, spec in TOOLS.items()
    ]


_TYPE_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}

_TYPE_NAMES = {
    "integer": "an integer", "number": "a number", "string": "a string",
    "boolean": "a boolean", "array": "an array", "object": "an object",
}


def _check(value: Any, schema: dict, path: str) -> Optional[str]:
    """The inputSchema subset this registry uses: type, enum, min/maxLength,
    minimum/maximum, maxItems, items, object properties/required/additionalProperties."""
    expected = schema.get("type")
    if expected and not _TYPE_CHECKS[expected](value):
        return f"Argument {path} must be {_TYPE_NAMES[expected]}"
    if "enum" in schema and value not in schema["enum"]:
        return f"Argument {path} must be one of: {', '.join(map(str, schema['enum']))}"
    if expected == "string":
        if len(value) < schema.get("minLength", 0):
            return f"Argument {path} must not be empty" if schema.get("minLength") == 1 else f"Argument {path} is too short"
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            return f"Argument {path} is longer than {schema['maxLength']} characters"
    if expected in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            return f"Argument {path} must be >= {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"Argument {path} must be <= {schema['maximum']}"
    if expected == "array":
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"Argument {path} has more than {schema['maxItems']} items"
        if "items" in schema:
            for i, item in enumerate(value):
                err = _check(item, schema["items"], f"{path}[{i}]")
                if err:
                    return err
    if expected == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if value.get(key) is None:
                return f"Missing required argument: {path + '.' if path else ''}{key}"
        for key, item in value.items():
            sub = props.get(key)
            name = f"{path}.{key}" if path else key
            if sub is None:
                if schema.get("additionalProperties", True) is False:
                    return f"Unknown argument: {name}"
                continue
            if item is None:
                continue
            err = _check(item, sub, name)
            if err:
                return err
    return None


def validate_args(name: str, args: dict) -> Optional[str]:
    """Validate arguments against the tool's inputSchema.

    Returns an error string, or None when valid.
    """
    return _check(args, TOOLS[name]["inputSchema"], "")
