"""
Background scheduler that auto-triggers briefing generation at the user's
configured time, without requiring the frontend to be open.

It runs the same pipeline as the manual endpoint (backend.briefing.pipeline)
through the same single-flight job, so a manual run in progress is joined
rather than duplicated. ``briefing_last_run`` is written by the pipeline only
when a run succeeds; failures retry with backoff.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from backend import prefs
from backend.briefing import schedule
from backend.briefing.events import get_event_job
from backend.briefing.pipeline import BRIEFING_JOB_KEY, LLM_ROLE, cleanup_stale_runs, start_briefing_job
from backend.llm.resolve import llm_configured
from backend.tasks import spawn

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 60
RETRY_BACKOFF_MINUTES = (5, 15, 30, 60)

_scheduler_task: asyncio.Task | None = None
_failures = 0
_retry_after: datetime | None = None


def _note_failure(now: datetime) -> None:
    global _failures, _retry_after
    delay = RETRY_BACKOFF_MINUTES[min(_failures, len(RETRY_BACKOFF_MINUTES) - 1)]
    _failures += 1
    _retry_after = now + timedelta(minutes=delay)
    logger.warning("Briefing scheduler: run failed; retrying in %d min.", delay)


def _note_success() -> None:
    global _failures, _retry_after
    _failures = 0
    _retry_after = None


async def check_and_run(now: datetime | None = None) -> bool:
    """One scheduler tick. Returns True if a run was attempted."""
    now = now or datetime.now()
    if _retry_after is not None and now < _retry_after:
        return False
    data = prefs.load_prefs()
    sched = data.get("briefing_schedule", "daily")
    if not schedule.is_due(now, sched, data.get("briefing_time"), data.get("briefing_day"),
                           data.get("briefing_last_run")):
        return False

    days = schedule.window_days(sched)
    existing = get_event_job(BRIEFING_JOB_KEY)
    if existing is None and not llm_configured(LLM_ROLE):
        # Not an error: wait quietly and run on the first tick after a model
        # is configured (briefing_last_run stays unset, so it is still due).
        return False
    if existing is not None:
        job = existing  # a manual run is in progress — its success counts
    else:
        logger.info("Briefing scheduler: auto-run triggered (schedule=%s, days=%d).", sched, days)
        job, _ = start_briefing_job(days, trigger="scheduled")
    try:
        outcome = await asyncio.shield(job.task)
    except asyncio.CancelledError:
        if job.task.cancelled():
            _note_failure(now)
            return True
        raise
    if outcome is not None and outcome.succeeded:
        _note_success()
    else:
        _note_failure(now)
    return True


async def _scheduler_loop() -> None:
    """Runs forever, checking once per minute whether briefing should auto-run."""
    try:
        await cleanup_stale_runs()
    except Exception:
        logger.exception("Briefing scheduler: stale-run cleanup failed.")
    while True:
        try:
            await check_and_run()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Briefing scheduler: error in check loop.")
            _note_failure(datetime.now())
        await asyncio.sleep(CHECK_INTERVAL_S)


def start_briefing_scheduler() -> None:
    """Start the background briefing scheduler as an asyncio task."""
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_task = spawn(_scheduler_loop(), name="briefing_scheduler")
    logger.info("Briefing scheduler started.")
