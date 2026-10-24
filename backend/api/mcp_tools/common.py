"""Shared by the MCP tools: limits, keyset cursors, coverage / review state,
the agent-facing serializations and argument helpers.

Ids are Mingly's internal ids (``conversation_id``, ``message_id``,
``person_id``, ``identity_id``); channel ids never reach an agent: message
``meta`` is translated (``MessageContext``) and identities carry a human
``address`` (an email / phone handle) instead of their ``external_id``.
Session-gated media URLs (``avatar_url``, attachment ``url``) are never
included. Conversations from every channel are listed, and tools that list
conversations take an optional ``channel`` filter.

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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.views import conversation_json
from backend.db import crud
from backend.db.models import Account, AgentReview, Conversation, HistoryCoverage, Identity, Message
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
    """Conversations on a channel fetch_history archives. Every other channel
    has nothing more to fetch, so its stored history counts as complete for
    reviews (its connector's history_note says how far back it goes)."""
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
    channels = dict((await db.execute(
        select(Conversation.id, Account.channel).join(Account, Account.id == Conversation.account_id)
        .where(Conversation.id.in_(conversation_ids))
    )).all())
    out = {}
    for cid, cov, review, utd, complete in rows:
        s = stats.get(cid)
        out[cid] = {
            "coverage": {
                "history_complete": bool(complete),
                "fetchable": channels.get(cid) in ARCHIVED_CHANNELS,
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
    "is_deleted_account", "is_self", "is_forum", "labels", "last_activity_at", "history_note",
)


def mcp_conversation(c: Conversation, with_last_message: bool = True) -> dict:
    full = conversation_json(c)
    d = {"conversation_id" if k == "id" else k: full.get(k) for k in _CONVERSATION_KEYS}
    if c.kind != "direct":
        d.pop("is_bot")
        d.pop("is_deleted_account")
    if c.kind not in ("group", "channel"):
        d.pop("is_forum")
    if with_last_message:
        d["last_message"] = full.get("last_message")
    return d


# Message fields an agent gets (serializers.message_out minus attachment URLs,
# HTML and scam state; meta translated, email participants added).
_MESSAGE_KEYS = (
    "id", "conversation_id", "seq", "kind", "sender", "subject", "text", "date", "is_outgoing", "meta",
    "fwd_from_name", "reactions", "reply_to_message_id", "reply_to_preview", "topic_id", "is_pinned",
    "is_edited",
)


# Identity handles a person can be reached at, shown as ``address``; other
# channels' external ids (Telegram user ids, Signal ACIs) are opaque.
_ADDRESS_CHANNELS = ("gmail", "sms")


def identity_address(channel: Optional[str], external_id: Optional[str]) -> Optional[str]:
    return external_id if channel in _ADDRESS_CHANNELS else None


# meta keys that only carry channel plumbing (raw headers, per-reactor maps
# already summarized in ``reactions``, label ids already on the conversation).
_DROPPED_META = ("rfc822_message_id", "references", "in_reply_to", "label_ids", "signal_reactions", "sms_reactions")


@dataclass
class MessageContext:
    """What serializing a page of messages needs beyond the rows: channels,
    email participants, and the Mingly ids of Telegram peers named in meta."""
    channels: Dict[int, str] = field(default_factory=dict)  # conversation_id -> channel
    participants: Dict[int, list] = field(default_factory=dict)
    persons: Dict[int, int] = field(default_factory=dict)  # Telegram user id -> person_id
    conversations: Dict[int, int] = field(default_factory=dict)  # Telegram peer id -> conversation_id

    def peer(self, peer_id: Any, prefix: str) -> dict:
        """``{<prefix>_person_id}`` / ``{<prefix>_conversation_id}`` for a
        Telegram peer Mingly stores; {} for one it doesn't."""
        if not isinstance(peer_id, int) or isinstance(peer_id, bool):
            return {}
        if peer_id > 0 and peer_id in self.persons:
            return {f"{prefix}_person_id": self.persons[peer_id]}
        if peer_id in self.conversations:
            return {f"{prefix}_conversation_id": self.conversations[peer_id]}
        return {}


def _row_message(row) -> Message:
    return row if isinstance(row, Message) else row[0]


def _telegram_peers(meta: dict) -> List[int]:
    found: List[Any] = []
    if isinstance(meta.get("forward"), dict):
        found.append(meta["forward"].get("from_id"))
    svc = meta.get("service")
    if isinstance(svc, dict):
        found += list(svc.get("user_ids") or []) + [svc.get(k) for k in ("user_id", "inviter_id", "channel_id", "chat_id")]
    if isinstance(meta.get("contact"), dict):
        found.append(meta["contact"].get("user_id"))
    if isinstance(meta.get("story"), dict):
        found.append(meta["story"].get("peer_id"))
    found.append(meta.get("sender_peer"))
    return [p for p in found if isinstance(p, int) and not isinstance(p, bool)]


async def message_context(db: AsyncSession, rows: list) -> MessageContext:
    msgs = [_row_message(r) for r in rows]
    ctx = MessageContext()
    if not msgs:
        return ctx
    ctx.channels = dict((await db.execute(
        select(Conversation.id, Account.channel).join(Account, Account.id == Conversation.account_id)
        .where(Conversation.id.in_({m.conversation_id for m in msgs}))
    )).all())
    ctx.participants = await crud.message_participants(db, [m.id for m in msgs])
    peers = set()
    for m in msgs:
        if m.meta_json and ctx.channels.get(m.conversation_id) == "telegram":
            try:
                meta = json.loads(m.meta_json)
            except ValueError:
                continue
            if isinstance(meta, dict):
                peers.update(_telegram_peers(meta))
    if peers:
        ctx.persons = {int(ext): pid for ext, pid in (await db.execute(
            select(Identity.external_id, Identity.person_id)
            .where(Identity.channel == "telegram", Identity.external_id.in_([str(p) for p in peers if p > 0]))
        )).all()}
        ctx.conversations = {int(ext): cid for ext, cid in (await db.execute(
            select(Conversation.external_id, Conversation.id).join(Account, Account.id == Conversation.account_id)
            .where(Account.channel == "telegram", Conversation.external_id.in_([str(p) for p in peers]))
        )).all()}
    return ctx


def _telegram_meta(meta: dict, ctx: MessageContext) -> dict:
    out = dict(meta)
    if isinstance(out.get("forward"), dict):
        fwd = dict(out["forward"])
        fwd.update(ctx.peer(fwd.pop("from_id", None), "from"))
        out["forward"] = fwd
    if isinstance(out.get("service"), dict):
        svc = dict(out["service"])
        if "user_ids" in svc:
            svc["person_ids"] = [ctx.persons[u] for u in svc.pop("user_ids") or [] if u in ctx.persons]
        for key, prefix in (("user_id", "user"), ("inviter_id", "inviter")):
            if key in svc:
                svc.update(ctx.peer(svc.pop(key), prefix))
        for key, prefix in (("channel_id", "migrated_to"), ("chat_id", "migrated_from")):
            if key in svc:
                svc.update(ctx.peer(svc.pop(key), prefix))
        out["service"] = svc
    if isinstance(out.get("contact"), dict):
        contact = dict(out["contact"])
        contact.update(ctx.peer(contact.pop("user_id", None), "contact"))
        out["contact"] = contact
    if isinstance(out.get("story"), dict):
        story = dict(out["story"])
        story.update(ctx.peer(story.pop("peer_id", None), "poster"))
        out["story"] = story
    if "sender_peer" in out:
        out.update(ctx.peer(out.pop("sender_peer"), "sent_as"))
    return out


def mcp_meta(meta: Any, channel: Optional[str], ctx: MessageContext) -> Any:
    """``meta`` with channel ids replaced by Mingly ids (Telegram peers →
    ``*_person_id`` / ``*_conversation_id``, dropped when not stored) and
    channel plumbing removed."""
    if not isinstance(meta, dict):
        return meta
    out = {k: v for k, v in meta.items() if k not in _DROPPED_META}
    if channel == "telegram":
        out = _telegram_meta(out, ctx)
    if isinstance(out.get("sticker"), dict):
        out["sticker"] = True  # pack / sticker ids are opaque
    return out or None


def mcp_msg(row, ctx: MessageContext) -> dict:
    m = _row_message(row)
    full = message_out(row)
    channel = ctx.channels.get(m.conversation_id)
    d = {"message_id" if k == "id" else k: full.get(k) for k in _MESSAGE_KEYS}
    if isinstance(d.get("sender"), dict):
        d["sender"] = {k: v for k, v in d["sender"].items() if k != "avatar_url"}
    d["meta"] = mcp_meta(d["meta"], channel, ctx)
    d["attachments"] = [
        {"kind": a["kind"], "filename": a["filename"], "mime_type": a["mime_type"], "size": a["size"]}
        for a in full["attachments"]
    ]
    parts = ctx.participants.get(m.id)
    if parts:
        d["participants"] = [
            {"role": p["role"], "person_id": p["person_id"], "identity_id": p["identity_id"],
             "name": p["display_name"], "address": identity_address(channel, p["external_id"])}
            for p in parts
        ]
    return d


async def mcp_messages(db: AsyncSession, rows: list) -> List[dict]:
    ctx = await message_context(db, rows)
    return [mcp_msg(r, ctx) for r in rows]


async def search_hits(db: AsyncSession, rows: List[Tuple[Message, Conversation]]) -> List[dict]:
    """Messages found across conversations, each saying where it is."""
    ctx = await message_context(db, [m for m, _ in rows])
    out = []
    for m, conv in rows:
        d = mcp_msg(m, ctx)
        title = conversation_json(conv, with_last_message=False)["title"]
        d.update(conversation_title=title, conversation_kind=conv.kind, channel=channel_of(conv))
        out.append(d)
    return out


def mcp_identity(ident: dict) -> dict:
    """An identity as agents see it: no avatar URL, ``address`` instead of
    the channel's external id."""
    out = {k: v for k, v in ident.items() if k not in ("avatar_url", "external_id")}
    out["address"] = identity_address(ident.get("channel"), ident.get("external_id"))
    return out


def mcp_person(payload: dict) -> dict:
    """``api.people.people_payload`` as agents see it: ``person_id``; the
    user's own ``notes`` field kept; ``person_notes_count`` (notes written
    about them); no avatar URLs; identities via ``mcp_identity``."""
    out = {k: v for k, v in payload.items() if k not in ("avatar_url", "id", "note_count")}
    out = {"person_id": payload["id"], **out}
    out["identities"] = [mcp_identity(i) for i in payload.get("identities", [])]
    out["person_notes_count"] = payload.get("note_count", 0)
    return out


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
                {"identity_id": i.id, "channel": i.channel, "username": i.username, "display_name": i.display_name,
                 "address": identity_address(i.channel, i.external_id)}
                for i in identities.get(pid, [])
            ],
            "is_contact": bool(p.is_contact),
            "is_important": bool(p.is_important),
            "relationship_status": p.relationship_status,
            "twitter_handle": p.twitter_handle,
            "linkedin_url": p.linkedin_url,
            "has_notes": bool(p.notes),  # the user's own notes field (get_person shows it)
            "positions": [
                {"organization": x.organization, "organization_id": x.organization_id,
                 "title": x.title, "is_active": x.is_active}
                for x in positions.get(pid, [])
            ],
            "person_notes_count": notes.get(pid, 0),
        }
    return out


def date_arg(args: dict, key: str) -> Optional[datetime]:
    """An ISO date / datetime argument as naive UTC (None when absent)."""
    raw = args.get(key)
    if raw is None:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"{key} must be an ISO date or datetime, e.g. 2026-10-01 or 2026-10-01T09:00:00Z")
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


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
    """The DMs an agent reviews: every one-to-one conversation on any channel
    (a chat, or an email thread with a single counterpart, as get_person's
    dms), never notes-to-self or deactivated ones."""
    return and_(
        or_(
            Conversation.kind == "direct",
            and_(Conversation.kind == "email_thread", Conversation.peer_identity_id.isnot(None)),
        ),
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
