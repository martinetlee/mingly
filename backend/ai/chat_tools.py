"""Tools of the in-app AI chat (OpenAI function-calling schema) and their
execution over the local data: read chats, search messages, find and tag
chats, sync tagged chats' history. Results are plain text for the model."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation
from backend.telegram.connector import connector as telegram_connector
from backend.telegram.sync import get_tag_sync_state, start_tag_sync

logger = logging.getLogger(__name__)

# Budgets for what the agent sees.
TOOL_MSG_CHARS = 300          # per message text in tool output
TOOL_RESULT_CHARS = 8_000     # per tool result


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_chat_messages",
            "description": (
                "Retrieve recent messages from a specific chat (any channel). "
                "Use this to understand what is being discussed in a particular chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_name": {
                        "type": "string",
                        "description": "Name (or part of the name) of the chat to look up.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent messages to retrieve (default 30, max 100).",
                        "default": 30,
                    },
                },
                "required": ["chat_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_chats_by_tag",
            "description": "Find all chats that have been assigned a specific tag.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag_name": {
                        "type": "string",
                        "description": "Tag name to search for (e.g. 'Work', 'Finance').",
                    },
                },
                "required": ["tag_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": (
                "Search for messages containing a keyword across all chats. "
                "Returns matching messages with their chat name and timestamp."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword or phrase to search for.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of results (default 20, max 50).",
                        "default": 20,
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_chats_by_title",
            "description": (
                "Search for chats whose title/name contains a keyword. Matching ignores "
                "spaces and case ('ETHTaipei' also matches 'ETH Taipei'). The result reports "
                "the total match count; only the first matches are listed if there are many."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to search for in chat titles.",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tag_chat",
            "description": (
                "Assign / add / create a tag to a chat. If the tag does not exist it will be created. "
                "Use this when the user asks you to tag or label a chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chat_name": {
                        "type": "string",
                        "description": "Name (or part of the name) of the chat to tag.",
                    },
                    "tag_name": {
                        "type": "string",
                        "description": "Name of the tag to assign.",
                    },
                },
                "required": ["chat_name", "tag_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_messages_in_tagged_chats",
            "description": (
                "Search message content within all chats carrying a tag, summarized per chat "
                "(match counts and latest snippet). Use this to answer 'which chats tagged X "
                "mention / are related to Y' — it is scoped and complete, unlike search_messages. "
                "Also reports how many tagged chats have no synced messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag_name": {
                        "type": "string",
                        "description": "Tag whose chats should be searched (e.g. 'ETHTaipei').",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to search for in those chats' messages.",
                    },
                },
                "required": ["tag_name", "keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sync_tagged_chats",
            "description": (
                "Download message history from Telegram for all Telegram chats carrying a tag "
                "that have never been synced. Runs in the background and can take several minutes. Use this "
                "when a search reports that many tagged chats have no synced messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag_name": {
                        "type": "string",
                        "description": "Tag whose unsynced chats should be synced.",
                    },
                },
                "required": ["tag_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tag_chats_matching",
            "description": (
                "Tag ALL chats whose title matches a keyword, in one operation. Matching ignores "
                "spaces and case. If the tag does not exist it will be created. Use this when the "
                "user asks to tag many or all chats matching a name — do NOT call tag_chat "
                "repeatedly for that."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to match against chat titles (e.g. 'ETHTaipei').",
                    },
                    "tag_name": {
                        "type": "string",
                        "description": "Name of the tag to assign to every matching chat.",
                    },
                },
                "required": ["keyword", "tag_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_chats_by_category",
            "description": "Find all chats in a specific category.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["tracked", "briefing"],
                        "description": (
                            "'tracked' = chats the user is actively monitoring; "
                            "'briefing' = chats included in the daily briefing."
                        ),
                    },
                },
                "required": ["category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_chats_by_member_count",
            "description": (
                "Find chats by their member count. Supports filtering for chats with "
                "more than, exactly, or less than a given number of members."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "The member count threshold to compare against.",
                    },
                    "operator": {
                        "type": "string",
                        "enum": ["more_than", "exactly", "less_than"],
                        "description": (
                            "'more_than' = chats with more than `count` members; "
                            "'exactly' = chats with exactly `count` members; "
                            "'less_than' = chats with fewer than `count` members."
                        ),
                    },
                },
                "required": ["count", "operator"],
            },
        },
    },
]


def _channel(conv) -> str:
    return (conv.account.channel if conv.account is not None else None) or "unknown"


def _qname(name, fallback: str = "") -> str:
    """Wrap a chat/tag name in backticks so LLMs preserve angle-bracket chars verbatim."""
    return f"`{name or fallback}`"


async def _find_or_create_tag(db: AsyncSession, tag_name: str):
    """crud.find_or_create_tag with the error phrased as a tool error."""
    tag, err = await crud.find_or_create_tag(db, tag_name)
    return tag, (f"Tool error: {err}" if err else None)


async def execute_tool(name: str, args: dict, db: AsyncSession) -> str:
    try:
        if name == "get_chat_messages":
            chat_name = args.get("chat_name", "")
            limit = min(int(args.get("limit", 30)), 100)
            conv = await crud.get_conversation_by_title(db, chat_name)
            if not conv:
                return f"No chat found matching '{chat_name}'."
            rows = (await crud.get_message_window(db, conv.id, limit=limit))["rows"]
            if not rows:
                return f"No messages found in {_qname(conv.title)}."
            # Newest lines that fit the budget, each text clipped.
            lines: list[str] = []
            used = 0
            for m, *_ in reversed(rows):  # newest-first
                body = m.text or m.subject
                if not body:
                    continue
                text = " ".join(body.split())
                if len(text) > TOOL_MSG_CHARS:
                    text = text[:TOOL_MSG_CHARS - 1] + "…"
                line = f"[{m.date.strftime('%Y-%m-%d %H:%M')}] {m.sender_name or 'Unknown'}: {text}"
                if lines and used + len(line) > TOOL_RESULT_CHARS:
                    break
                lines.append(line)
                used += len(line) + 1
            lines.reverse()
            return f"Last {len(lines)} messages in {_qname(conv.title)} ({_channel(conv)}):\n" + "\n".join(lines)

        elif name == "find_chats_by_tag":
            tag_name = args.get("tag_name", "")
            convs = await crud.get_conversations_by_tag_name(db, tag_name)
            if not convs:
                return f"No chats found with tag matching '{tag_name}'."
            names = [_qname(c.title, str(c.id)) for c in convs]
            return f"Chats tagged '{tag_name}':\n" + "\n".join(f"- {n}" for n in names)

        elif name == "search_messages":
            keyword = args.get("keyword", "")
            limit = min(int(args.get("limit", 20)), 50)
            rows, total = await crud.search_messages_by_keyword(db, keyword, limit=limit)
            if not rows:
                return f"No messages found containing '{keyword}'."
            lines = []
            for msg, conv in rows:
                body = msg.text or msg.subject
                if body:
                    ts = msg.date.strftime("%Y-%m-%d %H:%M")
                    sender = msg.sender_name or "Unknown"
                    lines.append(f"[{ts}] {_qname(conv.title, str(conv.id))} — {sender}: {body[:200]}")
            header = f"Found {total} messages containing '{keyword}'"
            if total > len(rows):
                header += (
                    f" (showing the {len(rows)} most recent; for a per-chat overview "
                    "within tagged chats use search_messages_in_tagged_chats)"
                )
            return header + ":\n" + "\n".join(lines)

        elif name == "search_chats_by_title":
            keyword = args.get("keyword", "")
            chats, total = await crud.search_conversations_by_title(db, keyword, limit=50)
            if not chats:
                return f"No chats found with title containing '{keyword}'."
            lines = [f"{_qname(c.title)} ({c.kind}, {_channel(c)})" for c in chats]
            header = f"Found {total} chats matching '{keyword}'"
            if total > len(chats):
                header += (
                    f" (showing first {len(chats)} alphabetically; "
                    "to tag all of them use tag_chats_matching)"
                )
            return header + ":\n" + "\n".join(lines)

        elif name == "tag_chat":
            chat_name = args.get("chat_name", "")
            tag_name = args.get("tag_name", "")
            # Use a fresh session for writes to avoid streaming session lifecycle issues
            async with AsyncSessionLocal() as write_db:
                conv = await crud.get_conversation_by_title(write_db, chat_name)
                if not conv:
                    return f"No chat found matching '{chat_name}'."
                tag, err = await _find_or_create_tag(write_db, tag_name)
                if tag is None:
                    return err
                existing = await crud.get_tags_for_conversation(write_db, conv.id)
                if any(t.id == tag.id for t in existing):
                    return f"{_qname(conv.title)} already has the tag '{tag.name}'."
                await crud.add_tag_to_conversation(write_db, conv.id, tag.id)
                return f"Tagged {_qname(conv.title)} with '{tag.name}'."

        elif name == "search_messages_in_tagged_chats":
            tag_name = args.get("tag_name", "")
            keyword = args.get("keyword", "")
            if not tag_name or not keyword:
                return "Both tag_name and keyword are required."
            total_tagged, _with_msgs, unsynced_ids = await crud.get_tag_sync_coverage(db, tag_name)
            if total_tagged == 0:
                return f"No chats carry a tag matching '{tag_name}'."
            rows = await crud.search_messages_in_tagged_conversations(db, tag_name, keyword)
            total_matches = sum(r.match_count for r in rows)
            if not rows:
                out = f"Among {total_tagged} chats tagged '{tag_name}', none mention '{keyword}'."
            else:
                shown = rows[:40]
                lines = []
                for r in shown:
                    date_str = r.latest_date.strftime("%Y-%m-%d") if r.latest_date else "?"
                    snippet = (r.latest_snippet or "").replace("\n", " ")[:100]
                    lines.append(f"- {_qname(r.title)} — {r.match_count} matches; latest ({date_str}): \"{snippet}\"")
                out = (
                    f"Among {total_tagged} chats tagged '{tag_name}', {len(rows)} mention "
                    f"'{keyword}' ({total_matches} matching messages)"
                )
                if len(rows) > len(shown):
                    out += f" — showing top {len(shown)} by match count"
                out += ":\n" + "\n".join(lines)
            if unsynced_ids:
                out += (
                    f"\nNote: {len(unsynced_ids)} of {total_tagged} tagged chats have no synced "
                    "messages and could not be searched. Use sync_tagged_chats to fetch their history."
                )
            return out

        elif name == "sync_tagged_chats":
            tag_name = args.get("tag_name", "")
            if not tag_name:
                return "tag_name is required."
            if not await telegram_connector.reachable():
                return (
                    "Telegram not connected — chat history can't be synced right now. "
                    "Tell the user to reconnect Telegram and try again."
                )

            def _progress(state: dict) -> str:
                msg = (
                    f"A sync of chats tagged '{state.get('tag_name')}' is already running: "
                    f"{state.get('done', 0)} of {state.get('total', 0)} chats done"
                )
                if state.get("skipped"):
                    msg += f", {state['skipped']} skipped (deactivated groups)"
                if state.get("failed"):
                    msg += f", {state['failed']} failed"
                if state.get("waiting_until"):
                    msg += f". Currently waiting out a Telegram rate limit until {state['waiting_until']} UTC"
                return msg + "."

            running = get_tag_sync_state()
            if running and not running.get("finished"):
                return _progress(running)
            total_tagged, _with_msgs, unsynced_ids = await crud.get_tag_sync_coverage(db, tag_name)
            if total_tagged == 0:
                return f"No chats carry a tag matching '{tag_name}'."
            if not unsynced_ids:
                return f"All {total_tagged} chats tagged '{tag_name}' already have synced messages."
            state = start_tag_sync(tag_name, unsynced_ids)
            if state.get("already_running"):
                return _progress(state)
            n = len(unsynced_ids)
            return (
                f"Started background sync of {n} unsynced chats tagged '{tag_name}' "
                f"(roughly {max(1, n // 30)}-{max(2, n // 15)} minutes). "
                "Tell the user to re-run their question once it completes."
            )

        elif name == "tag_chats_matching":
            keyword = args.get("keyword", "")
            tag_name = args.get("tag_name", "")
            if not keyword or not tag_name:
                return "Both keyword and tag_name are required."
            # Use a fresh session for writes to avoid streaming session lifecycle issues
            async with AsyncSessionLocal() as write_db:
                result = await write_db.execute(
                    select(Conversation).where(crud.title_match(keyword)).order_by(Conversation.title)
                )
                chats = list(result.scalars().all())
                if not chats:
                    return f"No chats found with title matching '{keyword}'. Nothing was tagged."
                tag, err = await _find_or_create_tag(write_db, tag_name)
                if tag is None:
                    return err
                newly = await crud.add_tag_to_conversations(write_db, [c.id for c in chats], tag.id)
                examples = ", ".join(_qname(c.title) for c in chats[:5])
                summary = (
                    f"Tagged {newly} chats matching '{keyword}' with '{tag.name}' "
                    f"({len(chats) - newly} already had it; {len(chats)} match in total)."
                )
                return summary + f" Examples: {examples}."

        elif name == "find_chats_by_category":
            category = args.get("category", "")
            if category == "tracked":
                chats = await crud.get_tracked_conversations(db)
            elif category == "briefing":
                chats = await crud.get_briefing_conversations(db)
            else:
                return f"Unknown category '{category}'. Use 'tracked' or 'briefing'."
            if not chats:
                return f"No chats in category '{category}'."
            names = [_qname(c.title, str(c.id)) for c in chats]
            return f"Chats in '{category}':\n" + "\n".join(f"- {n}" for n in names)

        elif name == "find_chats_by_member_count":
            count = int(args.get("count", 0))
            operator = args.get("operator", "more_than")
            chats = await crud.find_conversations_by_member_count(db, count=count, operator=operator)
            if not chats:
                op_label = {"more_than": "more than", "exactly": "exactly", "less_than": "less than"}.get(operator, operator)
                return f"No chats found with {op_label} {count} members."
            op_label = {"more_than": "more than", "exactly": "exactly", "less_than": "fewer than"}.get(operator, operator)
            lines = [f"- {_qname(c.title, str(c.id))} ({c.members_count} members)" for c in chats]
            return f"Chats with {op_label} {count} members:\n" + "\n".join(lines)

        return f"Unknown tool: {name}"
    except Exception as e:
        return f"Tool error: {e}"
