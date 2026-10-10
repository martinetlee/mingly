"""
Intelligence endpoints: the AI chat (``backend.ai.agent``), mentions, and
the briefing endpoints (backed by backend.briefing.pipeline).
"""
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
from backend.ai import agent
from backend.api.views import conversation_json
from backend.db.database import get_db
from backend.llm.base import ChatMessage
from backend.llm.resolve import LLMUnavailable, get_provider
from backend.serializers import message_out

logger = logging.getLogger(__name__)

router = APIRouter()



# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------


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
        user_message_id = await agent.persist_user_message(req.ai_conversation_id, req.message)
        prior = await crud.get_ai_conversation_messages(db, req.ai_conversation_id, limit=agent.HISTORY_MESSAGES)
        history = agent.window_history(prior)
        if not history or history[-1].role != "user" or history[-1].content != req.message:
            history.append(ChatMessage(role="user", content=req.message))
    elif req.messages:
        history = [ChatMessage(role=m.role, content=m.content) for m in req.messages]
    else:
        raise HTTPException(status_code=400, detail="Provide ai_conversation_id + message")

    # Release the request session's read transaction before streaming begins;
    # the agent loop opens its own short-lived sessions per tool call.
    await db.commit()
    return sse_response(agent.run_agent(history, req.ai_conversation_id, user_message_id))


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
