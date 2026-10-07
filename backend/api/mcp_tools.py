"""
Tool registry for the MCP endpoint.

Each tool returns a plain dict that the transport layer (backend/api/mcp.py)
serializes into an MCP text-content result. Every tool is tagged with an
``access`` of "read" or "write"; mcp.py refuses write tools unless the user
enabled them (Settings › MCP, pref ``mcp_write_enabled``).

Visibility: deactivated chats (groups migrated to a supergroup) are excluded
everywhere. Hidden, archived and briefing chats are included and flagged
(``is_hidden`` / ``is_archived`` / ``is_briefing``): those flags declutter the
app's chat list, and an agent asked to cover "everyone I've DMed" must not
silently skip anyone. The scam LLM columns (scam_llm_prompt /
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
    AgentReview, Chat, ChatHistoryCoverage, Contact, ContactPosition, Message, Organization,
)
from backend.serialization import iso_utc
from backend.api.validators import validate_linkedin_url
from backend.api.contacts import _contact_dict, _get_positions, _position_dict
from backend.api.organizations import list_organizations as _list_organizations
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


def _chat_visible(chat) -> bool:
    return chat is not None and not chat.is_deactivated


def _iso(dt) -> Optional[str]:
    return iso_utc(dt)


def _flag(obj, name: str) -> bool:
    return bool(getattr(obj, name, False))


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
        q.outerjoin(ChatHistoryCoverage, ChatHistoryCoverage.chat_id == Chat.telegram_id)
        .outerjoin(AgentReview, AgentReview.chat_id == Chat.telegram_id)
    )


_COMPLETE = func.coalesce(ChatHistoryCoverage.history_complete, False) == True  # noqa: E712


async def _chat_states(db: AsyncSession, chat_ids: List[int]) -> Dict[int, dict]:
    """{chat_id: {"coverage": {...}, "review": {...}}} for the given chats."""
    if not chat_ids:
        return {}
    rows = (await db.execute(
        _with_state(select(Chat.telegram_id, ChatHistoryCoverage, AgentReview, crud.review_up_to_date().label("utd")))
        .where(Chat.telegram_id.in_(chat_ids))
    )).all()
    stats = {
        r.chat_id: r for r in (await db.execute(
            select(
                Message.chat_id,
                func.count().label("n"),
                func.min(Message.date).label("first"),
                func.max(Message.date).label("last"),
                func.max(Message.tg_message_id).label("newest"),
            ).where(Message.chat_id.in_(chat_ids)).group_by(Message.chat_id)
        )).all()
    }
    out = {}
    for chat_id, cov, review, utd in rows:
        s = stats.get(chat_id)
        out[chat_id] = {
            "coverage": {
                "history_complete": bool(cov and cov.history_complete),
                "message_count": s.n if s else 0,
                "first_message_at": _iso(s.first) if s else None,
                "last_message_at": _iso(s.last) if s else None,
                "newest_tg_id": s.newest if s else None,
                "archive_error": cov.error if cov else None,
                "last_walk_at": _iso(cov.last_walk_at) if cov else None,
                "last_topup_at": _iso(cov.last_topup_at) if cov else None,
            },
            "review": {
                "reviewed_through_tg_id": review.reviewed_through_tg_id if review else None,
                "reviewed_at": _iso(review.reviewed_at) if review else None,
                "agent_label": review.agent_label if review else None,
                "up_to_date": bool(utd),
            },
        }
    return out


# ── Serializers ──────────────────────────────────────────────────────────────

def _mcp_chat(c, last_text=None, last_sender=None, last_date=None) -> dict:
    """Chat shape mirroring chats._chat_dict, minus session-gated media URLs."""
    name = c.name
    if name and name.isdigit() and c.username:
        name = c.username
    d = {
        "telegram_id": c.telegram_id,
        "name": name,
        "type": c.type,
        "username": c.username,
        "is_pinned": c.is_pinned,
        "is_archived": c.is_archived,
        "is_hidden": _flag(c, "is_hidden"),
        "is_briefing": _flag(c, "is_briefing"),
        "is_muted": c.is_muted,
        "unread_count": c.unread_count or 0,
        "members_count": c.members_count,
        "last_activity_at": _iso(getattr(c, "last_activity_at", None)),
    }
    if c.type == "private":
        d["is_bot"] = _flag(c, "is_bot")
        d["is_deleted_account"] = _flag(c, "is_deleted_account")
    if last_text is not None or last_sender is not None or last_date is not None:
        d["last_message"] = {
            "text": (last_text[:200] if last_text and len(last_text) > 200 else last_text) or "",
            "sender_name": last_sender,
            "date": _iso(last_date),
        }
    else:
        d["last_message"] = None
    return d


def _meta(m) -> Any:
    raw = getattr(m, "meta_json", None)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _mcp_msg(row) -> dict:
    """Message shape mirroring chats._serialize_msg, minus media URLs and scam LLM columns."""
    m, reply_sender, reply_text, reply_media_type = row
    reply_preview = None
    if m.reply_to_tg_message_id is not None:
        reply_preview = {
            "sender_name": reply_sender,
            "text": reply_text,
            "media_type": reply_media_type,
        }
    return {
        "tg_message_id": m.tg_message_id,
        "chat_id": m.chat_id,
        "kind": getattr(m, "kind", None) or "message",
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "text": m.text,
        "date": _iso(m.date),
        "is_outgoing": m.is_outgoing,
        "media_type": m.media_type,
        "media_filename": m.media_filename,
        "meta": _meta(m),
        "fwd_from_name": getattr(m, "fwd_from_name", None),
        "reactions": json.loads(m.reactions_json) if m.reactions_json else None,
        "reply_to_tg_message_id": m.reply_to_tg_message_id,
        "reply_to_preview": reply_preview,
        "topic_id": m.topic_id,
        "is_pinned": m.is_pinned,
        "is_edited": m.is_edited,
    }


def _search_hit(m, chat) -> dict:
    return {
        "tg_message_id": m.tg_message_id,
        "chat_id": m.chat_id,
        "chat_name": chat.name,
        "kind": getattr(m, "kind", None) or "message",
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "text": m.text,
        "date": _iso(m.date),
        "is_outgoing": m.is_outgoing,
    }


def _tag_dict(t, chat_count: Optional[int] = None) -> dict:
    d = {"id": t.id, "name": t.name, "color": t.color, "category": t.category}
    if chat_count is not None:
        d["chat_count"] = chat_count
    return d


def _person_summary(telegram_id: int, chat, contact, positions, notes_count: int) -> dict:
    name = None
    if contact:
        name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or None
    if not name and chat is not None:
        name = chat.name
    return {
        "telegram_id": telegram_id,
        "name": name or str(telegram_id),
        "username": (contact.username if contact else None) or (chat.username if chat is not None else None),
        "contact": None if contact is None else {
            "is_important": contact.is_important,
            "relationship_status": contact.relationship_status,
            "phone": contact.phone,
            "twitter_handle": contact.twitter_handle,
            "linkedin_url": contact.linkedin_url,
            "has_contact_notes": bool(contact.notes),
        },
        "positions": [
            {"organization": p.organization, "organization_id": p.organization_id,
             "title": p.title, "is_active": p.is_active}
            for p in positions
        ],
        "notes_count": notes_count,
    }


# ── Lookups ──────────────────────────────────────────────────────────────────

def _int_arg(args: dict, key: str, label: Optional[str] = None) -> int:
    try:
        value = args.get(key)
        if isinstance(value, bool):
            raise TypeError
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{label or key} must be an integer")


async def _require_visible_chat(db: AsyncSession, chat_id: Any):
    try:
        telegram_id = int(chat_id)
    except (TypeError, ValueError):
        raise ToolError("chat_id must be an integer Telegram chat ID")
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not _chat_visible(chat):
        raise ToolError(f"Chat {telegram_id} not found")
    return chat


async def _require_known_person(db: AsyncSession, telegram_id: int) -> None:
    """A person Mingly knows: a DM, a contact, a position, or a stored message they sent."""
    known = (await db.execute(select(
        select(Chat.id).where(Chat.telegram_id == telegram_id, Chat.type == "private").exists()
        | select(Contact.id).where(Contact.telegram_id == telegram_id).exists()
        | select(ContactPosition.id).where(ContactPosition.telegram_id == telegram_id).exists()
        | select(Message.id).where(Message.sender_id == telegram_id).exists()
    ))).scalar()
    if not known:
        raise ToolError(f"Person {telegram_id} not found (no DM, contact or message from them)")


async def _contacts_by_id(db: AsyncSession, ids: List[int]) -> Dict[int, Contact]:
    if not ids:
        return {}
    rows = (await db.execute(select(Contact).where(Contact.telegram_id.in_(ids)))).scalars().all()
    return {c.telegram_id: c for c in rows}


async def _positions_by_id(db: AsyncSession, ids: List[int]) -> Dict[int, List[ContactPosition]]:
    out: Dict[int, List[ContactPosition]] = {}
    if ids:
        rows = (await db.execute(
            select(ContactPosition).where(ContactPosition.telegram_id.in_(ids))
            .order_by(ContactPosition.is_active.desc(), func.lower(ContactPosition.organization), ContactPosition.id)
        )).scalars().all()
        for p in rows:
            out.setdefault(p.telegram_id, []).append(p)
    return out


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


# ── Read tools ───────────────────────────────────────────────────────────────

async def _tool_get_archive_status(db: AsyncSession, args: dict, ctx: dict) -> dict:
    status = await _archive().refresh_status()  # get_status() counts are cached, zero before the first run
    connected = await _telegram_connected()
    dm = and_(Chat.type == "private", Chat.is_deactivated == False, func.coalesce(Chat.is_self, False) == False)  # noqa: E712
    not_bot = func.coalesce(Chat.is_bot, False) == False  # noqa: E712
    row = (await db.execute(
        _with_state(select(
            func.count(),
            func.sum(case((and_(not_bot, _COMPLETE), 1), else_=0)),
            func.sum(case((and_(not_bot, crud.review_up_to_date()), 1), else_=0)),
            func.sum(case((not_bot, 0), else_=1)),
        )).where(dm)
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
    limit = _clamp(args.get("limit"), 50)
    scope = {"f": filt, "a": include_archived, "b": include_bots}
    key = _decode_cursor(args.get("cursor"), "dms", scope)

    q = _with_state(select(Chat)).where(
        Chat.type == "private",
        Chat.is_deactivated == False,  # noqa: E712
        func.coalesce(Chat.is_self, False) == False,  # noqa: E712
    )
    if not include_archived:
        q = q.where(Chat.is_archived == False)  # noqa: E712
    if not include_bots:
        q = q.where(func.coalesce(Chat.is_bot, False) == False)  # noqa: E712
    if filt == "unreviewed":
        q = q.where(not_(crud.review_up_to_date()))
    elif filt == "incomplete":
        q = q.where(not_(_COMPLETE))
    if key is not None:
        q = q.where(Chat.id > key[0])
    chats = list((await db.execute(q.order_by(Chat.id).limit(limit + 1))).scalars().all())
    more = len(chats) > limit
    chats = chats[:limit]

    ids = [c.telegram_id for c in chats]
    states = await _chat_states(db, ids)
    contacts = await _contacts_by_id(db, ids)
    positions = await _positions_by_id(db, ids)
    notes = await crud.count_person_notes(db, ids)
    dms = []
    for c in chats:
        d = _mcp_chat(c)
        d.pop("last_message", None)
        d["chat_id"] = d.pop("telegram_id")
        d["person"] = _person_summary(
            c.telegram_id, c, contacts.get(c.telegram_id), positions.get(c.telegram_id, []),
            notes.get(c.telegram_id, 0),
        )
        d.update(states.get(c.telegram_id, {}))
        dms.append(d)
    return {
        "dms": dms,
        "next_cursor": _encode_cursor("dms", [chats[-1].id], scope) if more else None,
        "filter": filt,
        "note": (
            "Ordered by when Mingly first saw each DM, so paging never skips one; new DMs "
            "appear at the end. Saved Messages is not listed; bots only with include_bots=true."
        ),
    }


_CHAT_SORTS = ("recent", "unread", "alpha", "stable")


def _chat_sort_cols(sort_by: str) -> List[Tuple[Any, bool]]:
    pinned = (func.coalesce(Chat.is_pinned, False), True)
    activity = (func.coalesce(Chat.last_activity_at, _EPOCH), True)
    if sort_by == "stable":
        return [(Chat.id, False)]
    if sort_by == "alpha":
        return [pinned, (func.coalesce(Chat.name, ""), False), (Chat.id, False)]
    if sort_by == "unread":
        return [pinned, (func.coalesce(Chat.unread_count, 0), True), activity, (Chat.id, True)]
    return [pinned, activity, (Chat.id, True)]


def _chat_sort_key(c, sort_by: str) -> list:
    pinned = 1 if c.is_pinned else 0
    activity = c.last_activity_at or _EPOCH
    if sort_by == "stable":
        return [c.id]
    if sort_by == "alpha":
        return [pinned, c.name or "", c.id]
    if sort_by == "unread":
        return [pinned, c.unread_count or 0, activity, c.id]
    return [pinned, activity, c.id]


async def _tool_list_chats(db: AsyncSession, args: dict, ctx: dict) -> dict:
    sort_by = args.get("sort_by") or "recent"
    if sort_by not in _CHAT_SORTS:
        raise ToolError(f"sort_by must be one of: {', '.join(_CHAT_SORTS)}")
    chat_type = args.get("type") or "any"
    tag_id = args.get("tag_id")
    include_archived = args.get("include_archived", True) is not False
    limit = _clamp(args.get("limit"), 50)
    scope = {"s": sort_by, "t": chat_type, "g": tag_id, "a": include_archived}
    key = _decode_cursor(args.get("cursor"), "chats", scope)

    cols = _chat_sort_cols(sort_by)
    q = select(Chat).where(Chat.is_deactivated == False)  # noqa: E712
    if chat_type != "any":
        q = q.where(Chat.type == chat_type)
    if not include_archived:
        q = q.where(Chat.is_archived == False)  # noqa: E712
    if tag_id is not None:
        from backend.db.models import ChatTag
        q = q.where(Chat.telegram_id.in_(select(ChatTag.chat_id).where(ChatTag.tag_id == tag_id)))
    if key is not None:
        q = q.where(_after(cols, key))
    chats = list((await db.execute(q.order_by(*_order_by(cols)).limit(limit + 1))).scalars().all())
    more = len(chats) > limit
    chats = chats[:limit]
    states = await _chat_states(db, [c.telegram_id for c in chats])
    out = []
    for c in chats:
        d = _mcp_chat(c, c.last_message_text, c.last_message_sender, c.last_message_date)
        d["coverage"] = states.get(c.telegram_id, {}).get("coverage")
        out.append(d)
    return {
        "chats": out,
        "next_cursor": _encode_cursor("chats", _chat_sort_key(chats[-1], sort_by), scope) if more else None,
        "note": None if sort_by == "stable" else (
            "Activity-based order can move chats between pages while you page; "
            "use sort_by='stable' to visit every chat exactly once."
        ),
    }


async def _tool_search_chats(db: AsyncSession, args: dict, ctx: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    chats, total = await crud.search_chats_by_title(db, query, limit=_clamp(args.get("limit"), 20))
    visible = [c for c in chats if _chat_visible(c)]
    return {"chats": [_mcp_chat(c) for c in visible], "total": total}


async def _tool_get_chat(db: AsyncSession, args: dict, ctx: dict) -> dict:
    chat = await _require_visible_chat(db, args.get("chat_id"))
    tags = await crud.get_tags_for_chat(db, chat.telegram_id)
    d = _mcp_chat(chat, chat.last_message_text, chat.last_message_sender, chat.last_message_date)
    d["tags"] = [_tag_dict(t) for t in tags]
    d.update((await _chat_states(db, [chat.telegram_id])).get(chat.telegram_id, {}))
    return d


def _incomplete_note(chat_id: int) -> str:
    return (
        "This chat's history is NOT fully archived yet, so older messages may be missing. "
        f"Call fetch_history(chat_id={chat_id}) until history_complete is true, then read "
        "again from the start (order='asc', no cursor)."
    )


async def _tool_get_messages(db: AsyncSession, args: dict, ctx: dict) -> dict:
    chat = await _require_visible_chat(db, args.get("chat_id"))
    order = args.get("order") or "desc"
    limit = _clamp(args.get("limit"), 50)
    scope = {"c": chat.telegram_id, "o": order}
    key = _decode_cursor(args.get("cursor"), "msgs", scope)
    after_id = args.get("after_tg_message_id")
    if after_id is not None:
        if key is not None:
            raise ToolError("Pass either cursor or after_tg_message_id, not both")
        if order != "asc":
            raise ToolError("after_tg_message_id requires order='asc'")
        # Resume after a message (e.g. review.reviewed_through_tg_id). If it was
        # deleted since, resume after the newest stored message before it.
        anchor = (await db.execute(
            select(Message.date, Message.tg_message_id)
            .where(Message.chat_id == chat.telegram_id, Message.tg_message_id <= after_id)
            .order_by(desc(Message.tg_message_id)).limit(1)
        )).first()
        if anchor is not None:
            key = [anchor.date, anchor.tg_message_id]

    descending = order == "desc"
    cols = [(Message.date, descending), (Message.tg_message_id, descending)]
    q = crud._with_reply_preview().where(Message.chat_id == chat.telegram_id)
    if key is not None:
        q = q.where(_after(cols, key))
    rows = list((await db.execute(q.order_by(*_order_by(cols)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    state = (await _chat_states(db, [chat.telegram_id])).get(chat.telegram_id, {})
    coverage = state.get("coverage") or {}
    complete = bool(coverage.get("history_complete"))

    last = rows[-1][0] if rows else None
    result = {
        "chat": {"telegram_id": chat.telegram_id, "name": chat.name, "type": chat.type},
        "order": order,
        "messages": [_mcp_msg(row) for row in rows],
        "next_cursor": _encode_cursor("msgs", [last.date, last.tg_message_id], scope) if more and last else None,
        "history_complete": complete,
        "coverage": coverage,
        "review": state.get("review"),
    }
    notes = []
    if not complete:
        notes.append(_incomplete_note(chat.telegram_id))
    if not more and order == "asc":
        notes.append(
            f"End of stored history (newest tg_message_id {coverage.get('newest_tg_id')}). "
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
    chat_id = None
    if args.get("chat_id") is not None:
        chat_id = (await _require_visible_chat(db, args.get("chat_id"))).telegram_id
    scope = {"q": query, "c": chat_id}
    key = _decode_cursor(args.get("cursor"), "search", scope)

    match = [crud.contains_ci(Message.text, query), Chat.is_deactivated == False]  # noqa: E712
    if chat_id is not None:
        match.append(Message.chat_id == chat_id)
    base = select(Message, Chat).join(Chat, Message.chat_id == Chat.telegram_id).where(*match)
    total = (await db.execute(
        select(func.count()).select_from(Message).join(Chat, Message.chat_id == Chat.telegram_id).where(*match)
    )).scalar() or 0
    cols = [(Message.date, True), (Message.id, True)]
    if key is not None:
        base = base.where(_after(cols, key))
    rows = list((await db.execute(base.order_by(*_order_by(cols)).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    last = rows[-1][0] if rows else None
    return {
        "messages": [_search_hit(m, chat) for m, chat in rows],
        "total": total,
        "next_cursor": _encode_cursor("search", [last.date, last.id], scope) if more and last else None,
    }


async def _tool_list_tags(db: AsyncSession, args: dict, ctx: dict) -> dict:
    rows = await crud.get_all_tags_with_counts(db)
    return {"tags": [_tag_dict(t, count) for t, count in rows]}


async def _tool_get_chats_by_tag(db: AsyncSession, args: dict, ctx: dict) -> dict:
    tag_name = str(args.get("tag_name") or "").strip()
    if not tag_name:
        raise ToolError("tag_name is required")
    chats = await crud.get_chats_by_tag_name(db, tag_name)
    return {"chats": [_mcp_chat(c) for c in chats if _chat_visible(c)]}


async def _tool_search_contacts(db: AsyncSession, args: dict, ctx: dict) -> dict:
    search = str(args.get("query") or "").strip()
    q = select(Contact)
    if search:
        q = q.where(or_(
            crud.contains_ci(Contact.first_name, search),
            crud.contains_ci(Contact.last_name, search),
            crud.contains_ci(Contact.username, search),
        ))
    try:
        offset = max(0, int(args.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    q = (
        q.order_by(Contact.first_name, Contact.last_name, Contact.id)
        .limit(_clamp(args.get("limit"), 50))
        .offset(offset)
    )
    contacts = list((await db.execute(q)).scalars().all())
    pos_map = await _positions_by_id(db, [c.telegram_id for c in contacts])
    return {"contacts": [_contact_dict(c, pos_map.get(c.telegram_id, [])) for c in contacts]}


async def _tool_get_contact(db: AsyncSession, args: dict, ctx: dict) -> dict:
    telegram_id = _int_arg(args, "telegram_id")
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    if not contact:
        raise ToolError(f"Contact {telegram_id} not found")
    return _contact_dict(contact, await _get_positions(db, telegram_id))


_GROUPS_LIMIT = 50


async def _tool_get_person(db: AsyncSession, args: dict, ctx: dict) -> dict:
    telegram_id = _int_arg(args, "telegram_id")
    await _require_known_person(db, telegram_id)
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    positions = await _get_positions(db, telegram_id)
    dm = (await db.execute(
        select(Chat).where(Chat.telegram_id == telegram_id, Chat.type == "private", Chat.is_deactivated == False)  # noqa: E712
    )).scalar_one_or_none()
    notes = await crud.list_person_notes(db, telegram_id)

    name = None
    if contact:
        name = _contact_dict(contact)["name"]
    elif dm is not None:
        name = dm.name
    if not name:
        row = (await db.execute(
            select(Message.sender_name).where(Message.sender_id == telegram_id, Message.sender_name.isnot(None))
            .order_by(desc(Message.date)).limit(1)
        )).first()
        name = row[0] if row else str(telegram_id)

    org_ids = [p.organization_id for p in positions if p.organization_id is not None]
    orgs = []
    if org_ids:
        orgs = [
            {"id": o.id, "name": o.name, "context": o.context}
            for o in (await db.execute(select(Organization).where(Organization.id.in_(org_ids)))).scalars().all()
        ]

    in_groups = (
        select(
            Message.chat_id,
            func.count().label("n"),
            func.max(Message.date).label("last"),
        )
        .where(Message.sender_id == telegram_id, Message.chat_id != telegram_id)
        .group_by(Message.chat_id)
        .subquery()
    )
    group_rows = (await db.execute(
        select(Chat, in_groups.c.n, in_groups.c.last)
        .join(in_groups, in_groups.c.chat_id == Chat.telegram_id)
        .where(Chat.is_deactivated == False)  # noqa: E712
        .order_by(desc(in_groups.c.last), Chat.id)
    )).all()

    dm_info = None
    if dm is not None:
        dm_info = _mcp_chat(dm, dm.last_message_text, dm.last_message_sender, dm.last_message_date)
        dm_info.update((await _chat_states(db, [dm.telegram_id])).get(dm.telegram_id, {}))
    return {
        "telegram_id": telegram_id,
        "name": name,
        "username": (contact.username if contact else None) or (dm.username if dm is not None else None),
        "contact": _contact_dict(contact, positions) if contact else None,
        "positions": [_position_dict(p) for p in positions],
        "organizations": orgs,
        "notes": [note_dict(n) for n in notes],
        "dm": dm_info,
        "groups": [
            {"chat_id": c.telegram_id, "name": c.name, "type": c.type,
             "messages_from_person": n, "last_message_at": _iso(last)}
            for c, n, last in group_rows[:_GROUPS_LIMIT]
        ],
        "groups_total": len(group_rows),
    }


async def _tool_fetch_history(db: AsyncSession, args: dict, ctx: dict) -> dict:
    chat = await _require_visible_chat(db, args.get("chat_id"))
    chat_id = chat.telegram_id
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
        st = (await _chat_states(db, [chat_id])).get(chat_id, {})
        coverage = st.get("coverage") or {}
        return {
            "chat_id": chat_id,
            "history_complete": bool(coverage.get("history_complete")),
            "telegram_connected": connected,
            "coverage": coverage,
            "archive_state": state,
            "note": TELEGRAM_OFFLINE_NOTE if not connected else ARCHIVE_STOPPED_NOTE,
        }
    try:
        await archive.request_chat(chat_id, priority=True)
        if wait > 0:
            # Returns the current coverage row at the timeout, complete or not
            await archive.wait_for_chat(chat_id, wait)
    except Exception as exc:  # Telegram not connected, vault locked, …
        raise ToolError(f"Could not fetch history for chat {chat_id}: {exc}")
    state = (await _chat_states(db, [chat_id])).get(chat_id, {})
    coverage = state.get("coverage") or {}
    complete = bool(coverage.get("history_complete"))
    if complete:
        note = "History is complete; read it with get_messages(order='asc')."
    elif coverage.get("archive_error"):
        note = f"The walk stopped before completing: {coverage['archive_error']}. Call fetch_history again to retry."
    else:
        note = "Still archiving in the background; call fetch_history again (or poll get_archive_status)."
    return {
        "chat_id": chat_id,
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
    telegram_id = _int_arg(args, "telegram_id")
    await _require_known_person(db, telegram_id)
    try:
        note = await crud.create_person_note(
            db, telegram_id,
            kind=args.get("kind"), title=args.get("title") or "", body=args.get("body") or "",
            citations=args.get("citations"), author="agent", agent_label=_agent_label(args, ctx),
        )
    except ValueError as exc:
        raise ToolError(str(exc))
    await broadcast_notes_changed(telegram_id)
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
    await broadcast_notes_changed(note.telegram_id)
    return note_dict(note)


async def _broadcast_person_changed(telegram_id: int) -> None:
    from backend.ws.manager import ws_manager
    await ws_manager.broadcast({"type": "person_changed", "telegram_id": telegram_id})


async def _tool_set_person_fields(db: AsyncSession, args: dict, ctx: dict) -> dict:
    telegram_id = _int_arg(args, "telegram_id")
    fields = ("is_important", "relationship_status", "twitter_handle", "linkedin_url")
    if all(args.get(f) is None for f in fields):
        raise ToolError(f"Give at least one of: {', '.join(fields)}")
    await _require_known_person(db, telegram_id)
    status = args.get("relationship_status")
    if status is not None and status != "auto" and status not in crud.VALID_STATUSES:
        raise ToolError(f"relationship_status must be 'auto' or one of: {', '.join(sorted(crud.VALID_STATUSES))}")

    linkedin = args.get("linkedin_url")
    if linkedin and linkedin.strip():
        try:
            validate_linkedin_url(linkedin)  # before a contact gets created for nothing
        except ValueError as exc:
            raise ToolError(str(exc))

    existing = (await db.execute(select(Contact).where(Contact.telegram_id == telegram_id))).scalar_one_or_none()
    if existing is None:
        dm = (await db.execute(
            select(Chat).where(Chat.telegram_id == telegram_id, Chat.type == "private")
        )).scalar_one_or_none()
        seed = {"first_name": dm.name, "username": dm.username} if dm is not None else {}
        existing = await crud.get_or_create_contact(db, telegram_id, **seed)
    contact = existing
    try:
        crud.apply_contact_fields(
            contact,
            twitter_handle=args.get("twitter_handle"),
            linkedin_url=args.get("linkedin_url"),
            is_important=args.get("is_important"),
        )
        if status is not None and status != "auto":
            crud.set_manual_relationship_status(contact, status)
    except ValueError as exc:
        await db.rollback()
        raise ToolError(str(exc))
    if status == "auto":
        contact.relationship_status_manual = False
    await db.commit()
    if status == "auto":
        contact = await crud.refresh_contact_status(db, contact.id) or contact
    await db.refresh(contact)
    await _broadcast_person_changed(telegram_id)
    return _contact_dict(contact, await _get_positions(db, telegram_id))


async def _tool_add_position(db: AsyncSession, args: dict, ctx: dict) -> dict:
    telegram_id = _int_arg(args, "telegram_id")
    await _require_known_person(db, telegram_id)
    try:
        pos = await crud.add_person_position(
            db, telegram_id,
            organization=args.get("organization") or "",
            title=args.get("title"),
            is_active=args.get("is_active", True) is not False,
        )
    except (LookupError, ValueError) as exc:
        raise ToolError(str(exc))
    await _broadcast_person_changed(telegram_id)
    return _position_dict(pos)


async def _tool_tag_chat(db: AsyncSession, args: dict, ctx: dict) -> dict:
    chat = await _require_visible_chat(db, args.get("chat_id"))
    tag, err = await crud.find_or_create_tag(db, str(args.get("tag_name") or ""))
    if tag is None:
        raise ToolError(err or "Could not create tag")
    try:
        await crud.add_tag_to_chat(db, chat.telegram_id, tag.id)
    except crud.StageTagLocked:
        raise ToolError("Board column tags can't be assigned directly")
    return {"chat_id": chat.telegram_id, "tag": _tag_dict(tag)}


async def _tool_mark_reviewed(db: AsyncSession, args: dict, ctx: dict) -> dict:
    chat = await _require_visible_chat(db, args.get("chat_id"))
    through = _int_arg(args, "through_tg_message_id")
    state = (await _chat_states(db, [chat.telegram_id])).get(chat.telegram_id, {})
    if not (state.get("coverage") or {}).get("history_complete"):
        raise ToolError(_incomplete_note(chat.telegram_id) + " A review can only be recorded on a complete history.")
    try:
        await crud.mark_chat_reviewed(db, chat.telegram_id, through, _agent_label(args, ctx))
    except ValueError as exc:
        raise ToolError(str(exc))
    state = (await _chat_states(db, [chat.telegram_id])).get(chat.telegram_id, {})
    review = state.get("review") or {}
    newest = (state.get("coverage") or {}).get("newest_tg_id")
    return {
        "chat_id": chat.telegram_id,
        "review": review,
        "note": None if review.get("up_to_date") else (
            f"Not up to date: messages newer than {review.get('reviewed_through_tg_id')} are stored "
            f"(newest {newest}). Read them with get_messages(order='asc', "
            f"after_tg_message_id={review.get('reviewed_through_tg_id')})."
        ),
    }


# ── Registry ─────────────────────────────────────────────────────────────────

def _schema(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


_LIMIT_PROP = {"type": "integer", "minimum": 1, "description": f"Max results per page (clamped to {MAX_LIMIT})"}
_CURSOR_PROP = {"type": "string", "description": "next_cursor from the previous page; omit for the first page"}
_CHAT_ID_PROP = {"type": "integer", "description": "Telegram chat ID (as returned by chat tools)"}
_PERSON_ID_PROP = {"type": "integer", "description": "Telegram user ID (a DM's chat_id is the person's ID)"}
_AGENT_LABEL_PROP = {"type": "string", "maxLength": 100, "description": "Who is writing (defaults to the MCP client name)"}
_CITATIONS_PROP = {
    "type": "array",
    "maxItems": crud.NOTE_CITATIONS_MAX,
    "description": "Messages backing the note; each must be stored locally",
    "items": {
        "type": "object",
        "properties": {"chat_id": {"type": "integer"}, "tg_message_id": {"type": "integer"}},
        "required": ["chat_id", "tg_message_id"],
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
            "Every private chat (DM), including archived and hidden ones, with the person (contact fields, "
            "positions, note count), archive coverage and review state. filter='unreviewed' is the review "
            "work queue; filter='incomplete' lists DMs whose history isn't fully archived. Stable keyset paging."
        ),
        "inputSchema": _schema({
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
            "filter": {"type": "string", "enum": list(_DM_FILTERS)},
            "include_archived": {"type": "boolean", "description": "Default true"},
            "include_bots": {"type": "boolean", "description": "Default false"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_list_dms,
    },
    "list_chats": {
        "description": (
            "List Telegram chats (private/group/channel) with archive coverage, newest activity first by "
            "default. Includes archived/hidden chats (flagged). sort_by='stable' guarantees each chat once."
        ),
        "inputSchema": _schema({
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
            "sort_by": {"type": "string", "enum": list(_CHAT_SORTS)},
            "type": {"type": "string", "enum": ["any", "private", "group", "channel"]},
            "include_archived": {"type": "boolean", "description": "Default true"},
            "tag_id": {"type": "integer", "description": "Only chats carrying this tag (see list_tags)"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_list_chats,
    },
    "search_chats": {
        "description": "Search chats by title (case- and space-insensitive).",
        "inputSchema": _schema({
            "query": {"type": "string", "minLength": 1},
            "limit": _LIMIT_PROP,
        }, ["query"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_search_chats,
    },
    "get_chat": {
        "description": "Get one chat by Telegram ID with its tags, last message, archive coverage and review state.",
        "inputSchema": _schema({"chat_id": _CHAT_ID_PROP}, ["chat_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_chat,
    },
    "get_messages": {
        "description": (
            "Read a chat's stored messages (incl. service events, polls, forwards) with keyset paging. "
            "order='asc' reads oldest→newest and is the one to use for complete reviews; "
            "after_tg_message_id resumes after a message (asc only). Says when history isn't complete."
        ),
        "inputSchema": _schema({
            "chat_id": _CHAT_ID_PROP,
            "cursor": _CURSOR_PROP,
            "order": {"type": "string", "enum": ["asc", "desc"], "description": "Default desc (newest first)"},
            "after_tg_message_id": {"type": "integer", "description": "Start after this message (order='asc')"},
            "limit": _LIMIT_PROP,
        }, ["chat_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_messages,
    },
    "search_messages": {
        "description": (
            "Search message text (case-insensitive), newest first, across all chats or one chat; "
            "page through every hit with next_cursor."
        ),
        "inputSchema": _schema({
            "query": {"type": "string", "minLength": 1},
            "chat_id": _CHAT_ID_PROP,
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
        }, ["query"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_search_messages,
    },
    "list_tags": {
        "description": "List all chat tags with the number of chats carrying each.",
        "inputSchema": _schema({}),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_list_tags,
    },
    "get_chats_by_tag": {
        "description": "List chats carrying a tag whose name matches (case-insensitive).",
        "inputSchema": _schema({
            "tag_name": {"type": "string", "minLength": 1},
        }, ["tag_name"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_chats_by_tag,
    },
    "search_contacts": {
        "description": "List saved contacts, optionally filtered by name/username, with job positions.",
        "inputSchema": _schema({
            "query": {"type": "string", "description": "Substring match on first/last/username"},
            "limit": _LIMIT_PROP,
            "offset": {"type": "integer", "minimum": 0, "description": "Pagination offset"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_search_contacts,
    },
    "get_contact": {
        "description": "Get one saved contact by Telegram user ID, including job positions.",
        "inputSchema": _schema({"telegram_id": _PERSON_ID_PROP}, ["telegram_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_contact,
    },
    "get_person": {
        "description": (
            "Everything stored about a person (saved contact or not): contact fields, positions and "
            "organizations, the notes timeline, their DM with coverage/review state, and groups they posted in."
        ),
        "inputSchema": _schema({"telegram_id": _PERSON_ID_PROP}, ["telegram_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": _tool_get_person,
    },
    "fetch_history": {
        "description": (
            f"Ask the archive to fetch a chat's full history from Telegram now (DM or group), waiting up to "
            f"wait_seconds (max {MAX_FETCH_WAIT_SECONDS}). Returns coverage; call again until history_complete."
        ),
        "inputSchema": _schema({
            "chat_id": _CHAT_ID_PROP,
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": MAX_FETCH_WAIT_SECONDS},
        }, ["chat_id"]),
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
            "telegram_id": _PERSON_ID_PROP,
            "kind": _NOTE_KIND_PROP,
            "title": {"type": "string", "minLength": 1, "maxLength": crud.NOTE_TITLE_MAX},
            "body": {"type": "string", "maxLength": crud.NOTE_BODY_MAX},
            "citations": _CITATIONS_PROP,
            "agent_label": _AGENT_LABEL_PROP,
        }, ["telegram_id", "kind", "title", "body"]),
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
            "Twitter handle or LinkedIn URL; saves them as a contact if needed. Free-text findings belong "
            "in add_person_note (the contact's own notes field stays the user's). Requires write access."
        ),
        "inputSchema": _schema({
            "telegram_id": _PERSON_ID_PROP,
            "is_important": {"type": "boolean"},
            "relationship_status": {"type": "string", "enum": ["auto"] + sorted(crud.VALID_STATUSES)},
            "twitter_handle": {"type": "string", "maxLength": 100},
            "linkedin_url": {"type": "string", "maxLength": 300},
        }, ["telegram_id"]),
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
            "telegram_id": _PERSON_ID_PROP,
            "organization": {"type": "string", "minLength": 1, "maxLength": 200},
            "title": {"type": "string", "maxLength": 200},
            "is_active": {"type": "boolean", "description": "Current role (default true)"},
        }, ["telegram_id", "organization"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        "handler": _tool_add_position,
    },
    "tag_chat": {
        "description": "Add a tag (created if new) to a chat. Requires write access.",
        "inputSchema": _schema({
            "chat_id": _CHAT_ID_PROP,
            "tag_name": {"type": "string", "minLength": 1, "maxLength": 100},
        }, ["chat_id", "tag_name"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "handler": _tool_tag_chat,
    },
    "mark_reviewed": {
        "description": (
            "Record that you reviewed a chat through a stored message (use its newest tg_message_id after "
            "reading to the end; 0 for a chat with no messages). Only allowed once history is complete. "
            "Newer messages put the chat back in list_dms(filter='unreviewed'). Requires write access."
        ),
        "inputSchema": _schema({
            "chat_id": _CHAT_ID_PROP,
            "through_tg_message_id": {"type": "integer", "minimum": 0},
            "agent_label": _AGENT_LABEL_PROP,
        }, ["chat_id", "through_tg_message_id"]),
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
