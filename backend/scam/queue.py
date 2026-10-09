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
from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import or_, select, update

from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Identity, Message
from backend.features import shield_enabled
from backend.prefs import get_pref
from backend.scam.channel_ids import TELEGRAM, telegram_user_ids
from backend.scam.client import ScanRequest
from backend.scam.verdict import unscanned_condition
from backend.tasks import spawn
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

MAX_QUEUE = 5000
_PRIORITY = {"manual": 0, "rescan": 0, "realtime": 1, "edit": 1, "resume": 2, "scan": 3}
_DEFAULT_PRIORITY = 2  # unknown sources
_DEFAULT_CONTEXT = 5


def worker_count() -> int:
    try:
        n = int(get_pref("scam_scan_workers", 1))
    except (TypeError, ValueError):
        n = 1
    return max(1, min(4, n))


# ---------------------------------------------------------------------------
# Sender profile hints (bio / contact flag known to the live channel client)
# ---------------------------------------------------------------------------

_profile_hints: "OrderedDict[int, dict]" = OrderedDict()
_MAX_HINTS = 2000


def note_sender_profile(identity_id: int, *, bio: Optional[str] = None, is_contact: Optional[bool] = None) -> None:
    """Optional: remember what the channel told us about a sender so the next
    scan of their message can include it, ahead of the stored identity."""
    if not identity_id or not shield_enabled():
        return
    hint = _profile_hints.pop(identity_id, {})
    if bio is not None:
        hint["bio"] = bio
    if is_contact is not None:
        hint["is_contact"] = is_contact
    _profile_hints[identity_id] = hint
    while len(_profile_hints) > _MAX_HINTS:
        _profile_hints.popitem(last=False)


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------

def _msg_dict(m: Message, tg_users: dict[int, int]) -> dict:
    d = {
        "message_id": m.id,
        "identity_id": m.sender_identity_id,
        "sender_name": m.sender_name,
        "text": m.text,
        "date": m.date.isoformat() if m.date else None,
    }
    if m.sender_identity_id in tg_users:
        d["telegram_user_id"] = tg_users[m.sender_identity_id]
    return d


def _context_limit() -> int:
    from backend.scam.pipeline import get_pipeline_manager

    mgr = get_pipeline_manager()
    if mgr is None:
        return _DEFAULT_CONTEXT
    try:
        return max(0, int(mgr.get_config().context_messages))
    except Exception:
        return _DEFAULT_CONTEXT


async def build_scan_request(conversation_id: int, message_ids: list[int]) -> Optional[ScanRequest]:
    """Build a request for ``message_ids`` from the DB. Context = each target
    sender's earlier messages in the conversation. Returns None if no target
    has text."""
    async with AsyncSessionLocal() as db:
        targets = (await db.execute(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.id.in_(list(message_ids)),
                Message.text.isnot(None),
                Message.text != "",
            ).order_by(Message.date.asc(), Message.id.asc())
        )).scalars().all()
        if not targets:
            return None

        limit = _context_limit()
        context: list[Message] = []
        seen = {m.id for m in targets}
        if limit > 0:
            first_by_sender: dict[int, Message] = {}
            for m in targets:
                if m.sender_identity_id and m.sender_identity_id not in first_by_sender:
                    first_by_sender[m.sender_identity_id] = m
            for sid, first in first_by_sender.items():
                rows = (await db.execute(
                    select(Message).where(
                        Message.conversation_id == conversation_id,
                        Message.sender_identity_id == sid,
                        Message.text.isnot(None),
                        Message.text != "",
                        or_(Message.date < first.date,
                            (Message.date == first.date) & (Message.seq < first.seq)),
                    ).order_by(Message.date.desc(), Message.id.desc()).limit(limit)
                )).scalars().all()
                context.extend(m for m in rows if m.id not in seen)
        context.sort(key=lambda m: (m.date, m.id))

        sender_ids = list(dict.fromkeys(m.sender_identity_id for m in targets if m.sender_identity_id))
        identities = {
            i.id: i for i in (await db.execute(select(Identity).where(Identity.id.in_(sender_ids)))).scalars()
        } if sender_ids else {}
        conv = await db.get(Conversation, conversation_id)
        channel = conv.account.channel if conv is not None and conv.account is not None else None
        tg_users: dict[int, int] = {}
        if channel == TELEGRAM:
            tg_users = await telegram_user_ids(db, {m.sender_identity_id for m in targets + context})

    profiles = []
    for sid in sender_ids:
        hint = _profile_hints.get(sid, {})
        ident = identities.get(sid)
        name = next((m.sender_name for m in targets if m.sender_identity_id == sid), None)
        profile = {
            "identity_id": sid,
            "person_id": ident.person_id if ident is not None else None,
            "name": name or (ident.display_name if ident is not None else None),
            "bio": hint.get("bio", (ident.bio if ident is not None else None) or ""),
            "is_contact": hint.get("is_contact", bool(ident.is_contact) if ident is not None else False),
        }
        if sid in tg_users:
            profile["telegram_user_id"] = tg_users[sid]
        profiles.append(profile)

    return ScanRequest(
        conversation_id=conversation_id,
        channel=channel,
        targets=[_msg_dict(m, tg_users) for m in targets],
        context=[_msg_dict(m, tg_users) for m in context],
        profiles=profiles,
        metadata={
            "conversation_id": conversation_id,
            "title": (conv.title if conv else None) or "Unknown",
            "kind": (conv.kind if conv else None) or "direct",
            "channel": channel,
        },
    )


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
        from backend.scam.service import MAX_AUTO_ATTEMPTS

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
        from backend.scam.service import get_scam_service

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
    from backend.scam.service import MAX_AUTO_ATTEMPTS, get_scam_service

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
