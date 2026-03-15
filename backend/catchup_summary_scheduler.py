"""
Background scheduler that periodically generates catch-up summaries
for all chats that need attention, storing results in the DB so the
frontend can read them instantly without on-demand computation.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from sqlalchemy import select, desc, asc, func

from backend.api.settings import _load_prefs
from backend.db.database import AsyncSessionLocal
from backend.db.models import CatchUpSummary, Message

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

_scheduler_task: asyncio.Task | None = None

# Default interval in minutes
DEFAULT_INTERVAL_MINUTES = 30

# Short messages that look like chit-chat / filler
_CHITCHAT_WORDS = {
    "hi", "hey", "hello", "haha", "lol", "ok", "okay", "thanks", "thank you",
    "ty", "np", "sure", "yes", "no", "yeah", "yep", "nope", "cool", "nice",
    "great", "k", "kk", "hehe", "hmm", "ah", "oh", "wow",
}


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
        "date": msg.date.isoformat() + "Z" if msg.date else None,
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


async def generate_summary_for_chat(db, chat_telegram_id: int) -> dict:
    """Compute catch-up context for a single chat. Returns a dict with
    summary, summary_messages, recent_messages, last_activity_at, and
    total_messages count."""

    # 1. Find the user's last outgoing message or last reaction
    last_outgoing_q = (
        select(Message.date)
        .where(
            Message.chat_id == chat_telegram_id,
            Message.is_outgoing == True,  # noqa: E712
        )
        .order_by(desc(Message.date))
        .limit(1)
    )
    last_outgoing_row = (await db.execute(last_outgoing_q)).scalar_one_or_none()

    last_reaction_q = (
        select(Message.date)
        .where(
            Message.chat_id == chat_telegram_id,
            Message.my_reaction != None,  # noqa: E711
        )
        .order_by(desc(Message.date))
        .limit(1)
    )
    last_reaction_row = (await db.execute(last_reaction_q)).scalar_one_or_none()

    anchor_date = None
    if last_outgoing_row and last_reaction_row:
        anchor_date = max(last_outgoing_row, last_reaction_row)
    elif last_outgoing_row:
        anchor_date = last_outgoing_row
    elif last_reaction_row:
        anchor_date = last_reaction_row

    # 2. Get messages after anchor (incoming only), filter chit-chat
    summary_messages: list[dict] = []
    if anchor_date:
        after_msgs_q = (
            select(Message)
            .where(
                Message.chat_id == chat_telegram_id,
                Message.date > anchor_date,
                Message.is_outgoing == False,  # noqa: E712
            )
            .order_by(asc(Message.date))
            .limit(20)
        )
        after_msgs = (await db.execute(after_msgs_q)).scalars().all()
        for m in after_msgs:
            if not _is_chitchat(m.text):
                summary_messages.append(_msg_dict(m))
    else:
        recent_incoming_q = (
            select(Message)
            .where(
                Message.chat_id == chat_telegram_id,
                Message.is_outgoing == False,  # noqa: E712
            )
            .order_by(desc(Message.date))
            .limit(5)
        )
        recent_incoming = (await db.execute(recent_incoming_q)).scalars().all()
        for m in reversed(recent_incoming):
            if not _is_chitchat(m.text):
                summary_messages.append(_msg_dict(m))

    # 3. Get last 3 messages from the chat
    last_3_q = (
        select(Message)
        .where(Message.chat_id == chat_telegram_id)
        .order_by(desc(Message.date))
        .limit(3)
    )
    last_3 = (await db.execute(last_3_q)).scalars().all()
    recent_messages = [_msg_dict(m) for m in reversed(last_3)]

    # 4. Count total messages in this chat (for tracking new messages later)
    total_count_q = (
        select(func.count(Message.id))
        .where(Message.chat_id == chat_telegram_id)
    )
    total_messages = (await db.execute(total_count_q)).scalar_one()

    # 5. Build summary text
    summary_text = ""
    if summary_messages:
        count = len(summary_messages)
        senders = list({m["sender_name"] for m in summary_messages if m["sender_name"]})
        sender_str = ", ".join(senders[:3]) if senders else "Someone"
        summary_text = f"{count} message{'s' if count != 1 else ''} from {sender_str} since your last activity."
    elif anchor_date:
        summary_text = "Only casual messages since your last activity."
    else:
        summary_text = "New conversation awaiting your first reply."

    return {
        "summary": summary_text,
        "summary_messages": summary_messages[:5],
        "recent_messages": recent_messages,
        "last_activity_at": anchor_date,
        "total_messages": total_messages,
    }


async def generate_all_summaries() -> int:
    """Generate and store summaries for all chats that have pending/active
    catch-up items. Returns the number of summaries generated."""
    from backend.db.models import CatchUpItem

    count = 0
    async with AsyncSessionLocal() as db:
        # Get all active catch-up chats
        items_q = select(CatchUpItem.chat_telegram_id).where(
            CatchUpItem.status.in_(["pending", "snoozed"])
        )
        chat_ids = (await db.execute(items_q)).scalars().all()

        for chat_id in chat_ids:
            try:
                result = await generate_summary_for_chat(db, chat_id)

                # Upsert into catchup_summaries
                existing_q = select(CatchUpSummary).where(
                    CatchUpSummary.chat_telegram_id == chat_id
                )
                existing = (await db.execute(existing_q)).scalar_one_or_none()

                if existing:
                    existing.summary = result["summary"]
                    existing.summary_messages_json = json.dumps(result["summary_messages"])
                    existing.recent_messages_json = json.dumps(result["recent_messages"])
                    existing.last_activity_at = result["last_activity_at"]
                    existing.total_messages_at_generation = result["total_messages"]
                    existing.generated_at = datetime.utcnow()
                else:
                    summary_row = CatchUpSummary(
                        chat_telegram_id=chat_id,
                        summary=result["summary"],
                        summary_messages_json=json.dumps(result["summary_messages"]),
                        recent_messages_json=json.dumps(result["recent_messages"]),
                        last_activity_at=result["last_activity_at"],
                        total_messages_at_generation=result["total_messages"],
                        generated_at=datetime.utcnow(),
                    )
                    db.add(summary_row)

                await db.commit()
                count += 1
            except Exception:
                logger.exception("Failed to generate summary for chat %d", chat_id)

    return count


async def _scheduler_loop() -> None:
    """Runs forever, generating catch-up summaries at the configured interval."""
    # Initial run after a short delay to let the app start up
    await asyncio.sleep(10)

    while True:
        try:
            interval = get_summary_interval_minutes()
            logger.info("Catch-up summary scheduler: running (interval=%dm)", interval)
            count = await generate_all_summaries()
            logger.info("Catch-up summary scheduler: generated %d summaries", count)
        except Exception:
            logger.exception("Catch-up summary scheduler: error in loop.")

        interval = get_summary_interval_minutes()
        await asyncio.sleep(interval * 60)


def start_catchup_summary_scheduler() -> None:
    """Start the background catch-up summary scheduler as an asyncio task."""
    global _scheduler_task
    if _scheduler_task is not None:
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    logger.info("Catch-up summary scheduler started.")
