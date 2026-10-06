"""
Read-only tool registry for the MCP endpoint.

Each tool wraps an existing crud/query helper and returns a plain dict that
the transport layer (backend/api/mcp.py) serializes into an MCP text-content
result. Hidden and deactivated chats are excluded from every tool, and the
scam LLM columns (scam_llm_prompt / scam_llm_response / scam_trace_json)
are never serialized.
"""
import json
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.models import Contact, ContactPosition
from backend.serialization import iso_utc
from backend.api.contacts import _contact_dict
from backend.api.organizations import list_organizations as _list_organizations

MAX_LIMIT = 100


class ToolError(Exception):
    """Tool-level failure reported to the client as isError content."""


def _clamp(value: Any, default: int, maximum: int = MAX_LIMIT) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(n, maximum))


def _chat_visible(chat) -> bool:
    return chat is not None and not chat.is_hidden and not chat.is_deactivated


def _iso(dt) -> Optional[str]:
    return iso_utc(dt)


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
        "is_muted": c.is_muted,
        "unread_count": c.unread_count or 0,
        "members_count": c.members_count,
        "last_activity_at": _iso(getattr(c, "last_activity_at", None)),
    }
    if last_text is not None or last_sender is not None or last_date is not None:
        d["last_message"] = {
            "text": (last_text[:200] if last_text and len(last_text) > 200 else last_text) or "",
            "sender_name": last_sender,
            "date": _iso(last_date),
        }
    else:
        d["last_message"] = None
    return d


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
        "sender_id": m.sender_id,
        "sender_name": m.sender_name,
        "text": m.text,
        "date": _iso(m.date),
        "is_outgoing": m.is_outgoing,
        "media_type": m.media_type,
        "media_filename": m.media_filename,
        "reactions": json.loads(m.reactions_json) if m.reactions_json else None,
        "reply_to_tg_message_id": m.reply_to_tg_message_id,
        "reply_to_preview": reply_preview,
        "topic_id": m.topic_id,
        "is_pinned": m.is_pinned,
        "is_edited": m.is_edited,
    }


def _tag_dict(t, chat_count: Optional[int] = None) -> dict:
    d = {"id": t.id, "name": t.name, "color": t.color, "category": t.category}
    if chat_count is not None:
        d["chat_count"] = chat_count
    return d


async def _require_visible_chat(db: AsyncSession, chat_id: Any):
    try:
        telegram_id = int(chat_id)
    except (TypeError, ValueError):
        raise ToolError("chat_id must be an integer Telegram chat ID")
    chat = await crud.get_chat_by_telegram_id(db, telegram_id)
    if not _chat_visible(chat):
        raise ToolError(f"Chat {telegram_id} not found")
    return chat


# --- Tool handlers ---

async def _tool_list_chats(db: AsyncSession, args: dict) -> dict:
    sort_by = args.get("sort_by", "recent")
    if sort_by not in ("recent", "unread", "alpha"):
        raise ToolError("sort_by must be one of: recent, unread, alpha")
    tag_id = args.get("tag_id")
    if tag_id is not None:
        try:
            tag_id = int(tag_id)
        except (TypeError, ValueError):
            raise ToolError("tag_id must be an integer")
    rows = await crud.get_chats(
        db,
        limit=_clamp(args.get("limit"), 50),
        offset=_clamp(args.get("offset"), 0, 1_000_000),
        sort_by=sort_by,
        tag_id=tag_id,
    )
    return {"chats": [_mcp_chat(*row) for row in rows]}


async def _tool_search_chats(db: AsyncSession, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    chats, total = await crud.search_chats_by_title(db, query, limit=_clamp(args.get("limit"), 20))
    visible = [c for c in chats if _chat_visible(c)]
    return {"chats": [_mcp_chat(c) for c in visible], "total": total}


async def _tool_get_chat(db: AsyncSession, args: dict) -> dict:
    chat = await _require_visible_chat(db, args.get("chat_id"))
    tags = await crud.get_tags_for_chat(db, chat.telegram_id)
    d = _mcp_chat(chat, chat.last_message_text, chat.last_message_sender, chat.last_message_date)
    d["tags"] = [_tag_dict(t) for t in tags]
    return d


async def _tool_get_messages(db: AsyncSession, args: dict) -> dict:
    chat = await _require_visible_chat(db, args.get("chat_id"))
    rows = await crud.get_messages(
        db,
        chat.telegram_id,
        limit=_clamp(args.get("limit"), 50),
        offset=_clamp(args.get("offset"), 0, 1_000_000),
    )
    return {
        "chat": {"telegram_id": chat.telegram_id, "name": chat.name, "type": chat.type},
        "messages": [_mcp_msg(row) for row in rows],
    }


async def _tool_search_messages(db: AsyncSession, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    limit = _clamp(args.get("limit"), 30)
    if args.get("chat_id") is not None:
        chat = await _require_visible_chat(db, args.get("chat_id"))
        rows = await crud.search_messages_in_chat(db, chat.telegram_id, query, limit=limit)
        messages = [
            {
                "tg_message_id": m.tg_message_id,
                "chat_id": m.chat_id,
                "chat_name": chat.name,
                "sender_name": m.sender_name,
                "text": m.text,
                "date": _iso(m.date),
            }
            for (m, _rs, _rt, _rm) in rows
        ]
        return {"messages": messages, "total": len(messages)}

    rows, total = await crud.search_messages_by_keyword(db, query, limit=limit)
    messages = [
        {
            "tg_message_id": m.tg_message_id,
            "chat_id": m.chat_id,
            "chat_name": chat.name,
            "sender_name": m.sender_name,
            "text": m.text,
            "date": _iso(m.date),
        }
        for (m, chat) in rows
        if _chat_visible(chat)
    ]
    return {"messages": messages, "total": total}


async def _tool_list_tags(db: AsyncSession, args: dict) -> dict:
    rows = await crud.get_all_tags_with_counts(db)
    return {"tags": [_tag_dict(t, count) for t, count in rows]}


async def _tool_get_chats_by_tag(db: AsyncSession, args: dict) -> dict:
    tag_name = str(args.get("tag_name") or "").strip()
    if not tag_name:
        raise ToolError("tag_name is required")
    chats = await crud.get_chats_by_tag_name(db, tag_name)
    return {"chats": [_mcp_chat(c) for c in chats if _chat_visible(c)]}


async def _tool_search_contacts(db: AsyncSession, args: dict) -> dict:
    search = str(args.get("query") or "").strip()
    q = select(Contact)
    if search:
        pattern = f"%{search}%"
        q = q.where(or_(
            Contact.first_name.ilike(pattern),
            Contact.last_name.ilike(pattern),
            Contact.username.ilike(pattern),
        ))
    q = (
        q.order_by(Contact.first_name, Contact.last_name)
        .limit(_clamp(args.get("limit"), 50))
        .offset(_clamp(args.get("offset"), 0, 1_000_000))
    )
    contacts = list((await db.execute(q)).scalars().all())

    pos_map: Dict[int, List[ContactPosition]] = {}
    if contacts:
        pos_result = await db.execute(
            select(ContactPosition).where(
                ContactPosition.telegram_id.in_([c.telegram_id for c in contacts])
            )
        )
        for p in pos_result.scalars().all():
            pos_map.setdefault(p.telegram_id, []).append(p)
    return {"contacts": [_contact_dict(c, pos_map.get(c.telegram_id, [])) for c in contacts]}


async def _tool_get_contact(db: AsyncSession, args: dict) -> dict:
    try:
        telegram_id = int(args.get("telegram_id"))
    except (TypeError, ValueError):
        raise ToolError("telegram_id must be an integer")
    contact = (await db.execute(
        select(Contact).where(Contact.telegram_id == telegram_id)
    )).scalar_one_or_none()
    if not contact:
        raise ToolError(f"Contact {telegram_id} not found")
    positions = list((await db.execute(
        select(ContactPosition)
        .where(ContactPosition.telegram_id == telegram_id)
        .order_by(ContactPosition.organization)
    )).scalars().all())
    return _contact_dict(contact, positions)


async def _tool_list_organizations(db: AsyncSession, args: dict) -> dict:
    orgs = await _list_organizations(search=str(args.get("search") or ""), db=db)
    return {"organizations": orgs}


# --- Registry ---

def _schema(properties: dict, required: Optional[List[str]] = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


_LIMIT_PROP = {"type": "integer", "description": f"Max results (clamped to {MAX_LIMIT})"}
_OFFSET_PROP = {"type": "integer", "description": "Pagination offset"}
_CHAT_ID_PROP = {"type": "integer", "description": "Telegram chat ID (as returned by chat tools)"}

TOOLS: Dict[str, Dict[str, Any]] = {
    "list_chats": {
        "description": "List Telegram chats (private/group/channel), newest activity first by default. Optionally filter by tag_id.",
        "inputSchema": _schema({
            "limit": _LIMIT_PROP,
            "offset": _OFFSET_PROP,
            "sort_by": {"type": "string", "enum": ["recent", "unread", "alpha"]},
            "tag_id": {"type": "integer", "description": "Only chats carrying this tag (see list_tags)"},
        }),
        "handler": _tool_list_chats,
    },
    "search_chats": {
        "description": "Search chats by title (case- and space-insensitive).",
        "inputSchema": _schema({
            "query": {"type": "string", "minLength": 1},
            "limit": _LIMIT_PROP,
        }, ["query"]),
        "handler": _tool_search_chats,
    },
    "get_chat": {
        "description": "Get one chat by Telegram ID, including its tags and last message.",
        "inputSchema": _schema({"chat_id": _CHAT_ID_PROP}, ["chat_id"]),
        "handler": _tool_get_chat,
    },
    "get_messages": {
        "description": "Read messages of a chat, newest first, with offset pagination.",
        "inputSchema": _schema({
            "chat_id": _CHAT_ID_PROP,
            "limit": _LIMIT_PROP,
            "offset": _OFFSET_PROP,
        }, ["chat_id"]),
        "handler": _tool_get_messages,
    },
    "search_messages": {
        "description": "Search message text (case-insensitive). Searches all chats, or one chat when chat_id is given.",
        "inputSchema": _schema({
            "query": {"type": "string", "minLength": 1},
            "chat_id": _CHAT_ID_PROP,
            "limit": _LIMIT_PROP,
        }, ["query"]),
        "handler": _tool_search_messages,
    },
    "list_tags": {
        "description": "List all chat tags with the number of chats carrying each.",
        "inputSchema": _schema({}),
        "handler": _tool_list_tags,
    },
    "get_chats_by_tag": {
        "description": "List chats carrying a tag whose name matches (case-insensitive).",
        "inputSchema": _schema({
            "tag_name": {"type": "string", "minLength": 1},
        }, ["tag_name"]),
        "handler": _tool_get_chats_by_tag,
    },
    "search_contacts": {
        "description": "List saved contacts, optionally filtered by name/username, with job positions.",
        "inputSchema": _schema({
            "query": {"type": "string", "description": "Substring match on first/last/username"},
            "limit": _LIMIT_PROP,
            "offset": _OFFSET_PROP,
        }),
        "handler": _tool_search_contacts,
    },
    "get_contact": {
        "description": "Get one contact by Telegram user ID, including job positions.",
        "inputSchema": _schema({
            "telegram_id": {"type": "integer", "description": "Telegram user ID"},
        }, ["telegram_id"]),
        "handler": _tool_get_contact,
    },
    "list_organizations": {
        "description": "List organizations with member counts, optionally filtered by name.",
        "inputSchema": _schema({
            "search": {"type": "string", "description": "Substring match on organization name"},
        }),
        "handler": _tool_list_organizations,
    },
}


def tool_descriptors() -> List[dict]:
    """The tools/list payload."""
    return [
        {"name": name, "description": spec["description"], "inputSchema": spec["inputSchema"]}
        for name, spec in TOOLS.items()
    ]


def validate_args(name: str, args: dict) -> Optional[str]:
    """Minimal inputSchema validation: required keys present, basic types match.

    Returns an error string, or None when valid.
    """
    spec = TOOLS[name]["inputSchema"]
    for key in spec["required"]:
        if args.get(key) is None:
            return f"Missing required argument: {key}"
    for key, value in args.items():
        prop = spec["properties"].get(key)
        if prop is None:
            return f"Unknown argument: {key}"
        expected = prop.get("type")
        if value is None:
            continue
        if expected == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"Argument {key} must be an integer"
        if expected == "string" and not isinstance(value, str):
            return f"Argument {key} must be a string"
    return None
