"""
Intelligence endpoint — agentic LLM chat with tool access to the local DB.
"""
import json
import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.database import get_db, AsyncSessionLocal
from backend.db import crud
from backend.llm.config import load_registry_config

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
            messages = await crud.get_messages(db, chat.telegram_id, limit=limit)
            if not messages:
                return f"No messages found in {_qname(chat.name)}."
            lines = [f"[{m.date.strftime('%Y-%m-%d %H:%M')}] {m.sender_name or 'Unknown'}: {m.text or ''}"
                     for m in reversed(messages) if m.text]
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
    messages: list[IntelligenceMessage]


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

        # Check if user replied after the mention
        replied = False
        if msg.date:
            replied = await crud.check_replied_after(db, msg.chat_id, msg.date)

        groups.append({
            "mention_msg": _serialize_msg(msg),
            "context_before": [_serialize_msg(m) for m in context["before"]],
            "context_after": [_serialize_msg(m) for m in context["after"]],
            "replied": replied,
            "chat": {
                "id": chat.telegram_id,
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
    messages_in = [{"role": m.role, "content": m.content} for m in req.messages]

    async def generate():
        async for chunk in _run_agent(messages_in, db):
            yield chunk

    return StreamingResponse(generate(), media_type="text/event-stream")
