"""The tool registry: schemas, descriptors and argument validation."""

from typing import Any, Callable, Dict, List, Optional

from backend.api.mcp_tools.archive import tool_fetch_history, tool_get_archive_status, tool_mark_reviewed
from backend.api.mcp_tools.boards import tool_get_workspace_board, tool_list_workspaces, tool_move_conversation_stage
from backend.api.mcp_tools.common import MAX_FETCH_WAIT_SECONDS, MAX_LIMIT
from backend.api.mcp_tools.context import (
    tool_get_briefing,
    tool_get_catch_up,
    tool_list_accounts,
    tool_list_briefings,
    tool_list_calendar_events,
)
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
    tool_list_topics,
    tool_search_conversations,
    tool_search_messages,
)
from backend.api.mcp_tools.people import (
    tool_get_organization,
    tool_get_person,
    tool_get_person_timeline,
    tool_list_merge_suggestions,
    tool_list_organizations,
    tool_search_people,
)
from backend.api.mcp_tools.writes import (
    tool_add_person_note,
    tool_add_position,
    tool_set_person_fields,
    tool_suggest_merge,
    tool_tag_conversation,
    tool_untag_conversation,
    tool_update_person_note,
    tool_update_position,
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


_WRITE_ADD = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False}
_WRITE_SET = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False}
_WRITE_IDEMPOTENT = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

_HISTORY_NOTE = (
    "Only Telegram history can be fetched; other channels hold everything Mingly can get, which may not "
    "reach back to the start (see each conversation's history_note)."
)


def _read(description: str, handler, properties: Optional[dict] = None, required: Optional[List[str]] = None,
          annotations: Optional[dict] = None) -> dict:
    return {"description": description, "inputSchema": _schema(properties or {}, required), "access": "read",
            "annotations": annotations or _READ, "handler": handler}


def _write(description: str, handler, properties: dict, required: List[str], annotations: dict) -> dict:
    return {"description": description + " Requires write access.", "inputSchema": _schema(properties, required),
            "access": "write", "annotations": annotations, "handler": handler}


TOOLS: Dict[str, Dict[str, Any]] = {
    # ── Setup ──────────────────────────────────────────────────────────────
    "list_accounts": _read(
        "The user's connected accounts (Telegram, Signal, SMS / iMessage, Gmail…): channel, label, status, "
        "the user's own person_id there, and each channel's history_note (how far back its history goes). "
        "Also the user's own organizations (people working there are colleagues). Start here to know what "
        "Mingly covers.",
        tool_list_accounts,
    ),
    # ── Review loop ────────────────────────────────────────────────────────
    "get_archive_status": _read(
        "Progress of the background Telegram DM archive (state, FloodWait, DMs complete/pending/failed) and "
        "how many DMs (every channel) are complete and reviewed. " + _HISTORY_NOTE + " Start every review here.",
        tool_get_archive_status,
    ),
    "list_dms": _read(
        "Every DM (one-to-one conversation, any channel: chats and single-counterpart email threads), including "
        "archived and hidden ones, with the person (identities, CRM fields, positions, person_notes_count), "
        "coverage and review state. filter='unreviewed' is the review work queue; filter='incomplete' lists DMs "
        "whose Telegram history isn't fully archived yet. Stable keyset paging.",
        tool_list_dms,
        {
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
            "filter": {"type": "string", "enum": list(DM_FILTERS)},
            "channel": _CHANNEL_PROP,
            "include_archived": {"type": "boolean", "description": "Default true"},
            "include_bots": {"type": "boolean", "description": "Default false"},
        },
    ),
    "fetch_history": _read(
        f"Ask the archive to fetch a Telegram conversation's full history now (DM or group), waiting up to "
        f"wait_seconds (max {MAX_FETCH_WAIT_SECONDS}). Returns coverage; call again until history_complete. "
        "Other channels can't fetch more: they answer history_complete=true at once, with their history_note.",
        tool_fetch_history,
        {
            "conversation_id": _CONVERSATION_ID_PROP,
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": MAX_FETCH_WAIT_SECONDS},
        },
        ["conversation_id"],
        {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True},
    ),
    # ── Conversations & messages ───────────────────────────────────────────
    "list_conversations": _read(
        "List conversations (kind direct / group / channel / email_thread, any channel) with coverage, newest "
        "activity first by default. Includes archived / hidden ones (flagged). sort_by='stable' guarantees each "
        "conversation once.",
        tool_list_conversations,
        {
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
            "sort_by": {"type": "string", "enum": list(CONVERSATION_SORTS)},
            "kind": {"type": "string", "enum": list(KINDS)},
            "channel": _CHANNEL_PROP,
            "include_archived": {"type": "boolean", "description": "Default true"},
            "tag_id": {"type": "integer", "description": "Only conversations carrying this tag (see list_tags)"},
            "label": {"type": "string", "description": "Only conversations with this channel label (Gmail, e.g. 'INBOX')"},
        },
    ),
    "search_conversations": _read(
        "Search conversations by title (case- and space-insensitive).",
        tool_search_conversations,
        {"query": {"type": "string", "minLength": 1}, "channel": _CHANNEL_PROP, "limit": _LIMIT_PROP},
        ["query"],
    ),
    "get_conversation": _read(
        "Get one conversation by ID with its tags, last message, coverage and review state.",
        tool_get_conversation,
        {"conversation_id": _CONVERSATION_ID_PROP},
        ["conversation_id"],
    ),
    "get_messages": _read(
        "Read a conversation's stored messages (incl. service events, polls, forwards; emails with subject and "
        "from/to/cc participants) with keyset paging. order='asc' reads oldest→newest and is the one to use for "
        "complete reviews; after_message_id resumes after a stored message (asc only). Says when Telegram "
        "history isn't complete, and the conversation's history_note.",
        tool_get_messages,
        {
            "conversation_id": _CONVERSATION_ID_PROP,
            "cursor": _CURSOR_PROP,
            "order": {"type": "string", "enum": ["asc", "desc"], "description": "Default desc (newest first)"},
            "after_message_id": {"type": "integer", "description": "Start after this message (order='asc')"},
            "limit": _LIMIT_PROP,
        },
        ["conversation_id"],
    ),
    "search_messages": _read(
        "Search message text and email subjects (case-insensitive), newest first, across all conversations or "
        "one; each hit says its conversation (title, kind, channel). Page through every hit with next_cursor.",
        tool_search_messages,
        {
            "query": {"type": "string", "minLength": 1},
            "conversation_id": _CONVERSATION_ID_PROP,
            "channel": _CHANNEL_PROP,
            "kind": {"type": "string", "enum": list(KINDS), "description": "Only in conversations of this kind"},
            "sender_person_id": {"type": "integer", "description": "Only messages this person sent"},
            "since": {"type": "string", "description": "ISO date/datetime (UTC): messages at or after"},
            "until": {"type": "string", "description": "ISO date/datetime (UTC): messages before"},
            "cursor": _CURSOR_PROP,
            "limit": _LIMIT_PROP,
        },
        ["query"],
    ),
    "list_topics": _read(
        "Topics of a forum group (is_forum): topic ids and titles, to read the topic_id on its messages.",
        tool_list_topics,
        {"conversation_id": _CONVERSATION_ID_PROP},
        ["conversation_id"],
    ),
    "list_tags": _read(
        "List all conversation tags with the number of conversations carrying each (category 'stage' tags are "
        "board columns: see get_workspace_board).",
        tool_list_tags,
    ),
    "get_conversations_by_tag": _read(
        "List conversations carrying a tag whose name contains tag_name (case-insensitive; board stage tags "
        "excluded).",
        tool_get_conversations_by_tag,
        {"tag_name": {"type": "string", "minLength": 1}, "channel": _CHANNEL_PROP},
        ["tag_name"],
    ),
    # ── People & organizations ─────────────────────────────────────────────
    "search_people": _read(
        "List people (one per human, merging their identities on every channel), optionally filtered by name, "
        "username, phone or email address, with CRM fields and job positions.",
        tool_search_people,
        {
            "query": {"type": "string", "description": "Substring match on names, usernames, phones and email addresses"},
            "contacts_only": {"type": "boolean", "description": "Only people in a channel address book"},
            "important_only": {"type": "boolean", "description": "Only people marked important"},
            "limit": _LIMIT_PROP,
            "offset": {"type": "integer", "minimum": 0, "description": "Pagination offset"},
        },
    ),
    "get_person": _read(
        "Everything stored about a person: identities on every channel, CRM fields, the user's own notes about "
        "them (notes), positions and organizations, person_notes (written by agents or the user), their DMs "
        "with coverage / review state, groups they posted in and email threads they're on.",
        tool_get_person,
        {"person_id": _PERSON_ID_PROP},
        ["person_id"],
    ),
    "get_person_timeline": _read(
        "A person's history across every channel, newest first: their DMs (both directions), what they sent in "
        "groups, emails they're on, and meetings with them. Keyset paged.",
        tool_get_person_timeline,
        {"person_id": _PERSON_ID_PROP, "cursor": _CURSOR_PROP, "limit": _LIMIT_PROP},
        ["person_id"],
    ),
    "list_merge_suggestions": _read(
        "Pending suggestions that two people are the same human (e.g. same phone on two channels), optionally "
        "for one person.",
        tool_list_merge_suggestions,
        {"person_id": _PERSON_ID_PROP},
    ),
    "list_organizations": _read(
        "List organizations with their context summary and member counts, optionally filtered by name.",
        tool_list_organizations,
        {"search": {"type": "string", "description": "Substring match on organization name"}},
    ),
    "get_organization": _read(
        "One organization: context summary, current and former members (person_id, title) and related group "
        "conversations.",
        tool_get_organization,
        {"organization_id": {"type": "integer", "description": "From list_organizations or a position"}},
        ["organization_id"],
    ),
    # ── What Mingly derives for the user ───────────────────────────────────
    "get_catch_up": _read(
        "Who is waiting on the user: tracked conversations and DMs with important people whose latest message "
        "is incoming and unanswered (minus what the user marked handled or snoozed), with Mingly's catch-up "
        "summary of each.",
        tool_get_catch_up,
        {"limit": _LIMIT_PROP},
    ),
    "list_briefings": _read(
        "Mingly's generated briefings (newest first): id, when, how many days each covers.",
        tool_list_briefings,
        {"limit": _LIMIT_PROP},
    ),
    "get_briefing": _read(
        "One briefing (default: the latest finished) with its entries: per-conversation / per-person summaries "
        "and the messages they rest on.",
        tool_get_briefing,
        {"briefing_id": {"type": "integer", "description": "From list_briefings; omit for the latest"}},
    ),
    "list_calendar_events": _read(
        "Calendar events (Google Calendar) overlapping [start, end), earliest first, with attendees as "
        "person_ids; person_id keeps meetings with that person.",
        tool_list_calendar_events,
        {
            "start": {"type": "string", "description": "ISO date/datetime (UTC)"},
            "end": {"type": "string", "description": "ISO date/datetime (UTC)"},
            "person_id": _PERSON_ID_PROP,
            "limit": {"type": "integer", "minimum": 1, "description": "Max events (default 200, max 1000)"},
        },
        ["start", "end"],
    ),
    "list_workspaces": _read(
        "The user's workspaces (tag-scoped groups of conversations, e.g. a deal pipeline) with member counts.",
        tool_list_workspaces,
    ),
    "get_workspace_board": _read(
        "A workspace's board: its stages (columns) in order with the conversations in each, plus conversations "
        "waiting to be placed and the backlog.",
        tool_get_workspace_board,
        {"workspace_id": {"type": "integer"}, "board_id": {"type": "integer", "description": "Default: the first board"}},
        ["workspace_id"],
    ),
    # ── Writes ─────────────────────────────────────────────────────────────
    "add_person_note": _write(
        "Write a note about a person (markdown body) shown on their Mingly profile. kind: context, "
        "due_diligence, risk, relationship or todo. Cite the messages it rests on.",
        tool_add_person_note,
        {
            "person_id": _PERSON_ID_PROP,
            "kind": _NOTE_KIND_PROP,
            "title": {"type": "string", "minLength": 1, "maxLength": crud.NOTE_TITLE_MAX},
            "body": {"type": "string", "maxLength": crud.NOTE_BODY_MAX},
            "citations": _CITATIONS_PROP,
            "agent_label": _AGENT_LABEL_PROP,
        },
        ["person_id", "kind", "title", "body"],
        _WRITE_ADD,
    ),
    "update_person_note": _write(
        "Edit an agent-written note (user-written notes are read-only to agents).",
        tool_update_person_note,
        {
            "note_id": {"type": "integer"},
            "kind": _NOTE_KIND_PROP,
            "title": {"type": "string", "minLength": 1, "maxLength": crud.NOTE_TITLE_MAX},
            "body": {"type": "string", "maxLength": crud.NOTE_BODY_MAX},
            "citations": _CITATIONS_PROP,
        },
        ["note_id"],
        _WRITE_SET,
    ),
    "set_person_fields": _write(
        "Set a person's important flag, relationship status ('auto' returns it to automatic), Twitter handle "
        "or LinkedIn URL. Free-text findings belong in add_person_note (the person's own notes field stays "
        "the user's).",
        tool_set_person_fields,
        {
            "person_id": _PERSON_ID_PROP,
            "is_important": {"type": "boolean"},
            "relationship_status": {"type": "string", "enum": ["auto"] + sorted(crud.VALID_STATUSES)},
            "twitter_handle": {"type": "string", "maxLength": 100},
            "linkedin_url": {"type": "string", "maxLength": 300},
        },
        ["person_id"],
        _WRITE_SET,
    ),
    "add_position": _write(
        "Record a person's role at an organization (matched by name, created if new).",
        tool_add_position,
        {
            "person_id": _PERSON_ID_PROP,
            "organization": {"type": "string", "minLength": 1, "maxLength": 200},
            "title": {"type": "string", "maxLength": 200},
            "is_active": {"type": "boolean", "description": "Current role (default true)"},
        },
        ["person_id", "organization"],
        _WRITE_ADD,
    ),
    "update_position": _write(
        "Edit one of a person's positions (position_id from get_person): new title, or end it (is_active "
        "false, ended_at = when they left).",
        tool_update_position,
        {
            "person_id": _PERSON_ID_PROP,
            "position_id": {"type": "integer"},
            "title": {"type": "string", "maxLength": 200},
            "is_active": {"type": "boolean"},
            "ended_at": {"type": "string", "description": "ISO date they left; empty string clears it"},
        },
        ["person_id", "position_id"],
        _WRITE_SET,
    ),
    "tag_conversation": _write(
        "Add a tag (created if new) to a conversation.",
        tool_tag_conversation,
        {"conversation_id": _CONVERSATION_ID_PROP, "tag_name": {"type": "string", "minLength": 1, "maxLength": 100}},
        ["conversation_id", "tag_name"],
        _WRITE_IDEMPOTENT,
    ),
    "untag_conversation": _write(
        "Remove a tag from a conversation (board stages are changed with move_conversation_stage).",
        tool_untag_conversation,
        {"conversation_id": _CONVERSATION_ID_PROP, "tag_name": {"type": "string", "minLength": 1, "maxLength": 100}},
        ["conversation_id", "tag_name"],
        _WRITE_SET,
    ),
    "move_conversation_stage": _write(
        "Place a conversation on a stage (column) of a workspace board, as the user does by dragging it; "
        "stage null puts it back in the backlog.",
        tool_move_conversation_stage,
        {
            "workspace_id": {"type": "integer"},
            "board_id": {"type": "integer", "description": "From get_workspace_board"},
            "conversation_id": _CONVERSATION_ID_PROP,
            "stage": {"type": "string", "description": "Stage name as get_workspace_board lists it; null = backlog"},
        },
        ["workspace_id", "board_id", "conversation_id"],
        _WRITE_SET,
    ),
    "suggest_merge": _write(
        "Suggest that two people are the same human. It appears in Mingly's merge suggestions for the user to "
        "accept or dismiss; nothing is merged by this tool.",
        tool_suggest_merge,
        {
            "person_a_id": _PERSON_ID_PROP,
            "person_b_id": _PERSON_ID_PROP,
            "reason": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Why you think so"},
            "agent_label": _AGENT_LABEL_PROP,
        },
        ["person_a_id", "person_b_id", "reason"],
        _WRITE_IDEMPOTENT,
    ),
    "mark_reviewed": _write(
        "Record that you reviewed a conversation through a stored message (its newest message_id after reading "
        "to the end; omit through_message_id for a conversation with no messages). Only allowed once "
        "history_complete (always, outside Telegram). Newer messages put it back in list_dms(filter='unreviewed').",
        tool_mark_reviewed,
        {
            "conversation_id": _CONVERSATION_ID_PROP,
            "through_message_id": {"type": "integer", "minimum": 1},
            "agent_label": _AGENT_LABEL_PROP,
        },
        ["conversation_id"],
        _WRITE_IDEMPOTENT,
    ),
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
