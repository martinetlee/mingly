"""
Intelligence endpoint — agentic LLM chat with tool access to the local DB,
mentions, and the briefing endpoints (backed by backend.briefing.pipeline).
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.sse import sse_response
from backend.briefing.events import get_event_job, sse
from backend.briefing.pipeline import (
    BRIEFING_JOB_KEY,
    replace_briefing_job,
    run_dict,
    start_briefing_job,
    start_regenerate_job,
)
from backend.db import crud
from backend.db.database import AsyncSessionLocal, get_db
from backend.llm.base import ChatMessage, LLMError
from backend.llm.budget import clip_to_tokens, estimate_tokens, prompt_budget_tokens
from backend.llm.resolve import LLMUnavailable, error_kind, error_message, get_provider
from backend.serializers import message_out
from backend.api.views import conversation_json
from backend.telegram.connector import connector as telegram_connector

logger = logging.getLogger(__name__)

router = APIRouter()

# Budgets for what the agent sees.
TOOL_MSG_CHARS = 300          # per message text in tool output
TOOL_RESULT_CHARS = 8_000     # per tool result
HISTORY_MESSAGES = 40         # persisted conversation turns loaded per request
HISTORY_CHARS = 24_000        # cap on replayed history before context budgeting
MAX_TOOL_ROUNDS = 8
AGENT_MAX_TOKENS = 2048

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling schema)
# ---------------------------------------------------------------------------

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

SYSTEM_PROMPT = (
    "You are an intelligent assistant for Common Defense, a personal relationship manager. "
    "You have access to tools that let you search the user's chats and messages (Telegram and "
    "other channels). "
    "Use your tools to answer questions accurately — look up information before responding. "
    "Be concise, helpful, and specific. When listing chats or messages, keep it brief and relevant. "
    "When asked to tag many or all chats matching a name, use the tag_chats_matching tool once "
    "instead of calling tag_chat repeatedly. For questions about message content within tagged "
    "chats (e.g. 'which chats tagged X discuss Y'), use search_messages_in_tagged_chats and "
    "always relay its note about unsynced chats to the user."
)

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

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
            from backend.telegram.sync import start_tag_sync, get_tag_sync_state
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
                from sqlalchemy import select as sa_select
                from backend.db.models import Conversation
                result = await write_db.execute(
                    sa_select(Conversation).where(crud.title_match(keyword)).order_by(Conversation.title)
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


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class IntelligenceMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class IntelligenceChatRequest(BaseModel):
    # Preferred: send ai_conversation_id + the new user message text only. The
    # backend persists the user message, loads the (windowed) history, and
    # persists the assistant reply + tool trace when the turn completes.
    ai_conversation_id: Optional[int] = None
    message: Optional[str] = None
    # Legacy fallback: explicit messages list (not persisted)
    messages: Optional[list[IntelligenceMessage]] = None


# ---------------------------------------------------------------------------
# Streaming agentic loop
# ---------------------------------------------------------------------------

def _emit_text_chunks(content: str, chunk_size: int = 60):
    """Split final text into small chunks for a streaming-like UX."""
    for i in range(0, len(content), chunk_size):
        yield content[i:i + chunk_size]


def _window_history(rows: list) -> list[ChatMessage]:
    """Persisted conversation rows → LLM messages, newest-first within HISTORY_CHARS."""
    out: list[ChatMessage] = []
    used = 0
    for m in reversed(rows):
        if m.role == "tool":
            # Persisted tool trace — replay as assistant context (we don't
            # keep tool_call_ids, so a real 'tool' role would be malformed)
            content = "[Tool results from my earlier turn]\n" + m.content
            role = "assistant"
        else:
            content, role = m.content, m.role
        if out and used + len(content) > HISTORY_CHARS:
            break
        out.append(ChatMessage(role=role, content=content))
        used += len(content)
    out.reverse()
    while out and out[0].role != "user":  # never start mid-turn
        out.pop(0)
    return out


async def _persist_user_message(ai_conversation_id: int, text: str) -> Optional[int]:
    """Persist the user's turn. Skips an identical, just-written user message
    (clients that still append it themselves) so it is never doubled."""
    from datetime import datetime, timedelta
    async with AsyncSessionLocal() as db:
        last = await crud.get_ai_conversation_messages(db, ai_conversation_id, limit=1)
        if last and last[-1].role == "user" and last[-1].content == text \
                and last[-1].created_at and datetime.utcnow() - last[-1].created_at < timedelta(minutes=2):
            return last[-1].id
        msg = await crud.add_ai_conversation_message(db, ai_conversation_id, "user", text)
        return msg.id


async def _persist_turn(ai_conversation_id: Optional[int], trace: list[str], text: str) -> Optional[int]:
    """Persist tool trace (so follow-ups keep the evidence) then the assistant reply."""
    if not ai_conversation_id:
        return None
    async with AsyncSessionLocal() as db:
        if trace:
            await crud.add_ai_conversation_message(db, ai_conversation_id, "tool", "\n".join(trace))
        msg = await crud.add_ai_conversation_message(db, ai_conversation_id, "assistant", text)
        return msg.id


def _message_tokens(m: ChatMessage) -> int:
    n = estimate_tokens(m.content) + 4
    if m.tool_calls:
        n += estimate_tokens(json.dumps(m.tool_calls, ensure_ascii=False))
    return n


def _agent_budget(provider, system_content: str, tools: Optional[list]) -> int:
    """Prompt tokens left for history + this turn, from the provider's
    context window (num_ctx for Ollama) minus the answer reserve, a safety
    margin, the system prompt and the tool schemas."""
    budget = prompt_budget_tokens(getattr(provider, "context_tokens", None), AGENT_MAX_TOKENS)
    fixed = estimate_tokens(system_content) + (estimate_tokens(json.dumps(tools)) if tools else 0)
    return max(512, budget - fixed)


def _fit_to_budget(messages: list[ChatMessage], turn_start: int, budget: int) -> tuple[list[ChatMessage], int]:
    """Make messages[1:] fit ``budget`` tokens. Drops the oldest history first
    (whole turns, so the history still starts with a user message), then
    shrinks this turn's tool results, largest first. The system prompt, the
    current user message and tool_call/tool pairs are never removed."""
    msgs = list(messages)

    def total() -> int:
        return sum(_message_tokens(m) for m in msgs[1:])

    while total() > budget and turn_start > 1:
        del msgs[1]
        turn_start -= 1
        while turn_start > 1 and msgs[1].role != "user":
            del msgs[1]
            turn_start -= 1
    over = total() - budget
    if over > 0:
        tool_idx = sorted(
            (i for i in range(turn_start, len(msgs)) if msgs[i].role == "tool"),
            key=lambda i: -_message_tokens(msgs[i]),
        )
        for i in tool_idx:
            if over <= 0:
                break
            cur = estimate_tokens(msgs[i].content)
            keep = max(100, cur - over)
            if keep < cur:
                m = msgs[i]
                msgs[i] = ChatMessage(role=m.role, content=clip_to_tokens(m.content, keep),
                                      tool_call_id=m.tool_call_id)
                over -= cur - estimate_tokens(msgs[i].content)
    return msgs, turn_start


async def _run_agent(history: list[ChatMessage], ai_conversation_id: Optional[int] = None, user_message_id=None):
    """
    Agentic loop through the provider abstraction:
      1. provider.complete(tools=TOOLS)
      2. tool calls → execute them, yield events, loop
      3. otherwise emit the final text (chunked), persist the turn, yield done.
    LLM failures end the stream with a typed ``error`` event.
    """
    try:
        provider = get_provider("intelligence")
    except LLMUnavailable as exc:
        yield sse("error", {"message": error_message(exc), "kind": error_kind(exc)})
        return

    from backend.org_context import get_user_orgs_system_context
    system_content = SYSTEM_PROMPT
    org_ctx = get_user_orgs_system_context()
    if org_ctx:
        system_content += "\n\n" + org_ctx

    messages = [ChatMessage(role="system", content=system_content)] + history
    tools = TOOLS if provider.supports_tools else None
    budget = _agent_budget(provider, system_content, tools)
    # Index of the current user message: everything before it is droppable history.
    turn_start = len(messages) - 1
    trace: list[str] = []
    final_content: Optional[str] = None

    for _ in range(MAX_TOOL_ROUNDS):
        messages, turn_start = _fit_to_budget(messages, turn_start, budget)
        try:
            resp = await provider.complete(
                messages, max_tokens=AGENT_MAX_TOKENS, temperature=0.3, tools=tools,
            )
        except LLMError as exc:
            yield sse("error", {"message": error_message(exc), "kind": error_kind(exc)})
            return

        if not resp.tool_calls:
            # Emit this same response's text directly. (A separate streaming
            # call would be a different sample that could promise tool use it
            # can no longer perform.)
            final_content = resp.content or ""
            if not final_content.strip():
                final_content = (
                    "The tool results above show what I found — ask a follow-up to continue."
                    if trace
                    else "I couldn't produce a response — please try rephrasing."
                )
            break

        messages.append(ChatMessage(
            role="assistant",
            content=resp.content or "",
            tool_calls=[tc.to_openai() for tc in resp.tool_calls],
            provider_content=resp.provider_content,
        ))
        for tc in resp.tool_calls:
            yield sse("tool_call", {"name": tc.name, "args": tc.arguments})
            # Fresh short-lived session per tool call: holding one session
            # across the multi-round LLM loop would keep a SQLite read
            # transaction open for minutes, blocking every writer.
            async with AsyncSessionLocal() as tool_db:
                result = await execute_tool(tc.name, tc.arguments or {}, tool_db)
            if len(result) > TOOL_RESULT_CHARS:
                result = result[:TOOL_RESULT_CHARS] + "\n…(truncated)"
            # A single result may use at most a third of the prompt budget.
            result = clip_to_tokens(result, budget // 3)
            yield sse("tool_result", {"name": tc.name, "content": result[:500]})
            try:
                args_str = json.dumps(tc.arguments, ensure_ascii=False)[:150]
            except Exception:
                args_str = "{}"
            trace.append(f"{tc.name}({args_str}) → {result[:300]}")
            messages.append(ChatMessage(role="tool", content=result, tool_call_id=tc.id))

    if final_content is None:
        # Hit the per-message tool-round cap — end gracefully so the turn is
        # displayed and persisted rather than vanishing behind a bare error.
        final_content = (
            "I hit the per-message tool limit. The results gathered so far are shown above — "
            "ask a follow-up to continue from them."
        )

    for chunk in _emit_text_chunks(final_content):
        yield sse("text", {"content": chunk})
    assistant_id = None
    try:
        assistant_id = await _persist_turn(ai_conversation_id, trace, final_content)
    except Exception:
        logger.warning("Failed to persist assistant turn", exc_info=True)
    yield sse("done", {
        "ai_conversation_id": ai_conversation_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_id,
    })


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/intelligence/status")
async def intelligence_status():
    try:
        provider = get_provider("intelligence")
    except LLMUnavailable:
        return {"configured": False, "model": None}
    return {"configured": True, "model": getattr(provider, "model", None)}


@router.get("/intelligence/mentions")
async def intelligence_mentions(
    days: int = 7,
    group_name: str = "",
    tag_name: str = "",
    channel: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Return up to 50 mention groups: messages in group/channel conversations
    that mention the user (on any signed-in account).
    Each group includes 2 context messages before and 1 after.
    Batched: a fixed number of queries regardless of the number of mentions.
    """
    mention_rows = await crud.get_mentions(
        db,
        limit=50,
        days=days,
        group_name_filter=group_name,
        tag_name_filter=tag_name,
        channel=channel,
    )
    # after=2: one shown as context, the second tells us whether messages were
    # omitted between the context and the user's reply.
    contexts = await crud.get_mention_contexts(db, [msg for msg, _ in mention_rows], before=2, after=2)
    tags = await crud.get_tags_for_conversations(db, [conv.id for _, conv in mention_rows])

    groups = []
    for msg, conv in mention_rows:
        ctx = contexts[msg.id]
        after = ctx["after"]
        reply = ctx["reply"]
        reply_has_gap = False
        if reply is not None and after:
            shown_last = after[0].seq
            reply_has_gap = reply.seq > shown_last and len(after) > 1 and after[1].seq < reply.seq
        groups.append({
            "mention_msg": message_out(msg),
            "context_before": [message_out(m) for m in ctx["before"]],
            "context_after": [message_out(m) for m in after[:1]],
            "replied": reply is not None,
            "user_reply_msg": message_out(reply) if reply is not None else None,
            "reply_has_gap": reply_has_gap,
            "conversation": {
                **conversation_json(conv, with_last_message=False),
                "tags": [{"id": t.id, "name": t.name, "color": t.color} for t in tags.get(conv.id, [])],
            },
        })
    return groups


@router.post("/intelligence/chat")
async def intelligence_chat(
    req: IntelligenceChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Streaming agentic chat endpoint (SSE: tool_call, tool_result, text, done, error).

    Preferred usage: {ai_conversation_id, message}. The backend is the single
    writer of the turn: it persists the user message now, and the assistant
    reply (+ tool trace) when the turn completes. Clients must NOT append
    either message themselves.

    Legacy fallback: {messages: [...]} — stateless, nothing persisted.
    """
    user_message_id = None
    if req.ai_conversation_id is not None and req.message is not None:
        user_message_id = await _persist_user_message(req.ai_conversation_id, req.message)
        prior = await crud.get_ai_conversation_messages(db, req.ai_conversation_id, limit=HISTORY_MESSAGES)
        history = _window_history(prior)
        if not history or history[-1].role != "user" or history[-1].content != req.message:
            history.append(ChatMessage(role="user", content=req.message))
    elif req.messages:
        history = [ChatMessage(role=m.role, content=m.content) for m in req.messages]
    else:
        raise HTTPException(status_code=400, detail="Provide ai_conversation_id + message")

    # Release the request session's read transaction before streaming begins;
    # the agent loop opens its own short-lived sessions per tool call.
    await db.commit()
    return sse_response(_run_agent(history, req.ai_conversation_id, user_message_id))


# ---------------------------------------------------------------------------
# Briefing
# ---------------------------------------------------------------------------

@router.get("/intelligence/briefing")
async def intelligence_briefing(days: int = 1, attach: bool = False, replace: bool = False):
    """
    Start a briefing (or attach to the one already running) and stream its
    events. The run continues if the client disconnects; reconnecting with
    the same request replays all events so far. See the API contract for the
    event shapes (start, progress, thinking, chat_done, chat_error, done,
    error, cancelled).
    """
    if attach:
        # Re-attach only: never start a run (avoids a duplicate run when the
        # job finished between /briefing/status and this request).
        running = get_event_job(BRIEFING_JOB_KEY)
        if running is None:
            async def idle():
                yield sse("idle", {})
            return sse_response(idle())
        return sse_response(running.sse(attached=True))
    days = max(1, min(int(days), 30))
    if replace:
        # Cancel whatever is running (any window), wait for it, start this one.
        job, replaced = await replace_briefing_job(days)
        return sse_response(job.sse(attached=False, extra={"replaced_job_id": replaced}))
    running = get_event_job(BRIEFING_JOB_KEY)
    if running is not None and running.meta.get("days") != days:
        # Never silently hand a 7-day request the events of a 1-day run.
        snap = running.snapshot()
        return JSONResponse(status_code=409, content={
            "code": "briefing_running",
            "detail": f"A {snap.get('days')}-day briefing is already running.",
            "running_days": snap.get("days"),
            "requested_days": days,
            "job_id": snap["job_id"],
            "trigger": snap.get("trigger"),
            "progress": snap["progress"],
        })
    job, started = start_briefing_job(days, trigger="manual")
    return sse_response(job.sse(attached=not started))


@router.get("/intelligence/briefing/status")
async def briefing_status():
    job = get_event_job(BRIEFING_JOB_KEY)
    if job is None:
        return {"running": False}
    return job.snapshot()


@router.post("/intelligence/briefing/cancel")
async def cancel_briefing():
    job = get_event_job(BRIEFING_JOB_KEY)
    if job is None or job.task is None:
        return {"cancelled": False}
    job.task.cancel()
    return {"cancelled": True, "job_id": job.id}


@router.get("/intelligence/briefing/history")
async def get_briefing_history(db: AsyncSession = Depends(get_db)):
    """All briefing runs with their entries, newest first (2 queries).
    A run being generated right now has status 'running'."""
    runs = await crud.list_briefing_runs(db)
    entries = await crud.get_entries_for_runs(db, [r.id for r in runs])
    return [run_dict(run, entries.get(run.id, [])) for run in runs]


@router.post("/intelligence/briefing/history/{run_id}/entries/{entry_id}/regenerate")
async def regenerate_briefing_entry(run_id: int, entry_id: int):
    """
    Regenerate one entry over its run's original window
    [generated_at - days, generated_at]. Streams start / thinking / done /
    error. The entry is only overwritten on success; closing the stream
    cancels the regeneration.
    """
    job, started = start_regenerate_job(run_id, entry_id)
    return sse_response(job.sse(attached=not started))


@router.delete("/intelligence/briefing/history/{run_id}")
async def delete_briefing_run(run_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a past briefing run and all its entries."""
    job = get_event_job(BRIEFING_JOB_KEY)
    if job is not None and job.progress.get("run_id") == run_id:
        raise HTTPException(status_code=409, detail="This briefing is still being generated")
    success = await crud.delete_briefing_run(db, run_id)
    if not success:
        raise HTTPException(status_code=404, detail="Briefing run not found")
    return {"success": True}
