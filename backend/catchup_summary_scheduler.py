"""
Background scheduler that periodically generates catch-up summaries for the
chats that need a reply (the same server-side needs-reply source the dashboard
uses), storing results in the DB so the frontend can read them instantly.

Generation runs as a single-flight job (``catchup-summaries``): the 30-minute
scheduler and the manual "generate" endpoint never run concurrently.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, desc, func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend import jobs
from backend.api.settings import _load_prefs
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Chat, CatchUpSummary, Message
from backend.llm.base import ChatMessage, LLMError
from backend.llm.resolve import LLMModelMissing, LLMUnavailable, get_provider
from backend.serialization import iso_utc
from backend.tasks import spawn

logger = logging.getLogger(__name__)

_scheduler_task: asyncio.Task | None = None

JOB_KEY = "catchup-summaries"
DEFAULT_INTERVAL_MINUTES = 30
# Needs-reply chats summarized per scheduled pass.
SCHEDULED_CHAT_LIMIT = 30
# Post-anchor messages considered (newest kept): a group last answered months
# ago must not turn into a thousands-of-messages prompt.
POST_ANCHOR_LIMIT = 60
PROMPT_CHAR_BUDGET = 12_000

# Short messages that look like chit-chat / filler
_CHITCHAT_WORDS = {
    "hi", "hey", "hello", "haha", "lol", "ok", "okay", "thanks", "thank you",
    "ty", "np", "sure", "yes", "no", "yeah", "yep", "nope", "cool", "nice",
    "great", "k", "kk", "hehe", "hmm", "ah", "oh", "wow",
}


CATCHUP_SUMMARY_PROMPT = """\
You are a concise assistant that summarizes chat conversations for a busy professional.
Given the chat name and recent messages since the user's last activity, write a brief
1-3 sentence summary that captures what happened and whether any action is needed.

Focus on:
- Key topics discussed or decisions made
- Any requests, questions, or action items directed at the user or user's organization
- Important updates or news shared

When mentioning people, we should make it clear which organization they belong to in this way: "John(AAA Corp) informs...".
Be direct. No filler. Write from a second-person perspective (e.g. "They asked you to...",
"The team discussed..."). Do not start with "Summary:" or similar labels."""


def _format_prompt(chat_name: str, messages: list[dict], org_context: str | None) -> Optional[str]:
    context_lines, new_lines = [], []
    for m in messages:
        line = f"  {m.get('sender_name') or 'Unknown'}: {m.get('text') or '(media)'}"
        (context_lines if m.get("_context") else new_lines).append(line)
    if not context_lines and not new_lines:
        return None
    # Keep the newest new-lines that fit the budget.
    kept: list[str] = []
    used = sum(len(x) for x in context_lines)
    for line in reversed(new_lines):
        if kept and used + len(line) > PROMPT_CHAR_BUDGET:
            break
        kept.append(line)
        used += len(line)
    kept.reverse()
    parts = [f"Chat: {chat_name}\n"]
    if org_context:
        parts.append(org_context)
    if context_lines:
        parts.append("Messages before your last activity (for context):\n" + "\n".join(context_lines))
    if kept:
        parts.append("Messages since your last activity:\n" + "\n".join(kept))
    return "\n\n".join(parts)


async def _llm_summarize(chat_name: str, messages: list[dict], org_context: str | None = None) -> str | None:
    """Summary text, or None on an ordinary model failure. Raises
    LLMUnavailable / LLMModelMissing so a batch stops calling a dead model."""
    user_msg = _format_prompt(chat_name, messages, org_context)
    if user_msg is None:
        return None
    provider = get_provider("catchup")
    try:
        response = await provider.complete(
            [
                ChatMessage(role="system", content=CATCHUP_SUMMARY_PROMPT),
                ChatMessage(role="user", content=user_msg),
            ],
            max_tokens=300,
            temperature=0.3,
        )
    except (LLMUnavailable, LLMModelMissing):
        raise
    except LLMError as e:
        logger.warning("LLM summarization failed for chat %s: %s", chat_name, e)
        return None
    summary = (response.content or "").strip()
    return summary or None


def _is_chitchat(text: str | None) -> bool:
    if not text:
        return True
    stripped = text.strip().lower().rstrip("!?.,:;")
    if stripped in _CHITCHAT_WORDS:
        return True
    if not any(c.isalnum() for c in stripped):
        return True
    return False


def _msg_dict(msg: Message) -> dict:
    return {
        "sender_name": msg.sender_name,
        "text": (msg.text[:200] if msg.text else None),
        "date": iso_utc(msg.date),
        "is_outgoing": msg.is_outgoing,
        "media_type": msg.media_type,
    }


def get_summary_interval_minutes() -> int:
    """Return configured interval in minutes (default 30)."""
    val = _load_prefs().get("catchup_summary_interval_minutes", DEFAULT_INTERVAL_MINUTES)
    try:
        return max(5, min(1440, int(val)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_MINUTES


async def generate_summary_for_chat(db, chat_telegram_id: int, use_llm: bool = True, orgs=None) -> dict:
    """Compute catch-up context for a single chat. Returns a dict with
    summary, summary_messages, recent_messages, last_activity_at, and
    total_messages count. Raises LLMUnavailable / LLMModelMissing."""

    # 1. Anchor: latest of last outgoing message or last reaction to someone else's message
    last_outgoing = (await db.execute(
        select(func.max(Message.date)).where(
            Message.chat_id == chat_telegram_id, Message.is_outgoing == True,  # noqa: E712
        )
    )).scalar()
    last_reaction = (await db.execute(
        select(func.max(Message.date)).where(
            Message.chat_id == chat_telegram_id,
            Message.is_outgoing == False,  # noqa: E712
            Message.my_reaction != None,  # noqa: E711
        )
    )).scalar()
    anchors = [d for d in (last_outgoing, last_reaction) if d is not None]
    anchor_date = max(anchors) if anchors else None

    # 2. 3 messages before the anchor (context) + the newest incoming after it
    summary_messages: list[dict] = []
    if anchor_date:
        before_msgs = (await db.execute(
            select(Message)
            .where(Message.chat_id == chat_telegram_id, Message.date <= anchor_date)
            .order_by(desc(Message.date)).limit(3)
        )).scalars().all()
        for m in reversed(before_msgs):
            d = _msg_dict(m)
            d["_context"] = True  # mark as pre-anchor context
            summary_messages.append(d)
        after_msgs = (await db.execute(
            select(Message)
            .where(
                Message.chat_id == chat_telegram_id,
                Message.date > anchor_date,
                Message.is_outgoing == False,  # noqa: E712
                Message.kind == "message",
            )
            .order_by(desc(Message.date)).limit(POST_ANCHOR_LIMIT)
        )).scalars().all()
        for m in reversed(after_msgs):
            if not _is_chitchat(m.text):
                summary_messages.append(_msg_dict(m))
    else:
        recent_incoming = (await db.execute(
            select(Message)
            .where(Message.chat_id == chat_telegram_id, Message.is_outgoing == False, Message.kind == "message")  # noqa: E712
            .order_by(desc(Message.date)).limit(5)
        )).scalars().all()
        for m in reversed(recent_incoming):
            if not _is_chitchat(m.text):
                summary_messages.append(_msg_dict(m))

    # 3. Count total messages in this chat (for tracking new messages later)
    total_messages = (await db.execute(
        select(func.count(Message.id)).where(Message.chat_id == chat_telegram_id)
    )).scalar_one()

    chat_name = (await db.execute(
        select(Chat.name).where(Chat.telegram_id == chat_telegram_id)
    )).scalar_one_or_none() or f"Chat {chat_telegram_id}"

    from backend.org_context import OrgDirectory
    if orgs is None:
        orgs = await OrgDirectory.load(db)
    sender_names = list({m["sender_name"] for m in summary_messages if m.get("sender_name")})
    org_context = orgs.for_senders(sender_names) if sender_names else None

    # All reads are done — release the transaction before the (slow) LLM call
    # so it doesn't hold a SQLite read lock against concurrent writers.
    await db.commit()

    summary_text = None
    degraded = False
    if summary_messages:
        if use_llm:
            summary_text = await _llm_summarize(chat_name, summary_messages, org_context=org_context)
        # Needed the model but didn't get a summary from it → basic fallback.
        degraded = summary_text is None

    if not summary_text:
        # Rule-based summary (real content, not a failure placeholder)
        new_msgs = [m for m in summary_messages if not m.get("_context")]
        if new_msgs:
            count = len(new_msgs)
            senders = list(dict.fromkeys(m["sender_name"] for m in new_msgs if m["sender_name"]))
            sender_str = ", ".join(senders[:3]) if senders else "Someone"
            summary_text = f"{count} message{'s' if count != 1 else ''} from {sender_str} since your last activity."
        elif anchor_date:
            summary_text = "Only casual messages since your last activity."
        else:
            summary_text = "New conversation awaiting your first reply."

    clean_messages = [{k: v for k, v in m.items() if k != "_context"} for m in summary_messages]
    return {
        "summary": summary_text,
        "summary_messages": clean_messages[-5:],
        "recent_messages": [],
        "last_activity_at": anchor_date,
        "total_messages": total_messages,
        "degraded": degraded,
    }


async def _upsert_summary(db, chat_id: int, result: dict) -> None:
    values = {
        "summary": result["summary"],
        "summary_messages_json": json.dumps(result["summary_messages"]),
        "recent_messages_json": json.dumps(result["recent_messages"]),
        "last_activity_at": result["last_activity_at"],
        "total_messages_at_generation": result["total_messages"],
        "generated_at": datetime.utcnow(),
        "is_degraded": bool(result.get("degraded")),
    }
    stmt = sqlite_insert(CatchUpSummary).values(chat_telegram_id=chat_id, **values)
    stmt = stmt.on_conflict_do_update(index_elements=["chat_telegram_id"], set_=values)
    await db.execute(stmt)
    await db.commit()


async def _is_fresh(db, chat_id: int, llm_available: bool = True) -> bool:
    """A stored summary is fresh if no message arrived since it was generated.
    A degraded (rule-based fallback) summary is stale whenever the LLM may be
    available again, so it gets replaced by a real one."""
    row = (await db.execute(
        select(CatchUpSummary.total_messages_at_generation, CatchUpSummary.is_degraded)
        .where(CatchUpSummary.chat_telegram_id == chat_id)
    )).one_or_none()
    if row is None:
        return False
    generated_total, degraded = row
    if degraded and llm_available:
        return False
    total = (await db.execute(select(func.count(Message.id)).where(Message.chat_id == chat_id))).scalar_one()
    return total == generated_total


async def needs_reply_chat_ids(limit: int = SCHEDULED_CHAT_LIMIT) -> list[int]:
    async with AsyncSessionLocal() as db:
        active, _ = await crud.get_needs_reply(db, limit=limit, handled_limit=0)
    return [row[0].telegram_id for row in active]


async def generate_all_summaries(
    chat_ids: Optional[List[int]] = None,
    *,
    force: bool = False,
    progress: Optional[dict] = None,
) -> int:
    """Generate and store summaries for ``chat_ids``, or for the chats that
    currently need a reply. Unchanged chats are skipped unless ``force``.
    Returns the number of summaries written."""
    if chat_ids is None:
        chat_ids = await needs_reply_chat_ids()
    if progress is not None:
        progress.update(total=len(chat_ids), done=0)

    from backend.org_context import OrgDirectory
    async with AsyncSessionLocal() as db:
        orgs = await OrgDirectory.load(db)  # once per batch, not per chat

    count = 0
    use_llm = True
    for chat_id in chat_ids:
        # One fresh session per chat: a failure can only poison its own session.
        async with AsyncSessionLocal() as db:
            try:
                if not force and await _is_fresh(db, chat_id, llm_available=use_llm):
                    continue
                try:
                    result = await generate_summary_for_chat(db, chat_id, use_llm=use_llm, orgs=orgs)
                except (LLMUnavailable, LLMModelMissing) as exc:
                    # Model is down: don't hammer it for every chat; fall back to rule-based.
                    logger.warning("Catch-up summaries: LLM unavailable (%s); using rule-based summaries.", exc)
                    use_llm = False
                    if progress is not None:
                        progress["llm_error"] = str(exc)
                    await db.rollback()
                    result = await generate_summary_for_chat(db, chat_id, use_llm=False, orgs=orgs)
                await _upsert_summary(db, chat_id, result)
                count += 1
            except Exception:
                logger.exception("Failed to generate summary for chat %d", chat_id)
            finally:
                if progress is not None:
                    progress["done"] = progress.get("done", 0) + 1
    return count


# ---------------------------------------------------------------------------
# Single-flight job + status (shared by scheduler and the manual endpoint)
# ---------------------------------------------------------------------------

_last_job: dict = {}


def get_job_status() -> dict:
    return dict(_last_job) if _last_job else {"status": "idle"}


def start_generation(chat_ids: Optional[List[int]] = None, *, force: bool = False) -> tuple[dict, bool]:
    """Start the summary job, or report the one already running.
    Returns (status_dict, started)."""
    global _last_job
    if jobs.get_running(JOB_KEY) is not None:
        return get_job_status(), False

    state = {
        "job_id": uuid.uuid4().hex[:12],
        "status": "running",
        "chat_ids": chat_ids,
        "total": len(chat_ids) if chat_ids is not None else None,
        "done": 0,
        "summaries_generated": 0,
        "started_at": time.time(),
        "finished_at": None,
        "error": None,
        "llm_error": None,
    }
    _last_job = state

    async def body():
        try:
            n = await generate_all_summaries(chat_ids, force=force, progress=state)
            state.update(status="done", summaries_generated=n)
        except Exception as exc:
            state.update(status="error", error=str(exc))
            raise
        finally:
            state["finished_at"] = time.time()
            try:
                from backend.ws.manager import ws_manager
                await ws_manager.broadcast({"type": "catchup_summaries_updated", "job_id": state["job_id"]})
            except Exception:
                pass
        return state

    jobs.run_single(JOB_KEY, body)
    return dict(state), True


async def _scheduler_loop() -> None:
    """Runs forever, generating catch-up summaries at the configured interval."""
    await asyncio.sleep(10)  # let the app start up
    while True:
        try:
            _, started = start_generation()
            task = jobs.get_running(JOB_KEY)
            if task is not None:
                await asyncio.shield(task)
            if started:
                logger.info("Catch-up summary scheduler: generated %s summaries",
                            _last_job.get("summaries_generated"))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Catch-up summary scheduler: error in loop.")
        await asyncio.sleep(get_summary_interval_minutes() * 60)


def start_catchup_summary_scheduler() -> None:
    """Start the background catch-up summary scheduler as an asyncio task."""
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_task = spawn(_scheduler_loop(), name="catchup_summary_scheduler")
    logger.info("Catch-up summary scheduler started.")
