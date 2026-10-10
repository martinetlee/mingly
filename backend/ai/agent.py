"""The in-app AI chat agent: a tool-calling loop over the configured
``intelligence`` model, streamed as SSE events (tool_call, tool_result, text,
done, error), with the conversation history windowed and fitted to the
model's context budget. The backend persists both sides of each turn."""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from backend.ai.chat_tools import TOOL_RESULT_CHARS, TOOLS, execute_tool
from backend.briefing.events import sse
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.llm.base import ChatMessage, LLMError
from backend.llm.budget import clip_to_tokens, estimate_tokens, prompt_budget_tokens
from backend.llm.resolve import LLMUnavailable, error_kind, error_message, get_provider
from backend.org_context import get_user_orgs_system_context

logger = logging.getLogger(__name__)

HISTORY_MESSAGES = 40         # persisted conversation turns loaded per request
HISTORY_CHARS = 24_000        # cap on replayed history before context budgeting
MAX_TOOL_ROUNDS = 8
AGENT_MAX_TOKENS = 2048


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


def _emit_text_chunks(content: str, chunk_size: int = 60):
    """Split final text into small chunks for a streaming-like UX."""
    for i in range(0, len(content), chunk_size):
        yield content[i:i + chunk_size]


def window_history(rows: list) -> list[ChatMessage]:
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


async def persist_user_message(ai_conversation_id: int, text: str) -> Optional[int]:
    """Persist the user's turn. Skips an identical, just-written user message
    (clients that still append it themselves) so it is never doubled."""
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


async def run_agent(history: list[ChatMessage], ai_conversation_id: Optional[int] = None, user_message_id=None):
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
