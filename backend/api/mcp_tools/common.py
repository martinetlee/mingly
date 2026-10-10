"""Shared by the MCP tools: limits, keyset cursors, coverage / review state,
the agent-facing serializations and argument helpers.

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
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.views import conversation_json
from backend.db import crud
from backend.db.models import Account, AgentReview, Conversation, HistoryCoverage, Message
from backend.serialization import iso_utc
from backend.serializers import channel_of, message_out
from backend.telegram import archive
from backend.telegram.archive import ARCHIVED_CHANNELS
from backend.telegram.connector import connector as telegram_connector


MAX_LIMIT = 100


MAX_FETCH_WAIT_SECONDS = 60


EPOCH = datetime(1970, 1, 1)


class ToolError(Exception):
    """Tool-level failure reported to the client as isError content."""


def clamp(value: Any, default: int, maximum: int = MAX_LIMIT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(n, maximum))


def visible(conv) -> bool:
    return conv is not None and not conv.is_deactivated


def iso(dt) -> Optional[str]:
    return iso_utc(dt)


def _jsonable(v):
    return {"dt": v.isoformat()} if isinstance(v, datetime) else v


def _from_jsonable(v):
    if isinstance(v, dict) and set(v) == {"dt"}:
        return datetime.fromisoformat(v["dt"])
    return v


def encode_cursor(kind: str, key: list, scope: dict) -> str:
    raw = json.dumps({"t": kind, "s": scope, "k": [_jsonable(v) for v in key]}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(raw: Any, kind: str, scope: dict) -> Optional[list]:
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


def after(cols: List[Tuple[Any, bool]], key: list):
    """Rows strictly after `key` in the lexicographic order of `cols`
    [(expr, descending)]. Every expr must be non-NULL."""
    if len(key) != len(cols):
        raise ToolError("Invalid cursor: pass back next_cursor unchanged, with the same filters/order")
    clauses = []
    for i, (expr, descending) in enumerate(cols):
        prefix = [cols[j][0] == key[j] for j in range(i)]
        clauses.append(and_(*prefix, expr < key[i] if descending else expr > key[i]))
    return or_(*clauses)


def order_by(cols: List[Tuple[Any, bool]]) -> list:
    return [desc(e) if d else e for e, d in cols]


def with_state(q):
    return (
        q.outerjoin(HistoryCoverage, HistoryCoverage.conversation_id == Conversation.id)
        .outerjoin(AgentReview, AgentReview.conversation_id == Conversation.id)
    )


def _archived_channel():
    """Conversations on a channel fetch_history archives; every other
    channel's stored history counts as complete."""
    return Conversation.account_id.in_(select(Account.id).where(Account.channel.in_(ARCHIVED_CHANNELS)))


COMPLETE = or_(
    func.coalesce(HistoryCoverage.history_complete, False) == True,  # noqa: E712
    not_(_archived_channel()),
)


def review_up_to_date():
    return crud.review_up_to_date(complete=COMPLETE)


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


async def conversation_states(db: AsyncSession, conversation_ids: List[int]) -> Dict[int, dict]:
    """{conversation_id: {"coverage": {...}, "review": {...}}}."""
    if not conversation_ids:
        return {}
    rows = (await db.execute(
        with_state(select(
            Conversation.id, HistoryCoverage, AgentReview, review_up_to_date().label("utd"),
            COMPLETE.label("complete"),
        ))
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
    for cid, cov, review, utd, complete in rows:
        s = stats.get(cid)
        out[cid] = {
            "coverage": {
                "history_complete": bool(complete),
                "message_count": s.n if s else 0,
                "first_message_at": iso(s.first) if s else None,
                "last_message_at": iso(s.last) if s else None,
                "newest_message_id": newest.get(cid),
                "archive_error": cov.error if cov else None,
                "last_walk_at": iso(cov.last_walk_at) if cov else None,
                "last_topup_at": iso(cov.last_topup_at) if cov else None,
            },
            "review": {
                "reviewed_through_message_id": reviewed_through.get(cid) if review else None,
                "reviewed_at": iso(review.reviewed_at) if review else None,
                "agent_label": review.agent_label if review else None,
                "up_to_date": bool(utd),
            },
        }
    return out


# Conversation fields an agent gets (serializers.conversation_out minus
# session-gated media URLs and UI-only state).
_CONVERSATION_KEYS = (
    "id", "account_id", "channel", "kind", "title", "username", "peer", "is_pinned", "is_archived",
    "is_hidden", "is_briefing", "is_tracked", "is_muted", "unread_count", "members_count", "is_bot",
    "is_deleted_account", "last_activity_at",
)


def mcp_conversation(c: Conversation, with_last_message: bool = True) -> dict:
    full = conversation_json(c)
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


def mcp_msg(row) -> dict:
    full = message_out(row)
    d = {"message_id" if k == "id" else k: full.get(k) for k in _MESSAGE_KEYS}
    d["attachments"] = [
        {"kind": a["kind"], "filename": a["filename"], "mime_type": a["mime_type"], "size": a["size"]}
        for a in full["attachments"]
    ]
    return d


def search_hit(m: Message, conv: Conversation) -> dict:
    d = mcp_msg(m)
    d["conversation_title"] = conv.title
    d["channel"] = channel_of(conv)
    return d


def tag_dict(t, conversation_count: Optional[int] = None) -> dict:
    d = {"id": t.id, "name": t.name, "color": t.color, "category": t.category}
    if conversation_count is not None:
        d["conversation_count"] = conversation_count
    return d


async def person_summaries(db: AsyncSession, person_ids: List[int]) -> Dict[int, dict]:
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


def int_arg(args: dict, key: str, label: Optional[str] = None) -> int:
    try:
        value = args.get(key)
        if isinstance(value, bool):
            raise TypeError
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{label or key} must be an integer")


async def require_visible_conversation(db: AsyncSession, conversation_id: Any) -> Conversation:
    try:
        if isinstance(conversation_id, bool):
            raise TypeError
        cid = int(conversation_id)
    except (TypeError, ValueError):
        raise ToolError("conversation_id must be an integer conversation ID")
    conv = await crud.get_conversation(db, cid)
    if not visible(conv):
        raise ToolError(f"Conversation {cid} not found")
    return conv


async def require_person(db: AsyncSession, person_id: int):
    person = await crud.get_person(db, person_id)
    if person is None:
        raise ToolError(f"Person {person_id} not found")
    return person


def get_archive():
    return archive


async def telegram_connected() -> bool:
    return await telegram_connector.reachable()


TELEGRAM_OFFLINE_NOTE = (
    "Telegram isn't connected — open Mingly and unlock it; archiving resumes automatically. "
    "Already-stored messages are still readable. Don't retry fetch_history until "
    "get_archive_status shows telegram_connected: true; tell the user instead."
)


ARCHIVE_STOPPED_NOTE = (
    "The archive isn't running (Mingly is still starting up, or it was stopped). Stored messages "
    "are still readable; check get_archive_status later instead of retrying in a loop."
)


def direct_filter():
    """Direct conversations an agent reviews (never notes-to-self or deactivated ones)."""
    return and_(
        Conversation.kind == "direct",
        Conversation.is_deactivated == False,  # noqa: E712
        func.coalesce(Conversation.is_self, False) == False,  # noqa: E712
    )


def incomplete_note(conversation_id: int) -> str:
    return (
        "This conversation's history is NOT fully archived yet, so older messages may be missing. "
        f"Call fetch_history(conversation_id={conversation_id}) until history_complete is true, then "
        "read again from the start (order='asc', no cursor)."
    )


def agent_label(args: dict, ctx: dict) -> Optional[str]:
    label = args.get("agent_label")
    if isinstance(label, str) and label.strip():
        return label.strip()
    return ctx.get("client_name") or "mcp agent"
