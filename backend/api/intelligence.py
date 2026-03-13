"""
Intelligence endpoint — agentic LLM chat with tool access to the local DB.
"""
import json
import logging
import re
import asyncio
from typing import Any, AsyncGenerator, Optional, Tuple, List

logger = logging.getLogger(__name__)

import httpx
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db, AsyncSessionLocal
from backend.db import crud
from backend.db.models import BriefingRun, BriefingEntry
from backend.llm.config import load_registry_config
from backend.llm.briefing_prompts import (
    DEFAULT_BRIEFING_CHAT_PROMPT,
    DEFAULT_IMPORTANT_CONTACT_PROMPT,
    DEFAULT_TRACKED_CHAT_PROMPT,
)
from backend.api.settings import (
    get_briefing_max_tokens,
    get_briefing_prompt_override,
    get_briefing_prompt_overrides,
)

router = APIRouter()

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling schema)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_chat_messages",
            "description": (
                "Retrieve recent messages from a specific Telegram chat. "
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
            "description": "Search for chats whose title/name contains a keyword.",
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
    "You are an intelligent assistant for Mingly, a personal Telegram relationship manager. "
    "You have access to tools that let you search the user's Telegram chats and messages. "
    "Use your tools to answer questions accurately — look up information before responding. "
    "Be concise, helpful, and specific. When listing chats or messages, keep it brief and relevant."
)

# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _qname(name, fallback: str = "") -> str:
    """Wrap a chat/tag name in backticks so LLMs preserve angle-bracket chars verbatim."""
    return f"`{name or fallback}`"


async def execute_tool(name: str, args: dict, db: AsyncSession) -> str:
    try:
        if name == "get_chat_messages":
            chat_name = args.get("chat_name", "")
            limit = min(int(args.get("limit", 30)), 100)
            chat = await crud.get_chat_by_name(db, chat_name)
            if not chat:
                return f"No chat found matching '{chat_name}'."
            rows = await crud.get_messages(db, chat.telegram_id, limit=limit)
            if not rows:
                return f"No messages found in {_qname(chat.name)}."
            lines = [f"[{m.date.strftime('%Y-%m-%d %H:%M')}] {m.sender_name or 'Unknown'}: {m.text or ''}"
                     for m, *_ in reversed(rows) if m.text]
            return f"Last {len(lines)} messages in {_qname(chat.name)}:\n" + "\n".join(lines)

        elif name == "find_chats_by_tag":
            tag_name = args.get("tag_name", "")
            chats = await crud.get_chats_by_tag_name(db, tag_name)
            if not chats:
                return f"No chats found with tag matching '{tag_name}'."
            names = [_qname(c.name, str(c.telegram_id)) for c in chats]
            return f"Chats tagged '{tag_name}':\n" + "\n".join(f"- {n}" for n in names)

        elif name == "search_messages":
            keyword = args.get("keyword", "")
            limit = min(int(args.get("limit", 20)), 50)
            rows = await crud.search_messages_by_keyword(db, keyword, limit=limit)
            if not rows:
                return f"No messages found containing '{keyword}'."
            lines = []
            for msg, chat in rows:
                if msg.text:
                    ts = msg.date.strftime("%Y-%m-%d %H:%M")
                    sender = msg.sender_name or "Unknown"
                    lines.append(f"[{ts}] {_qname(chat.name, str(chat.telegram_id))} — {sender}: {msg.text[:200]}")
            return f"Messages containing '{keyword}':\n" + "\n".join(lines)

        elif name == "search_chats_by_title":
            keyword = args.get("keyword", "")
            chats = await crud.search_chats_by_title(db, keyword)
            if not chats:
                return f"No chats found with title containing '{keyword}'."
            lines = [f"{_qname(c.name)} ({c.type})" for c in chats]
            return f"Chats matching '{keyword}':\n" + "\n".join(lines)

        elif name == "tag_chat":
            chat_name = args.get("chat_name", "")
            tag_name = args.get("tag_name", "")
            # Use a fresh session for writes to avoid streaming session lifecycle issues
            async with AsyncSessionLocal() as write_db:
                chat = await crud.get_chat_by_name(write_db, chat_name)
                if not chat:
                    return f"No chat found matching '{chat_name}'."
                all_tags = await crud.get_all_tags(write_db)
                tag = next((t for t in all_tags if t.name.lower() == tag_name.lower()), None)
                if not tag:
                    tag = await crud.create_tag(write_db, name=tag_name)
                existing = await crud.get_tags_for_chat(write_db, chat.telegram_id)
                if any(t.id == tag.id for t in existing):
                    return f"{_qname(chat.name)} already has the tag '{tag.name}'."
                await crud.add_tag_to_chat(write_db, chat.telegram_id, tag.id)
                return f"Tagged {_qname(chat.name)} with '{tag.name}'."

        elif name == "find_chats_by_category":
            category = args.get("category", "")
            if category == "tracked":
                chats = await crud.get_tracked_chats(db)
            elif category == "briefing":
                chats = await crud.get_briefing_chats(db)
            else:
                return f"Unknown category '{category}'. Use 'tracked' or 'briefing'."
            if not chats:
                return f"No chats in category '{category}'."
            names = [_qname(c.name, str(c.telegram_id)) for c in chats]
            return f"Chats in '{category}':\n" + "\n".join(f"- {n}" for n in names)

        elif name == "find_chats_by_member_count":
            count = int(args.get("count", 0))
            operator = args.get("operator", "more_than")
            chats = await crud.find_chats_by_member_count(db, count=count, operator=operator)
            if not chats:
                op_label = {"more_than": "more than", "exactly": "exactly", "less_than": "less than"}.get(operator, operator)
                return f"No chats found with {op_label} {count} members."
            op_label = {"more_than": "more than", "exactly": "exactly", "less_than": "fewer than"}.get(operator, operator)
            lines = [f"- {_qname(c.name, str(c.telegram_id))} ({c.members_count} members)" for c in chats]
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
    # Preferred: send conversation_id + the new user message text only.
    # Backend loads full history from DB to build LLM context.
    conversation_id: Optional[int] = None
    message: Optional[str] = None
    # Legacy fallback: explicit messages list (kept for compatibility)
    messages: Optional[list[IntelligenceMessage]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _get_llm_config():
    """Return (base_url, model) for the orchestrator or (None, None) if not configured."""
    cfg = load_registry_config()
    if not cfg.orchestrator_name:
        return None, None
    inst = next((i for i in cfg.instances if i.name == cfg.orchestrator_name), None)
    if not inst:
        return None, None
    return inst.base_url.rstrip("/"), inst.model


# ---------------------------------------------------------------------------
# Streaming agentic loop
# ---------------------------------------------------------------------------

async def _run_agent(messages_in: list[dict], db: AsyncSession):
    """
    Agentic loop:
      1. Call LLM with tools (non-streaming).
      2. If response contains tool_calls → execute them, yield events, loop.
      3. Otherwise stream the final text and yield done.
    """
    base_url, model = _get_llm_config()
    if not base_url:
        yield _sse("error", {"message": "No LLM configured. Set up an AI model in Settings."})
        return

    messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages_in

    async with httpx.AsyncClient(timeout=120.0) as client:
        for _ in range(8):  # max tool-call rounds
            # ── Non-streaming call to check for tool calls ──
            try:
                resp = await client.post(
                    f"{base_url}/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "tools": TOOLS,
                        "tool_choice": "auto",
                        "max_tokens": 2048,
                        "temperature": 0.3,
                        "stream": False,
                    },
                )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                yield _sse("error", {"message": f"LLM unreachable: {exc}"})
                return
            if resp.status_code != 200:
                yield _sse("error", {"message": f"LLM error: {resp.text[:200]}"})
                return

            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls:
                # No more tool calls — stream the final answer
                final_content = msg.get("content") or ""
                # Stream word by word for a nice effect, or just yield as one chunk
                # Use streaming call for nicer UX
                async with client.stream(
                    "POST",
                    f"{base_url}/chat/completions",
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": 2048,
                        "temperature": 0.3,
                        "stream": True,
                    },
                ) as stream_resp:
                    if stream_resp.status_code != 200:
                        # Fallback: send non-streamed content
                        yield _sse("text", {"content": final_content})
                    else:
                        async for line in stream_resp.aiter_lines():
                            if line.startswith("data: ") and line != "data: [DONE]":
                                try:
                                    chunk = json.loads(line[6:])
                                    delta = chunk["choices"][0]["delta"].get("content", "")
                                    if delta:
                                        yield _sse("text", {"content": delta})
                                except Exception:
                                    pass
                yield _sse("done", {})
                return

            # ── Execute tool calls ──
            # Append assistant message with tool_calls
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": tool_calls})

            for tc in tool_calls:
                fn = tc["function"]
                tool_name = fn["name"]
                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    tool_args = {}

                yield _sse("tool_call", {"name": tool_name, "args": tool_args})

                result = await execute_tool(tool_name, tool_args, db)

                yield _sse("tool_result", {"name": tool_name, "content": result[:500]})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

    yield _sse("error", {"message": "Max tool-call rounds exceeded."})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/intelligence/status")
async def intelligence_status():
    base_url, model = _get_llm_config()
    return {"configured": base_url is not None, "model": model}


@router.get("/intelligence/mentions")
async def intelligence_mentions(
    days: int = 7,
    group_name: str = "",
    tag_name: str = "",
    db: AsyncSession = Depends(get_db),
):
    """
    Return up to 50 mention groups: messages in group/channel chats where
    the authenticated user's username appears in the text.
    Each group includes 2 context messages before and 1 after.

    Query params:
      - days: time window (7, 14, 30), default 7
      - group_name: filter by chat name substring
      - tag_name: filter to chats with this tag
    """
    from backend.telegram.client import get_client

    client = get_client()
    if client is None:
        return []

    try:
        me = await client.get_me()
    except Exception:
        return []

    username = me.username if me else None
    if not username:
        return []

    mention_rows = await crud.get_mentions(
        db,
        username,
        limit=50,
        days=days,
        group_name_filter=group_name,
        tag_name_filter=tag_name,
    )

    def _serialize_msg(m: Any) -> dict:
        media_path = getattr(m, "media_path", None)
        return {
            "id": m.id,
            "telegram_id": m.telegram_id,
            "chat_id": m.chat_id,
            "sender_id": m.sender_id,
            "sender_name": m.sender_name,
            "text": m.text,
            "date": m.date.isoformat() if m.date else None,
            "is_outgoing": m.is_outgoing,
            "media_type": m.media_type,
            "media_url": f"/api/media/{media_path}" if media_path else None,
            "my_reaction": getattr(m, "my_reaction", None),
        }

    groups = []
    for msg, chat in mention_rows:
        context = await crud.get_messages_around(
            db,
            chat_id=msg.chat_id,
            telegram_id=msg.telegram_id,
            before=2,
            after=1,
        )

        # Fetch tags for the chat
        chat_tags = await crud.get_tags_for_chat(db, chat.telegram_id)

        # Check if user replied after the mention; fetch the actual reply message
        replied = False
        user_reply_msg = None
        reply_has_gap = False
        reply_msg = await crud.get_first_reply_after(db, msg.chat_id, msg.telegram_id)
        if reply_msg:
            replied = True
            user_reply_msg = _serialize_msg(reply_msg)
            # Determine if there are messages between the last context_after message
            # (or the mention itself) and the reply, indicating omitted messages
            last_context_id = context["after"][-1].telegram_id if context["after"] else msg.telegram_id
            gap_count = await crud.count_messages_between(
                db, msg.chat_id, last_context_id, reply_msg.telegram_id
            )
            reply_has_gap = gap_count > 0

        groups.append({
            "mention_msg": _serialize_msg(msg),
            "context_before": [_serialize_msg(m) for m in context["before"]],
            "context_after": [_serialize_msg(m) for m in context["after"]],
            "replied": replied,
            "user_reply_msg": user_reply_msg,
            "reply_has_gap": reply_has_gap,
            "chat": {
                "id": chat.telegram_id,
                "telegram_id": chat.telegram_id,
                "name": chat.name,
                "type": chat.type,
                "is_tracked": chat.is_tracked,
                "tags": [{"id": t.id, "name": t.name, "color": t.color} for t in chat_tags],
            },
        })

    return groups


@router.post("/intelligence/chat")
async def intelligence_chat(
    req: IntelligenceChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Streaming agentic chat endpoint.

    Preferred usage: send {conversation_id, message} — the backend loads the
    full persisted conversation history from DB and appends the new user message
    as context, ensuring isolation between conversations.

    Legacy fallback: send {messages: [...]} for backwards compatibility.
    """
    if req.conversation_id is not None and req.message is not None:
        # Load persisted history from DB to build context
        prior_msgs = await crud.get_conversation_messages(db, req.conversation_id)
        messages_in = [
            {"role": m.role, "content": m.content}
            for m in prior_msgs
        ]
        # Append the new user message (frontend already persisted it)
        messages_in.append({"role": "user", "content": req.message})
    elif req.messages:
        # Legacy: full messages list provided by caller
        messages_in = [{"role": m.role, "content": m.content} for m in req.messages]
    else:
        messages_in = []

    async def generate():
        async for chunk in _run_agent(messages_in, db):
            yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Briefing / Summary endpoint
# ---------------------------------------------------------------------------

def _build_message_map(messages_text: str) -> Tuple[dict, set]:
    """
    Parse the messages_text sent to the LLM and return:
      pos_to_tid  – {1-indexed-position: telegram_id}
      valid_tids  – set of all telegram_ids present in the context
    """
    pos_to_tid: dict = {}
    valid_tids: set = set()
    for line in messages_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r'\[id:(\d+)\]', line)
        if m:
            tid = int(m.group(1))
            pos = len(pos_to_tid) + 1  # 1-indexed
            pos_to_tid[pos] = tid
            valid_tids.add(tid)
    return pos_to_tid, valid_tids


def _resolve_source_ids(
    raw_ids: List[int], pos_to_tid: dict, valid_tids: set
) -> List[int]:
    """
    Remap LLM-returned source IDs to actual telegram IDs:
      - If the value is already a valid telegram_id in the context → keep it
      - If the value looks like a 1-indexed position → map to telegram_id
      - Otherwise discard
    Deduplicates while preserving order.
    """
    resolved: List[int] = []
    seen: set = set()
    for sid in raw_ids:
        if sid in valid_tids:
            result = sid  # already a real telegram_id
        elif sid in pos_to_tid:
            result = pos_to_tid[sid]  # position number → telegram_id
        else:
            continue  # neither valid telegram_id nor known position
        if result not in seen:
            seen.add(result)
            resolved.append(result)
    return resolved


def _extract_ids(ids_raw: list) -> List[int]:
    """Normalise a raw source_ids list to plain ints, capped at 20."""
    valid: List[int] = []
    seen: set = set()
    for x in ids_raw:
        s = str(x).strip()
        if s.startswith("id:"):
            s = s[3:]
        if s.lstrip("-").isdigit():
            v = int(s)
            if v not in seen:
                seen.add(v)
                valid.append(v)
        if len(valid) >= 20:
            break
    return valid


def _try_json_parse(text: str):
    """Attempt json.loads; return parsed dict or None."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    return None


def _renumber_citations(summary: str, source_ids: List[int]) -> Tuple[str, List[int]]:
    """
    Renumber inline [n] citation markers in summary to be strictly sequential [1],[2],[3]...
    starting from the first citation found, regardless of what numbers the LLM used.

    source_ids must already be ordered by first-appearance of the original citation numbers
    (i.e. source_ids[0] = telegram ID for whichever [n] appears first in the text).

    Returns (renumbered_summary, trimmed_source_ids).
    """
    # Collect unique citation numbers in order of first appearance
    mapping: dict = {}  # original_n -> sequential_counter
    for m in re.finditer(r'\[(\d+)\]', summary):
        n = int(m.group(1))
        if n not in mapping:
            mapping[n] = len(mapping) + 1  # 1-based sequential counter

    if not mapping:
        return summary, source_ids

    def _replace(m: re.Match) -> str:
        return f'[{mapping[int(m.group(1))]}]'

    new_summary = re.sub(r'\[(\d+)\]', _replace, summary)
    # Trim source_ids to however many unique citations were found
    new_source_ids = source_ids[:len(mapping)]
    return new_summary, new_source_ids


def _clean_summary(text: str) -> str:
    """Strip markdown formatting and URLs from LLM summary output."""
    # Remove URLs (http/https)
    text = re.sub(r'https?://\S+', '', text)
    # Remove bold/italic/strikethrough markers (*bold*, **bold**, _italic_, ~~strike~~)
    text = re.sub(r'\*{1,3}([^*\n]+?)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,2}([^_\n]+?)_{1,2}', r'\1', text)
    text = re.sub(r'~~([^~\n]+?)~~', r'\1', text)
    # Remove inline code backticks
    text = re.sub(r'`([^`\n]+?)`', r'\1', text)
    # Collapse extra spaces left by removals
    text = re.sub(r'  +', ' ', text)
    # Clean up trailing spaces on each line
    text = '\n'.join(line.rstrip() for line in text.splitlines())
    return text.strip()


def _parse_summary_response(raw: str) -> Tuple[str, List[int]]:
    """
    Parse a summary response that may be plain text or JSON with summary + source_ids.
    Returns (summary_text, list_of_telegram_ids).
    """
    raw = raw.strip()

    # Strip markdown code fences that some LLMs add (```json ... ``` or ``` ... ```)
    if raw.startswith("```"):
        raw = re.sub(r'^```(?:json)?\s*\n?', '', raw)
        raw = re.sub(r'\n?```\s*$', '', raw)
        raw = raw.strip()

    # Attempt 1: direct parse
    obj = _try_json_parse(raw)

    # Attempt 2: LLM sometimes emits literal newlines inside JSON strings
    if obj is None:
        sanitized = re.sub(r'(?<!\\)\n', r'\\n', raw)
        obj = _try_json_parse(sanitized)

    # Attempt 3: extract the JSON object via regex (handles trailing garbage)
    if obj is None:
        m = re.search(r'\{.*?\}', raw, re.DOTALL)
        if m:
            obj = _try_json_parse(m.group(0))
        # Attempt 3b: try from first { to last }
        if obj is None:
            start = raw.find('{')
            end = raw.rfind('}')
            if start != -1 and end != -1 and end > start:
                obj = _try_json_parse(raw[start:end + 1])
                if obj is None:
                    sanitized2 = re.sub(r'(?<!\\)\n', r'\\n', raw[start:end + 1])
                    obj = _try_json_parse(sanitized2)

    def _coerce_summary_text(value: Any) -> str:
        """Normalise summary field to readable text (handles list/object payloads)."""
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            lines: list[str] = []
            for item in value:
                txt = str(item).strip()
                if not txt:
                    continue
                # Keep existing bullets/numbering, otherwise make it a bullet line.
                if re.match(r'^(?:[•\-\*\d]+[\.\)]?\s+)', txt):
                    lines.append(txt)
                else:
                    lines.append(f"• {txt}")
            return "\n".join(lines).strip()
        if isinstance(value, dict):
            # Some models return nested shape: {"text": "..."} or {"summary": "..."}.
            for k in ("summary", "text", "content"):
                if k in value:
                    return _coerce_summary_text(value.get(k))
            return json.dumps(value, ensure_ascii=False)
        return str(value).strip()

    if obj is not None:
        summary = _coerce_summary_text(obj.get("summary", ""))
        ids_raw = obj.get("source_ids", [])
        if isinstance(ids_raw, list):
            source_ids = _extract_ids(ids_raw)
            # Normalise citation numbering to be strictly sequential [1],[2],[3]...
            summary, source_ids = _renumber_citations(summary, source_ids)
            return _clean_summary(summary), source_ids

    # Plain text fallback
    return _clean_summary(raw), []


async def _summarize_chat(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    chat_name: str,
    chat_category: str,
    messages_text: str,
    max_tokens: int = 4000,
    cancel: Optional[asyncio.Event] = None,
) -> AsyncGenerator[Tuple[str, Any], None]:
    """Async generator: yields ('token'|'reasoning', data) then ('result', (summary, ids))."""
    prompt_override = get_briefing_prompt_override()
    if prompt_override:
        system = prompt_override
    else:
        category_overrides = get_briefing_prompt_overrides()
        if chat_category == "tracked":
            system = category_overrides.get("tracked") or DEFAULT_TRACKED_CHAT_PROMPT
        else:
            system = category_overrides.get("briefing") or DEFAULT_BRIEFING_CHAT_PROMPT

    user_msg = f"Chat: {chat_name}\n\nMessages:\n{messages_text}"
    content_acc = ""
    reasoning_acc = ""
    token_count = 0

    try:
        async with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
                "stream": True,
            },
            timeout=180.0,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                logger.error("_summarize_chat HTTP %s for %s: %.300s", resp.status_code, chat_name, body)
                yield ("result", ("", []))
                return

            async for line in resp.aiter_lines():
                if cancel is not None and cancel.is_set():
                    logger.info("_summarize_chat cancelled for %s", chat_name)
                    yield ("result", ("", []))
                    return
                if not line.startswith("data: "):
                    continue
                chunk = line[6:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                    delta = data["choices"][0]["delta"]
                    content_delta = delta.get("content") or ""
                    reasoning_delta = delta.get("reasoning") or delta.get("reasoning_content") or ""
                    content_acc += content_delta
                    reasoning_acc += reasoning_delta
                    if reasoning_delta:
                        yield ("reasoning", reasoning_delta)
                    token_count += 1
                    if token_count % 10 == 0:
                        yield ("token", token_count)
                except Exception:
                    pass

    except Exception as exc:
        logger.exception("_summarize_chat failed for chat=%s: %s", chat_name, exc)
        yield ("result", ("", []))
        return

    # Emit final token count
    if token_count > 0:
        yield ("token", token_count)

    # Parse accumulated response
    raw = content_acc.strip()
    if not raw and reasoning_acc:
        # Reasoning model put answer in thinking field — extract last JSON block
        json_match = None
        for m in re.finditer(r'\{[^{}]*"summary"[^{}]*\}', reasoning_acc, re.DOTALL):
            json_match = m
        if json_match:
            raw = json_match.group(0)
            logger.info("_summarize_chat: extracted JSON from reasoning for %s", chat_name)

    if not raw:
        logger.warning("_summarize_chat empty content for chat=%s, tokens=%d", chat_name, token_count)
        yield ("result", ("", []))
        return

    logger.debug("_summarize_chat raw for %s: %.200s", chat_name, raw)
    summary, source_ids = _parse_summary_response(raw)
    pos_to_tid, valid_tids = _build_message_map(messages_text)
    source_ids = _resolve_source_ids(source_ids, pos_to_tid, valid_tids)
    yield ("result", (summary, source_ids))


async def _summarize_important_contact(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    contact_name: str,
    messages_text: str,
    max_tokens: int = 4000,
    cancel: Optional[asyncio.Event] = None,
) -> AsyncGenerator[Tuple[str, Any], None]:
    """Async generator: yields ('token'|'reasoning', data) then ('result', (summary, ids))."""
    system = DEFAULT_IMPORTANT_CONTACT_PROMPT
    user_msg = f"Person: {contact_name}\n\nMessages:\n{messages_text}"
    content_acc = ""
    reasoning_acc = ""
    token_count = 0

    try:
        async with client.stream(
            "POST",
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
                "stream": True,
            },
            timeout=180.0,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                logger.error("_summarize_important_contact HTTP %s for %s: %.300s", resp.status_code, contact_name, body)
                yield ("result", ("", []))
                return

            async for line in resp.aiter_lines():
                if cancel is not None and cancel.is_set():
                    logger.info("_summarize_important_contact cancelled for %s", contact_name)
                    yield ("result", ("", []))
                    return
                if not line.startswith("data: "):
                    continue
                chunk = line[6:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    data = json.loads(chunk)
                    delta = data["choices"][0]["delta"]
                    content_delta = delta.get("content") or ""
                    reasoning_delta = delta.get("reasoning") or delta.get("reasoning_content") or ""
                    content_acc += content_delta
                    reasoning_acc += reasoning_delta
                    if reasoning_delta:
                        yield ("reasoning", reasoning_delta)
                    token_count += 1
                    if token_count % 10 == 0:
                        yield ("token", token_count)
                except Exception:
                    pass

    except Exception as exc:
        logger.exception("_summarize_important_contact failed for contact=%s: %s", contact_name, exc)
        yield ("result", ("", []))
        return

    if token_count > 0:
        yield ("token", token_count)

    raw = content_acc.strip()
    if not raw and reasoning_acc:
        json_match = None
        for m in re.finditer(r'\{[^{}]*"summary"[^{}]*\}', reasoning_acc, re.DOTALL):
            json_match = m
        if json_match:
            raw = json_match.group(0)
            logger.info("_summarize_important_contact: extracted JSON from reasoning for %s", contact_name)

    if not raw:
        logger.warning("_summarize_important_contact empty content for contact=%s, tokens=%d", contact_name, token_count)
        yield ("result", ("", []))
        return

    logger.debug("_summarize_important_contact raw for %s: %.200s", contact_name, raw)
    summary, source_ids = _parse_summary_response(raw)
    pos_to_tid, valid_tids = _build_message_map(messages_text)
    source_ids = _resolve_source_ids(source_ids, pos_to_tid, valid_tids)
    yield ("result", (summary, source_ids))


@router.get("/intelligence/briefing")
async def intelligence_briefing(
    request: Request,
    days: int = 1,
    db: AsyncSession = Depends(get_db),
):
    """
    Generate summaries for all tracked and briefing chats.
    Streams SSE events:
      - start: {total: N}
      - progress: {current: N, chat_name: str}
      - chat_done: {chat_id, chat_name, category, summary}
      - done: {run_id}
      - error: {message}
    """
    from datetime import datetime, timedelta

    base_url, model = _get_llm_config()
    if not base_url:
        async def _err():
            yield _sse("error", {"message": "No LLM configured. Set up an AI model in Settings."})
        return StreamingResponse(_err(), media_type="text/event-stream")

    since = datetime.utcnow() - timedelta(days=days)

    async def generate():
        cancel = asyncio.Event()

        async def _watch_disconnect():
            while not cancel.is_set():
                if await request.is_disconnected():
                    cancel.set()
                    return
                await asyncio.sleep(0.3)

        watcher = asyncio.create_task(_watch_disconnect())

        try:
            tracked = await crud.get_tracked_chats(db)
            briefing = await crud.get_briefing_chats(db)
            all_chats = [("tracked", c) for c in tracked] + [("briefing", c) for c in briefing]
            important_contacts = await crud.get_important_contacts(db)

            yield _sse("start", {"total": len(all_chats) + len(important_contacts)})

            collected: list[dict] = []

            # --- Chat summaries ---
            async with httpx.AsyncClient(timeout=180.0) as client:
                for idx, (category, chat) in enumerate(all_chats):
                    if cancel.is_set():
                        break
                    yield _sse("progress", {"current": idx + 1, "chat_name": chat.name})

                    msgs = await crud.get_messages_since(db, chat.telegram_id, since, limit=200)
                    if not msgs:
                        entry = {
                            "chat_id": chat.telegram_id,
                            "chat_name": chat.name,
                            "category": category,
                            "summary": "No messages in this period.",
                            "sources": [],
                        }
                        collected.append(entry)
                        yield _sse("chat_done", entry)
                        continue

                    lines = []
                    for row in msgs:
                        m, reply_sender, reply_text = row[0], row[1], row[2]
                        if m.text:
                            sender = "You" if m.is_outgoing else (m.sender_name or "Unknown")
                            if reply_sender and reply_text:
                                snippet = reply_text[:60].rstrip()
                                lines.append(f'{sender} [↩ {reply_sender}: "{snippet}"]: {m.text[:300]}')
                            else:
                                lines.append(f"{sender}: {m.text[:300]}")

                    if not lines:
                        entry = {
                            "chat_id": chat.telegram_id,
                            "chat_name": chat.name,
                            "category": category,
                            "summary": "No text messages in this period.",
                            "sources": [],
                        }
                        collected.append(entry)
                        yield _sse("chat_done", entry)
                        continue

                    messages_text = "\n".join(lines[-150:])
                    summary, source_ids = "", []
                    async for event_type, data in _summarize_chat(
                        client, base_url, model, chat.name, category, messages_text,
                        max_tokens=get_briefing_max_tokens(), cancel=cancel,
                    ):
                        if event_type == "token":
                            yield _sse("thinking", {"chat_name": chat.name, "tokens": data})
                        elif event_type == "reasoning":
                            yield _sse("thinking", {"chat_name": chat.name, "reasoning": data})
                        else:
                            summary, source_ids = data
                    if cancel.is_set():
                        break
                    if not summary:
                        summary = "Summary unavailable."

                    sources = [{"chat_id": chat.telegram_id, "telegram_id": tid} for tid in source_ids]
                    entry = {
                        "chat_id": chat.telegram_id,
                        "chat_name": chat.name,
                        "category": category,
                        "summary": summary,
                        "sources": sources,
                    }
                    collected.append(entry)
                    yield _sse("chat_done", entry)

            # --- Important contacts ---
            base_idx = len(all_chats)
            async with httpx.AsyncClient(timeout=180.0) as ic_client:
                for idx, contact in enumerate(important_contacts):
                    if cancel.is_set():
                        break
                    contact_name = (
                        f"{contact.first_name or ''} {contact.last_name or ''}".strip()
                        or contact.username
                        or str(contact.telegram_id)
                    )
                    yield _sse("progress", {"current": base_idx + idx + 1, "chat_name": contact_name})

                    rows = await crud.get_messages_from_sender_since(
                        db, contact.telegram_id, since, limit=100
                    )
                    if not rows:
                        entry = {
                            "chat_id": contact.telegram_id,
                            "chat_name": contact_name,
                            "category": "important_contact",
                            "summary": "No messages in this period.",
                            "sources": [],
                        }
                        collected.append(entry)
                        yield _sse("chat_done", entry)
                        continue

                    lines = []
                    msg_chat_map: dict[int, int] = {}
                    for row in rows:
                        msg, chat, reply_sender, reply_text = row[0], row[1], row[2], row[3]
                        if msg.text:
                            chat_label = chat.name or str(chat.telegram_id)
                            if reply_sender and reply_text:
                                snippet = reply_text[:60].rstrip()
                                lines.append(f'[{chat_label}] [↩ {reply_sender}: "{snippet}"]: {msg.text[:300]}')
                            else:
                                lines.append(f"[{chat_label}] {msg.text[:300]}")
                            msg_chat_map[msg.telegram_id] = chat.telegram_id

                    if not lines:
                        entry = {
                            "chat_id": contact.telegram_id,
                            "chat_name": contact_name,
                            "category": "important_contact",
                            "summary": "No text messages in this period.",
                            "sources": [],
                        }
                        collected.append(entry)
                        yield _sse("chat_done", entry)
                        continue

                    messages_text = "\n".join(lines[-80:])
                    summary, source_ids = "", []
                    async for event_type, data in _summarize_important_contact(
                        ic_client, base_url, model, contact_name, messages_text,
                        max_tokens=get_briefing_max_tokens(), cancel=cancel,
                    ):
                        if event_type == "token":
                            yield _sse("thinking", {"chat_name": contact_name, "tokens": data})
                        elif event_type == "reasoning":
                            yield _sse("thinking", {"chat_name": contact_name, "reasoning": data})
                        else:
                            summary, source_ids = data
                    if cancel.is_set():
                        break
                    if not summary:
                        summary = "Summary unavailable."

                    sources = [
                        {"chat_id": msg_chat_map[tid], "telegram_id": tid}
                        for tid in source_ids
                        if tid in msg_chat_map
                    ]
                    entry = {
                        "chat_id": contact.telegram_id,
                        "chat_name": contact_name,
                        "category": "important_contact",
                        "summary": summary,
                        "sources": sources,
                    }
                    collected.append(entry)
                    yield _sse("chat_done", entry)

            # Persist the run and its entries to DB
            run_id = None
            if collected:
                try:
                    async with AsyncSessionLocal() as write_db:
                        run = await crud.create_briefing_run(write_db, days=days)
                        run_id = run.id
                        for e in collected:
                            sources = e.get("sources", [])
                            await crud.add_briefing_entry(
                                write_db,
                                run_id=run.id,
                                chat_id=e["chat_id"],
                                chat_name=e["chat_name"],
                                category=e["category"],
                                summary=e["summary"],
                                sources_json=json.dumps(sources) if sources else None,
                            )
                except Exception:
                    pass  # don't fail streaming on persistence error

            yield _sse("done", {"run_id": run_id})

        finally:
            cancel.set()
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass

    return StreamingResponse(generate(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Briefing history endpoints
# ---------------------------------------------------------------------------

@router.get("/intelligence/briefing/history")
async def get_briefing_history(db: AsyncSession = Depends(get_db)):
    """Return all past briefing runs with their entries, newest first."""
    runs = await crud.list_briefing_runs(db)
    result = []
    for run in runs:
        entries = await crud.get_briefing_run_entries(db, run.id)
        result.append({
            "id": run.id,
            "generated_at": run.generated_at.isoformat() if run.generated_at else None,
            "days": run.days,
            "entries": [
                {
                    "id": e.id,
                    "chat_id": e.chat_id,
                    "chat_name": e.chat_name,
                    "category": e.category,
                    "summary": e.summary,
                    "sources": json.loads(e.sources_json) if e.sources_json else [],
                }
                for e in entries
            ],
        })
    return result


@router.post("/intelligence/briefing/history/{run_id}/entries/{entry_id}/regenerate")
async def regenerate_briefing_entry(
    request: Request,
    run_id: int,
    entry_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Regenerate a single briefing entry in-place for an existing run.
    Streams SSE events so the frontend can show progress/reasoning and cancel.
    """
    async def generate():
        cancel = asyncio.Event()

        async def _watch_disconnect():
            while not cancel.is_set():
                if await request.is_disconnected():
                    cancel.set()
                    return
                await asyncio.sleep(0.3)

        watcher = asyncio.create_task(_watch_disconnect())

        try:
            run_res = await db.execute(select(BriefingRun).where(BriefingRun.id == run_id))
            run = run_res.scalar_one_or_none()
            if not run:
                yield _sse("error", {"message": "Briefing run not found"})
                return

            entry_res = await db.execute(
                select(BriefingEntry).where(
                    BriefingEntry.id == entry_id,
                    BriefingEntry.run_id == run_id,
                )
            )
            entry = entry_res.scalar_one_or_none()
            if not entry:
                yield _sse("error", {"message": "Briefing entry not found"})
                return

            from datetime import datetime, timedelta

            base_url, model = _get_llm_config()
            if not base_url:
                yield _sse("error", {"message": "No LLM configured. Set up an AI model in Settings."})
                return

            yield _sse(
                "start",
                {"entry_id": entry.id, "chat_name": entry.chat_name, "category": entry.category},
            )

            since = datetime.utcnow() - timedelta(days=run.days or 1)
            summary = ""
            sources: list[dict[str, int]] = []

            if entry.category in ("tracked", "briefing"):
                msgs = await crud.get_messages_since(db, entry.chat_id, since, limit=200)
                if not msgs:
                    summary = "No messages in this period."
                else:
                    lines = []
                    for row in msgs:
                        m, reply_sender, reply_text = row[0], row[1], row[2]
                        if m.text:
                            sender = "You" if m.is_outgoing else (m.sender_name or "Unknown")
                            if reply_sender and reply_text:
                                snippet = reply_text[:60].rstrip()
                                lines.append(f'{sender} [↩ {reply_sender}: "{snippet}"]: {m.text[:300]}')
                            else:
                                lines.append(f"{sender}: {m.text[:300]}")

                    if not lines:
                        summary = "No text messages in this period."
                    else:
                        messages_text = "\n".join(lines[-150:])
                        source_ids: list[int] = []
                        async with httpx.AsyncClient(timeout=180.0) as client:
                            async for event_type, data in _summarize_chat(
                                client,
                                base_url,
                                model,
                                entry.chat_name,
                                entry.category,
                                messages_text,
                                max_tokens=get_briefing_max_tokens(),
                                cancel=cancel,
                            ):
                                if event_type == "token":
                                    yield _sse("thinking", {"entry_id": entry.id, "tokens": data, "chat_name": entry.chat_name})
                                elif event_type == "reasoning":
                                    yield _sse("thinking", {"entry_id": entry.id, "reasoning": data, "chat_name": entry.chat_name})
                                else:
                                    summary, source_ids = data
                        if not summary:
                            summary = "Summary unavailable."
                        sources = [{"chat_id": entry.chat_id, "telegram_id": tid} for tid in source_ids]

            elif entry.category == "important_contact":
                rows = await crud.get_messages_from_sender_since(db, entry.chat_id, since, limit=100)
                if not rows:
                    summary = "No messages in this period."
                else:
                    lines = []
                    msg_chat_map: dict[int, int] = {}
                    for row in rows:
                        msg, chat, reply_sender, reply_text = row[0], row[1], row[2], row[3]
                        if msg.text:
                            chat_label = chat.name or str(chat.telegram_id)
                            if reply_sender and reply_text:
                                snippet = reply_text[:60].rstrip()
                                lines.append(f'[{chat_label}] [↩ {reply_sender}: "{snippet}"]: {msg.text[:300]}')
                            else:
                                lines.append(f"[{chat_label}] {msg.text[:300]}")
                            msg_chat_map[msg.telegram_id] = chat.telegram_id

                    if not lines:
                        summary = "No text messages in this period."
                    else:
                        messages_text = "\n".join(lines[-80:])
                        source_ids: list[int] = []
                        async with httpx.AsyncClient(timeout=180.0) as client:
                            async for event_type, data in _summarize_important_contact(
                                client,
                                base_url,
                                model,
                                entry.chat_name,
                                messages_text,
                                max_tokens=get_briefing_max_tokens(),
                                cancel=cancel,
                            ):
                                if event_type == "token":
                                    yield _sse("thinking", {"entry_id": entry.id, "tokens": data, "chat_name": entry.chat_name})
                                elif event_type == "reasoning":
                                    yield _sse("thinking", {"entry_id": entry.id, "reasoning": data, "chat_name": entry.chat_name})
                                else:
                                    summary, source_ids = data
                        if not summary:
                            summary = "Summary unavailable."
                        sources = [
                            {"chat_id": msg_chat_map[tid], "telegram_id": tid}
                            for tid in source_ids
                            if tid in msg_chat_map
                        ]
            else:
                yield _sse("error", {"message": f"Unsupported category: {entry.category}"})
                return

            if cancel.is_set():
                yield _sse("cancelled", {"entry_id": entry.id})
                return

            entry.summary = summary
            entry.sources_json = json.dumps(sources) if sources else None
            await db.commit()
            await db.refresh(entry)

            yield _sse(
                "done",
                {
                    "entry": {
                        "id": entry.id,
                        "run_id": entry.run_id,
                        "chat_id": entry.chat_id,
                        "chat_name": entry.chat_name,
                        "category": entry.category,
                        "summary": entry.summary,
                        "sources": sources,
                    }
                },
            )

        finally:
            cancel.set()
            watcher.cancel()
            try:
                await watcher
            except (asyncio.CancelledError, Exception):
                pass

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.delete("/intelligence/briefing/history/{run_id}")
async def delete_briefing_run(run_id: int, db: AsyncSession = Depends(get_db)):
    """Delete a past briefing run and all its entries."""
    success = await crud.delete_briefing_run(db, run_id)
    if not success:
        raise HTTPException(status_code=404, detail="Briefing run not found")
    return {"success": True}
