"""Bounded scan queue: the only way messages get scam-checked.

Real-time checks (ingest fan-out), the startup/mass scan, manual "Check for
scam" and resumed requests all enqueue ``(conversation_id, message_id)``
targets here (internal ids).
A small pool of workers (default 1 — a local Ollama serves one request at a
time anyway) builds one ``ScanRequest`` per target and runs it.

* Deduped: a target already queued or running is not queued again, so mass
  scan, startup scan and real-time checks never re-scan the same row
  concurrently.
* Prioritised: manual checks jump ahead of real-time, which jump ahead of
  bulk scans; a queued bulk entry is upgraded when the user asks for it.
* Bounded: when MAX_QUEUE targets are waiting, new non-manual targets are
  dropped with a terminal ``scam_check_error`` (they stay unscanned and the
  next ``scan_unscanned_messages`` picks them up). Manual checks always get in.
* An edit that lands while the same message is being scanned re-runs it
  once afterwards, so the verdict reflects the latest text.
* Started lazily on first use, inside the running loop; the first start
  reclaims rows a previous process left 'pending'.
"""

import asyncio
import itertools
import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select, update

from backend.db.database import AsyncSessionLocal
from backend.db.models import Message
from backend.features import shield_enabled
from backend.prefs import get_pref
from backend.scam.requests import build_scan_request
from backend.scam.scan import MAX_AUTO_ATTEMPTS
from backend.scam.service import get_scam_service
from backend.scam.verdict import unscanned_condition
from backend.tasks import spawn
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

MAX_QUEUE = 5000
_PRIORITY = {"manual": 0, "rescan": 0, "realtime": 1, "edit": 1, "resume": 2, "scan": 3}
_DEFAULT_PRIORITY = 2  # unknown sources


def worker_count() -> int:
    try:
        n = int(get_pref("scam_scan_workers", 1))
    except (TypeError, ValueError):
        n = 1
    return max(1, min(4, n))


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------

class _ScanQueue:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.PriorityQueue] = None
        self._pending: dict[tuple, tuple] = {}  # key -> (priority, source) of the live entry
        self._running: set[tuple] = set()
        self._dirty: dict[tuple, str] = {}  # running keys to re-run once (text edited mid-scan)
        self._workers: list[asyncio.Task] = []
        self._seq = itertools.count()
        self._reclaimed = False
        self._reclaim_lock: Optional[asyncio.Lock] = None

    def _ensure_started(self) -> asyncio.PriorityQueue:
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._queue is None:
            # First use, or a new event loop (tests): start from scratch.
            # Unbounded at the asyncio level; MAX_QUEUE is enforced on live
            # entries so manual checks can always get in.
            self._loop = loop
            self._queue = asyncio.PriorityQueue()
            self._pending = {}
            self._running = set()
            self._dirty = {}
            self._workers = []
            self._reclaimed = False
            self._reclaim_lock = asyncio.Lock()
        self._workers = [w for w in self._workers if not w.done()]
        while len(self._workers) < worker_count():
            self._workers.append(spawn(self._worker(), name=f"scam_scan_worker_{len(self._workers)}"))
        return self._queue

    async def _ensure_ready(self) -> asyncio.PriorityQueue:
        queue = self._ensure_started()
        if not self._reclaimed:
            async with self._reclaim_lock:
                if not self._reclaimed:
                    self._reclaimed = True
                    await self._reclaim_stale_pending()
        return queue

    async def _reclaim_stale_pending(self) -> None:
        """Rows left 'pending' by a previous process (crash/quit mid-queue)
        have no in-memory entry: release the claim and queue the eligible
        ones again."""

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Message.id, Message.conversation_id, Message.scam_attempts, Message.text)
                .where(Message.scam_status == "pending")
            )).all()
            stale = [r for r in rows if (r.conversation_id, r.id) not in self._pending
                     and (r.conversation_id, r.id) not in self._running]
            if not stale:
                return
            ids = [r.id for r in stale]
            # Back to retryable: 'failed' (due now) if it was tried before, else unscanned
            await db.execute(
                update(Message).where(Message.id.in_(ids), Message.scam_attempts > 0)
                .values(scam_status="failed", scam_next_retry_at=None)
            )
            await db.execute(
                update(Message).where(Message.id.in_(ids), Message.scam_status == "pending")
                .values(scam_status=None)
            )
            await db.commit()
        logger.info("Reclaimed %d stale pending scam scans", len(stale))

        by_conv: dict[int, list[int]] = {}
        for r in stale:
            if (r.scam_attempts or 0) < MAX_AUTO_ATTEMPTS and r.text:
                by_conv.setdefault(r.conversation_id, []).append(r.id)
        for conversation_id, mids in by_conv.items():
            await self._enqueue(conversation_id, mids, "resume")

    def is_busy(self, conversation_id: int, message_id: int) -> bool:
        key = (conversation_id, message_id)
        return key in self._pending or key in self._running

    def stats(self) -> dict:
        return {"queued": len(self._pending), "running": len(self._running), "workers": len(self._workers)}

    async def enqueue(self, conversation_id: int, message_ids: list[int], source: str) -> list[int]:
        await self._ensure_ready()
        return await self._enqueue(conversation_id, message_ids, source)

    async def _enqueue(self, conversation_id: int, message_ids: list[int], source: str) -> list[int]:
        queue = self._ensure_started()
        prio = _PRIORITY.get(source, _DEFAULT_PRIORITY)
        added: list[int] = []
        dropped: list[int] = []
        for tid in dict.fromkeys(int(t) for t in message_ids):
            key = (conversation_id, tid)
            if key in self._running:
                if source == "edit":
                    # The scan in flight read the old text: run once more after it
                    self._dirty[key] = source
                continue
            current = self._pending.get(key)
            if current is not None and current[0] <= prio:
                continue
            if current is None and prio > 0 and len(self._pending) >= MAX_QUEUE:
                dropped.append(tid)  # manual checks (prio 0) are never dropped
                continue
            self._pending[key] = (prio, source)
            queue.put_nowait((prio, next(self._seq), key, source))
            added.append(tid)
        if added:
            # Claim rows with no result yet; earlier good results stay visible
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Message)
                    .where(
                        Message.conversation_id == conversation_id,
                        Message.id.in_(added),
                        or_(Message.scam_status.is_(None), Message.scam_status == "failed"),
                    )
                    .values(scam_status="pending")
                )
                await db.commit()
        if dropped:
            logger.warning("Scam scan queue full (%d); dropped %d %s targets in conversation %s",
                           MAX_QUEUE, len(dropped), source, conversation_id)
            await ws_manager.broadcast({
                "type": "scam_check_error",
                "request_id": None,
                "source": source,
                "conversation_id": conversation_id,
                "message_ids": dropped,
                "identity_ids": [],
                "messages": [],
                "error": "Scan queue is full; the message will be scanned later",
            })
        return added

    async def join(self) -> None:
        """Wait until every queued target has been processed (tests/shutdown)."""
        if self._queue is not None and self._loop is asyncio.get_running_loop():
            await self._queue.join()

    async def _worker(self) -> None:
        queue = self._queue
        while True:
            prio, _, key, source = await queue.get()
            try:
                if self._pending.get(key) != (prio, source):
                    continue  # superseded by a higher-priority entry
                del self._pending[key]
                self._running.add(key)
                try:
                    await self._process(key, source)
                finally:
                    self._running.discard(key)
                rerun = self._dirty.pop(key, None)
                if rerun is not None:
                    await self._enqueue(key[0], [key[1]], rerun)
            except Exception:
                logger.exception("Scam scan worker failed for %s", key)
            finally:
                queue.task_done()

    async def _process(self, key: tuple, source: str) -> None:

        conversation_id, message_id = key
        svc = get_scam_service()
        req = await build_scan_request(conversation_id, [message_id]) if svc is not None else None
        if req is not None:
            await svc.check_messages_for_scam(req, source=source)
            return

        # Nothing to judge: release the claim and still end the check with a
        # terminal event so no UI spinner waits forever.
        error = "Scam detection is not initialized" if svc is None else "Message has no text to analyze"
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(Message)
                .where(Message.id == message_id, Message.scam_status == "pending")
                .values(scam_status=None)
            )
            await db.commit()
        await ws_manager.broadcast({
            "type": "scam_check_error",
            "request_id": None,
            "source": source,
            "conversation_id": conversation_id,
            "message_ids": [message_id],
            "identity_ids": [],
            "messages": [],
            "error": error,
        })


_scan_queue = _ScanQueue()


def get_scan_queue() -> _ScanQueue:
    return _scan_queue


async def enqueue_targets(conversation_id: int, message_ids: list[int], source: str = "realtime") -> None:
    """Queue messages (internal ids in ``conversation_id``) to be judged, one
    request per target. ``source`` is one
    of manual | rescan | realtime | edit | resume | scan (unknown values are
    accepted at normal priority). An edited message is re-judged even if it
    already had a result; the earlier result stays visible until then."""
    if not shield_enabled():
        return
    await _scan_queue.enqueue(conversation_id, list(message_ids), source)


async def scan_unscanned_messages(max_messages: int = 200, max_age_hours: int = 48) -> int:
    """Queue recent incoming messages with no successful scan (never scanned,
    stale pending, or failed with the retry backoff elapsed). Returns the
    number of targets queued.

    Skips a first-time sync (no message older than the window), so onboarding
    doesn't scan the user's whole recent history.
    """

    if not shield_enabled() or get_scam_service() is None:
        return 0
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=max_age_hours)

    async with AsyncSessionLocal() as db:
        if (await db.execute(select(Message.id).where(Message.date < cutoff).limit(1))).scalar() is None:
            logger.info("scan_unscanned_messages: no messages older than %dh — likely first sync, skipping", max_age_hours)
            return 0
        rows = (await db.execute(
            select(Message.conversation_id, Message.id).where(
                unscanned_condition(now),
                Message.scam_attempts < MAX_AUTO_ATTEMPTS,
                Message.is_outgoing == False,  # noqa: E712
                Message.kind == "message",  # service lines are generated text
                Message.text.isnot(None),
                Message.text != "",
                Message.date >= cutoff,
            ).order_by(Message.date.desc(), Message.id.desc()).limit(max_messages)
        )).all()

    by_conv: dict[int, list[int]] = {}
    for conversation_id, message_id in rows:
        by_conv.setdefault(conversation_id, []).append(message_id)

    queued = 0
    for conversation_id, ids in by_conv.items():
        queued += len(await _scan_queue.enqueue(conversation_id, ids, "scan"))
    logger.info("scan_unscanned_messages: queued %d of %d candidates across %d conversations",
                queued, len(rows), len(by_conv))
    return queued


async def resume_pending_checks() -> None:
    """Startup: re-run checks a previous process left unfinished."""

    svc = get_scam_service()
    if svc is None:
        return
    for conversation_id, message_ids in await svc.resume_pending_requests():
        await enqueue_targets(conversation_id, message_ids, source="resume")
