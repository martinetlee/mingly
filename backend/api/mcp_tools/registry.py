"""The tool registry: schemas, descriptors and argument validation."""

from typing import Any, Callable, Dict, List, Optional

from backend.api.mcp_tools.archive import tool_fetch_history, tool_get_archive_status, tool_mark_reviewed
from backend.api.mcp_tools.common import MAX_FETCH_WAIT_SECONDS, MAX_LIMIT
from backend.api.mcp_tools.conversations import (
    CONVERSATION_SORTS,
    DM_FILTERS,
    KINDS,
    tool_get_conversation,
    tool_get_conversations_by_tag,
    tool_get_messages,
    tool_list_conversations,
    tool_list_dms,
    tool_list_tags,
    tool_search_conversations,
    tool_search_messages,
)
from backend.api.mcp_tools.people import tool_get_person, tool_list_organizations, tool_search_people
from backend.api.mcp_tools.writes import (
    tool_add_person_note,
    tool_add_position,
    tool_set_person_fields,
    tool_tag_conversation,
    tool_update_person_note,
)
from backend.db import crud


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
            "Progress of the background Telegram DM archive (state, FloodWait, DMs complete/pending/failed) "
            "and how many DMs (every channel) are complete and reviewed. Only Telegram history is archived; "
            "other channels' DMs always count as complete. Start every review here."
        ),
        "inputSchema": _schema({}),
        "access": "read",
        "annotations": _READ,
        "handler": tool_get_archive_status,
    },
    "list_dms": {
        "description": (
            "Every direct conversation (DM) on every channel, including archived and hidden ones, with the "
            "person (identities, CRM fields, positions, note count), archive coverage and review state. "
            "filter='unreviewed' is the review work queue; filter='incomplete' lists DMs whose history "
            "isn't fully archived (Telegram only: other channels count as complete). Stable keyset paging."
        ),
        "inputSchema": _schema({
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
            "filter": {"type": "string", "enum": list(DM_FILTERS)},
            "channel": _CHANNEL_PROP,
            "include_archived": {"type": "boolean", "description": "Default true"},
            "include_bots": {"type": "boolean", "description": "Default false"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": tool_list_dms,
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
            "sort_by": {"type": "string", "enum": list(CONVERSATION_SORTS)},
            "kind": {"type": "string", "enum": list(KINDS)},
            "channel": _CHANNEL_PROP,
            "include_archived": {"type": "boolean", "description": "Default true"},
            "tag_id": {"type": "integer", "description": "Only conversations carrying this tag (see list_tags)"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": tool_list_conversations,
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
        "handler": tool_search_conversations,
    },
    "get_conversation": {
        "description": "Get one conversation by ID with its tags, last message, archive coverage and review state.",
        "inputSchema": _schema({"conversation_id": _CONVERSATION_ID_PROP}, ["conversation_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": tool_get_conversation,
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
        "handler": tool_get_messages,
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
        "handler": tool_search_messages,
    },
    "list_tags": {
        "description": "List all conversation tags with the number of conversations carrying each.",
        "inputSchema": _schema({}),
        "access": "read",
        "annotations": _READ,
        "handler": tool_list_tags,
    },
    "get_conversations_by_tag": {
        "description": "List conversations carrying a tag whose name matches (case-insensitive).",
        "inputSchema": _schema({
            "tag_name": {"type": "string", "minLength": 1},
            "channel": _CHANNEL_PROP,
        }, ["tag_name"]),
        "access": "read",
        "annotations": _READ,
        "handler": tool_get_conversations_by_tag,
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
        "handler": tool_search_people,
    },
    "get_person": {
        "description": (
            "Everything stored about a person: their identities on every channel, CRM fields, positions and "
            "organizations, the notes timeline, their DMs with coverage/review state, and groups they posted in."
        ),
        "inputSchema": _schema({"person_id": _PERSON_ID_PROP}, ["person_id"]),
        "access": "read",
        "annotations": _READ,
        "handler": tool_get_person,
    },
    "fetch_history": {
        "description": (
            f"Ask the archive to fetch a Telegram conversation's full history now (DM or group), waiting up to "
            f"wait_seconds (max {MAX_FETCH_WAIT_SECONDS}). Returns coverage; call again until history_complete. "
            "Other channels aren't archived on demand: they answer history_complete=true at once."
        ),
        "inputSchema": _schema({
            "conversation_id": _CONVERSATION_ID_PROP,
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": MAX_FETCH_WAIT_SECONDS},
        }, ["conversation_id"]),
        "access": "read",
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
        "handler": tool_fetch_history,
    },
    "list_organizations": {
        "description": "List organizations with member counts, optionally filtered by name.",
        "inputSchema": _schema({
            "search": {"type": "string", "description": "Substring match on organization name"},
        }),
        "access": "read",
        "annotations": _READ,
        "handler": tool_list_organizations,
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
        "handler": tool_add_person_note,
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
        "handler": tool_update_person_note,
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
        "handler": tool_set_person_fields,
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
        "handler": tool_add_position,
    },
    "tag_conversation": {
        "description": "Add a tag (created if new) to a conversation. Requires write access.",
        "inputSchema": _schema({
            "conversation_id": _CONVERSATION_ID_PROP,
            "tag_name": {"type": "string", "minLength": 1, "maxLength": 100},
        }, ["conversation_id", "tag_name"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "handler": tool_tag_conversation,
    },
    "mark_reviewed": {
        "description": (
            "Record that you reviewed a conversation through a stored message (its newest message_id after "
            "reading to the end; omit through_message_id for a conversation with no messages). Only allowed "
            "once history is complete (always the case outside Telegram). Newer messages put it back in "
            "list_dms(filter='unreviewed'). "
            "Requires write access."
        ),
        "inputSchema": _schema({
            "conversation_id": _CONVERSATION_ID_PROP,
            "through_message_id": {"type": "integer", "minimum": 1},
            "agent_label": _AGENT_LABEL_PROP,
        }, ["conversation_id"]),
        "access": "write",
        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        "handler": tool_mark_reviewed,
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
