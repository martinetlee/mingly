"""Event-streaming jobs and the SSE wire format (the HTTP response wrapper is api/sse.py).

An ``EventJob`` is a single-flight background task (via ``backend.jobs``)
that records every event it emits. Any number of HTTP clients can attach to
it: each subscriber first replays the events so far, then follows live ones.
A client disconnecting therefore never kills a briefing, and a second
"Generate" click joins the run already in progress instead of starting a
duplicate.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Optional


from backend import jobs
from backend.llm.resolve import LLMError, LLMUnavailable, error_kind, error_message

logger = logging.getLogger(__name__)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def error_payload(exc: BaseException) -> dict:
    if isinstance(exc, LLMError):
        return {"message": error_message(exc), "kind": error_kind(exc)}
    return {"message": f"Unexpected error: {exc}", "kind": "internal"}


class EventJob:
    def __init__(self, key: str, kind: str, meta: Optional[dict] = None, cancel_when_orphaned: bool = False):
        self.id = uuid.uuid4().hex[:12]
        self.key = key
        self.kind = kind
        self.meta = dict(meta or {})
        self.started_at = time.time()
        self.events: list[tuple[str, dict]] = []
        self.finished = False
        self.result: Any = None
        self.progress: dict = {}
        self.task: Optional[asyncio.Task] = None
        self.cancel_when_orphaned = cancel_when_orphaned
        self._subscribers = 0
        self._changed = asyncio.Event()

    def emit(self, event: str, data: dict) -> None:
        self.events.append((event, data))
        if event in ("start", "progress"):
            self.progress.update({k: v for k, v in data.items() if k in ("total", "current", "conversation_title", "run_id", "days")})
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def finish(self) -> None:
        self.finished = True
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    def snapshot(self) -> dict:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "running": not self.finished,
            "started_at": self.started_at,
            **self.meta,
            "progress": dict(self.progress),
        }

    async def subscribe(self) -> AsyncIterator[tuple[str, dict]]:
        """Replay all events so far, then follow live events until the job ends."""
        self._subscribers += 1
        i = 0
        try:
            while True:
                changed = self._changed
                while i < len(self.events):
                    yield self.events[i]
                    i += 1
                if self.finished:
                    return
                await changed.wait()
        finally:
            self._subscribers -= 1
            if (
                self.cancel_when_orphaned and self._subscribers == 0
                and not self.finished and self.task is not None
            ):
                self.task.cancel()

    async def sse(self, attached: bool = False, extra: Optional[dict] = None) -> AsyncIterator[str]:
        tagged = False
        async for event, data in self.subscribe():
            if event == "start" and not tagged:
                data = {**data, "job_id": self.id, "attached": attached, **(extra or {})}
                tagged = True
            yield sse(event, data)


_event_jobs: dict[str, EventJob] = {}


def get_event_job(key: str) -> Optional[EventJob]:
    """The running job for ``key``, or None."""
    job = _event_jobs.get(key)
    if job is None:
        return None
    if job.finished or jobs.get_running(key) is not job.task:
        return None
    return job


def start_event_job(
    key: str,
    kind: str,
    body: Callable[[EventJob], Awaitable[Any]],
    *,
    meta: Optional[dict] = None,
    cancel_when_orphaned: bool = False,
) -> tuple[EventJob, bool]:
    """Start ``body(job)`` as the single job for ``key`` or return the running
    one. Returns (job, started). Exceptions inside ``body`` become an
    ``error`` event; cancellation becomes ``cancelled``."""
    existing = get_event_job(key)
    if existing is not None:
        return existing, False

    job = EventJob(key, kind, meta, cancel_when_orphaned)

    async def runner():
        try:
            job.result = await body(job)
            return job.result
        except asyncio.CancelledError:
            job.emit("cancelled", {**job.progress})
            raise
        except LLMUnavailable as exc:
            # Expected state (no model configured), not a crash.
            logger.info("Job %s skipped: %s", key, exc)
            job.emit("error", error_payload(exc))
            return None
        except Exception as exc:
            logger.exception("Job %s failed", key)
            job.emit("error", error_payload(exc))
            return None
        finally:
            job.finish()

    task, started = jobs.run_single(key, runner)
    if not started:  # a plain backend.jobs task holds the key (not an EventJob)
        raise RuntimeError(f"job {key} is already running")
    job.task = task
    _event_jobs[key] = job
    return job, True
