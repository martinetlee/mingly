"""
Background scheduler that auto-triggers briefing generation at the user's
configured time, without requiring the frontend to be open.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta

import httpx

from backend.api.settings import _load_prefs, _save_prefs
from backend.api.intelligence import _get_llm_config, _summarize_chat, _summarize_important_contact
from backend.db.database import AsyncSessionLocal
from backend.db import crud

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)

_scheduler_task: asyncio.Task | None = None


def _should_auto_run(schedule: str, last_run: str | None, briefing_time: str = "09:00") -> bool:
    """
    Port of the frontend shouldAutoRun logic to Python.
    Returns True if a briefing should be generated now.
    """
    now = datetime.now()
    parts = briefing_time.split(":")
    run_hour = int(parts[0]) if parts else 9
    run_min = int(parts[1]) if len(parts) > 1 else 0
    run_time = now.replace(hour=run_hour, minute=run_min, second=0, microsecond=0)

    if not last_run:
        return True

    last = datetime.fromisoformat(last_run)

    if schedule == "weekly":
        # Run if last run was before the start of this week (Monday 00:00)
        monday = now.replace(hour=0, minute=0, second=0, microsecond=0)
        weekday = monday.weekday()  # 0=Mon
        monday -= timedelta(days=weekday)
        return last < monday

    # Daily: run if it's past the configured run time and last run was before that time today
    if now >= run_time and last < run_time:
        return True
    # Or if last run was yesterday or earlier
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return last < today_midnight


async def _run_briefing_background(days: int) -> None:
    """
    Run briefing generation in the background (no SSE streaming).
    This mirrors the logic of the /api/intelligence/briefing endpoint
    but writes results directly to the DB without streaming.
    """
    base_url, model = _get_llm_config()
    if not base_url:
        logger.warning("Briefing scheduler: No LLM configured, skipping.")
        return

    since = datetime.utcnow() - timedelta(days=days)
    collected: list[dict] = []

    async with AsyncSessionLocal() as db:
        tracked_rows = await crud.get_tracked_chats(db)
        briefing_rows = await crud.get_briefing_chats(db)
        # get_tracked_chats/get_briefing_chats return (Chat, last_text, last_sender, last_date) tuples
        all_chats = [("tracked", row[0]) for row in tracked_rows] + [("briefing", row[0]) for row in briefing_rows]
        important_contacts = await crud.get_important_contacts(db)

        async with httpx.AsyncClient(timeout=120.0) as client:
            for category, chat in all_chats:
                msgs = await crud.get_messages_since(db, chat.telegram_id, since, limit=200)
                if not msgs:
                    collected.append({
                        "chat_id": chat.telegram_id,
                        "chat_name": chat.name,
                        "category": category,
                        "summary": "No messages in this period.",
                        "sources": [],
                    })
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
                    collected.append({
                        "chat_id": chat.telegram_id,
                        "chat_name": chat.name,
                        "category": category,
                        "summary": "No text messages in this period.",
                        "sources": [],
                    })
                    continue

                messages_text = "\n".join(lines[-150:])
                summary, source_ids = "", []
                async for event_type, data in _summarize_chat(
                    client, base_url, model, chat.name, category, messages_text
                ):
                    if event_type == "result":
                        summary, source_ids = data
                if not summary:
                    summary = "Summary unavailable."

                sources = [{"chat_id": chat.telegram_id, "telegram_id": tid} for tid in source_ids]
                collected.append({
                    "chat_id": chat.telegram_id,
                    "chat_name": chat.name,
                    "category": category,
                    "summary": summary,
                    "sources": sources,
                })

        # Important contacts
        async with httpx.AsyncClient(timeout=120.0) as ic_client:
            for contact in important_contacts:
                contact_name = (
                    f"{contact.first_name or ''} {contact.last_name or ''}".strip()
                    or contact.username
                    or str(contact.telegram_id)
                )

                rows = await crud.get_messages_from_sender_since(
                    db, contact.telegram_id, since, limit=100
                )
                if not rows:
                    collected.append({
                        "chat_id": contact.telegram_id,
                        "chat_name": contact_name,
                        "category": "important_contact",
                        "summary": "No messages in this period.",
                        "sources": [],
                    })
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
                    collected.append({
                        "chat_id": contact.telegram_id,
                        "chat_name": contact_name,
                        "category": "important_contact",
                        "summary": "No text messages in this period.",
                        "sources": [],
                    })
                    continue

                messages_text = "\n".join(lines[-80:])
                summary, source_ids = "", []
                async for event_type, data in _summarize_important_contact(
                    ic_client, base_url, model, contact_name, messages_text
                ):
                    if event_type == "result":
                        summary, source_ids = data
                if not summary:
                    summary = "Summary unavailable."

                sources = [
                    {"chat_id": msg_chat_map[tid], "telegram_id": tid}
                    for tid in source_ids
                    if tid in msg_chat_map
                ]
                collected.append({
                    "chat_id": contact.telegram_id,
                    "chat_name": contact_name,
                    "category": "important_contact",
                    "summary": summary,
                    "sources": sources,
                })

    # Persist
    if collected:
        try:
            async with AsyncSessionLocal() as write_db:
                run = await crud.create_briefing_run(write_db, days=days)
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
            logger.info("Briefing scheduler: generated run with %d entries.", len(collected))
        except Exception:
            logger.exception("Briefing scheduler: failed to persist run.")

    # Update last-run timestamp in prefs
    prefs = _load_prefs()
    prefs["briefing_last_run"] = datetime.now().isoformat()
    _save_prefs(prefs)


async def _scheduler_loop() -> None:
    """
    Runs forever, checking once per minute whether briefing should auto-run.
    """
    while True:
        try:
            prefs = _load_prefs()
            schedule = prefs.get("briefing_schedule", "daily")
            briefing_time = prefs.get("briefing_time", "09:00")
            last_run = prefs.get("briefing_last_run")

            if _should_auto_run(schedule, last_run, briefing_time):
                # Update timestamp BEFORE running to prevent duplicate triggers
                # while the (potentially long-running) briefing generation executes.
                prefs["briefing_last_run"] = datetime.now().isoformat()
                _save_prefs(prefs)

                days = 7 if schedule == "weekly" else 1
                logger.info("Briefing scheduler: auto-run triggered (schedule=%s, days=%d).", schedule, days)
                await _run_briefing_background(days)
        except Exception:
            logger.exception("Briefing scheduler: error in check loop.")

        await asyncio.sleep(60)


def start_briefing_scheduler() -> None:
    """Start the background briefing scheduler as an asyncio task."""
    global _scheduler_task
    if _scheduler_task is not None:
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    logger.info("Briefing scheduler started.")
