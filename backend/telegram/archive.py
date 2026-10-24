"""Background archive of every DM's full Telegram history.

One single-flight worker (``jobs.run_single("dm_archive")``) owns all archive
work, so walks never overlap:

1. enumerate every dialog (``sync_dialogs(limit=None, include_archived=True)``,
   skipped when the runtime's startup just did it) so each peer's input
   entity is cached and the bot / deleted / self flags are current;
2. serve explicit requests first (``request_chat``: groups, channels or a
   DM someone is waiting on);
3. walk each private chat that isn't complete, most recently active first
   (bots only with the ``dm_archive_include_bots`` pref);
4. every 15 minutes, top up complete DMs whose activity moved since their
   last top-up.

Coverage (``history_coverage``, per conversation, in Telegram message ids =
``messages.seq``) is the durable state. The invariant: every message with
``walk_cursor_seq <= seq <= newest_contiguous_seq`` is stored, and
``history_complete`` means the range reaches the chat's first message.

- A full walk pages newest→oldest with ``offset_id`` and never jumps. The
  cursor is saved with every page, in the same transaction as the page, so
  a restart resumes mid-chat without a gap. The walk is complete only when
  Telethon's iterator is exhausted. It handles MessageEmpty, short pages
  from "buggy" channels and the non-slice single response, so short pages
  are never taken as the end.
- A top-up walks ``min_id=newest_contiguous_seq`` upwards (``reverse=True``).
  It advances ``newest_contiguous_seq`` page by page, so missed live
  updates can't leave holes. Messages that arrive during a full walk are
  above its starting point and are fetched by the top-up that follows
  every walk.

Pages share the coordinator's RateLimiter. On a FloodWait the worker waits
it out (state ``waiting_flood``) and resumes the same chat; it never aborts.
Any other per-chat error is recorded on the coverage row. That chat is
retried with exponential backoff while the others continue.

Not covered: messages deleted on Telegram after they were archived stay
stored (the coordinator removes them when a window is re-fetched). Media
files are not downloaded by the archive (rows keep media type/name/size).
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, case, func, or_, select
from telethon.errors import FloodWaitError

from backend import prefs
from backend.aio import LoopLocal
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Account, Conversation, HistoryCoverage
from backend.jobs import get_running, run_single
from backend.serialization import iso_utc
from backend.tasks import spawn
from backend.telegram import client as tg_client, ids, sync as tg_sync
from backend.telegram.convert import rows_for
from backend.telegram.coordinator import broadcast_conversations_synced, coordinator
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

# Channels whose history this archive walks. Every other channel's stored
# history is all Mingly gets (its connector imports what the channel offers).
ARCHIVED_CHANNELS = (ids.CHANNEL,)

JOB_KEY = "dm_archive"
PAGE_SIZE = 100
TOPUP_INTERVAL_SECONDS = 15 * 60
DIALOGS_FRESH_SECONDS = 10 * 60
NO_CLIENT_RETRY_SECONDS = 10
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 6 * 3600
STATUS_MIN_INTERVAL = 1.0
PREF_PAUSED = "dm_archive_paused"
PREF_INCLUDE_BOTS = "dm_archive_include_bots"


class _Preempted(Exception):
    """A preemptible walk yielded to a request / pause / stop at a page boundary."""


def _retry_after(attempts: int) -> timedelta:
    return timedelta(seconds=min(RETRY_BASE_SECONDS * 2 ** max(0, attempts - 1), RETRY_MAX_SECONDS))


def _include_bots() -> bool:
    return bool(prefs.get_pref(PREF_INCLUDE_BOTS, False))


def _dm_filter():
    """Telegram direct conversations (bots only with the pref)."""
    cond = and_(
        Conversation.kind == "direct",
        Conversation.account_id.in_(select(Account.id).where(Account.channel == ids.CHANNEL)),
    )
    if not _include_bots():
        cond = and_(cond, Conversation.is_bot == False)  # noqa: E712
    return cond


class DMArchive:
    def __init__(self):
        self.state = "stopped"
        self.paused = False
        self.flood_wait_until: Optional[datetime] = None
        self.current_chat: Optional[dict] = None
        self.started_at: Optional[datetime] = None
        self.last_topup_at: Optional[datetime] = None
        self._counts = {"total": 0, "complete": 0, "pending": 0, "failed": 0, "bots_skipped": 0}
        self._queue: list[int] = []
        self._queue_types: dict[int, Optional[str]] = {}
        self._waiters: dict[int, list[asyncio.Future]] = {}
        self._wake: LoopLocal[asyncio.Event] = LoopLocal(asyncio.Event)
        self._dialogs_enumerated_at: Optional[datetime] = None
        self._last_broadcast = 0.0
        self._trailing: Optional[asyncio.Task] = None

    # -- status ---------------------------------------------------------------

    def get_status(self) -> dict:
        """Current status (DM counts as of the last refresh; see refresh_status)."""
        return {
            "state": self.state,
            "flood_wait_until": iso_utc(self.flood_wait_until),
            "dms": dict(self._counts),
            "current_chat": dict(self.current_chat) if self.current_chat else None,
            "started_at": iso_utc(self.started_at),
            "last_topup_at": iso_utc(self.last_topup_at),
            "groups_queued": sum(1 for cid in self._queue if self._queue_types.get(cid) != "direct"),
        }

    async def refresh_status(self) -> dict:
        await self._refresh_counts()
        return self.get_status()

    async def _refresh_counts(self) -> None:
        async with AsyncSessionLocal() as db:
            cov = HistoryCoverage
            complete = cov.history_complete == True  # noqa: E712
            # case() → integer sums (a Boolean-typed SUM would come back as bool)
            row = (await db.execute(
                select(
                    func.count(Conversation.id),
                    func.sum(case((complete, 1), else_=0)),
                    func.sum(case((and_(~func.coalesce(complete, False), cov.error.isnot(None)), 1), else_=0)),
                )
                .select_from(Conversation)
                .outerjoin(cov, cov.conversation_id == Conversation.id)
                .where(_dm_filter())
            )).one()
            bots = 0
            if not _include_bots():
                bots = (await db.execute(
                    select(func.count(Conversation.id)).where(
                        Conversation.kind == "direct", Conversation.is_bot == True,  # noqa: E712
                        Conversation.account_id.in_(select(Account.id).where(Account.channel == ids.CHANNEL)),
                    )
                )).scalar() or 0
        total, complete, failed = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        self._counts = {
            "total": total,
            "complete": complete,
            "pending": max(0, total - complete - failed),
            "failed": failed,
            "bots_skipped": int(bots),
        }

    async def _broadcast(self, force: bool = False) -> None:
        """archive_progress, at most once per second; a throttled update is
        delivered by a trailing broadcast so the last state always arrives."""
        now = time.monotonic()
        wait = self._last_broadcast + STATUS_MIN_INTERVAL - now
        if not force and wait > 0:
            if self._trailing is None or self._trailing.done():
                self._trailing = spawn(self._trailing_broadcast(wait), name="dm_archive_status")
            return
        self._last_broadcast = now
        await ws_manager.broadcast({"type": "archive_progress", **self.get_status()})

    async def _trailing_broadcast(self, delay: float) -> None:
        await asyncio.sleep(delay)
        self._last_broadcast = time.monotonic()
        await ws_manager.broadcast({"type": "archive_progress", **self.get_status()})

    async def _set_state(self, state: str) -> None:
        # Nothing to do while paused is "paused": the worker may reach its idle
        # path after a pause() that landed past its pause check this step.
        if state == "idle" and self.paused:
            state = "paused"
        changed = state != self.state
        self.state = state
        if changed:
            await self._broadcast(force=True)

    # -- controls ---------------------------------------------------------------

    def _event(self) -> asyncio.Event:
        return self._wake.get()

    def _poke(self) -> None:
        self._event().set()

    def _ensure_worker(self) -> None:
        run_single(JOB_KEY, self._run)

    def auto_start(self) -> None:
        """Called by the runtime once its startup sequence is done. Honors a
        pause the user set in an earlier session."""
        self.paused = bool(prefs.get_pref(PREF_PAUSED, False))
        self._ensure_worker()
        self._poke()

    def start(self) -> None:
        """Resume (or start) the background archive."""
        self.paused = False
        prefs.update_prefs(lambda p: p.pop(PREF_PAUSED, None))
        if self.state in ("paused", "stopped"):
            self.state = "running"
        self._ensure_worker()
        self._poke()

    def pause(self) -> None:
        """Pause the DM backlog and periodic top-ups at the next page boundary.
        Explicit request_chat work is still served."""
        self.paused = True
        prefs.update_prefs(lambda p: p.__setitem__(PREF_PAUSED, True))
        if self.state in ("running", "idle"):
            self.state = "paused"
        self._poke()

    def stop(self) -> None:
        """Cancel the worker (runtime stop: lock, logout, reset). Progress is
        durable; the next start resumes from the coverage rows."""
        task = get_running(JOB_KEY)
        if task is not None:
            task.cancel()
        self.state = "stopped"
        self.current_chat = None
        self.flood_wait_until = None
        self._queue.clear()
        self._queue_types.clear()
        for cid in list(self._waiters):
            self._resolve(cid)

    async def request_conversation(self, conversation_id: int, priority: bool = True) -> None:
        """Queue a full walk (or a top-up when already complete) for any
        Telegram conversation, DM or group, ahead of the DM backlog."""
        if conversation_id not in self._queue:
            async with AsyncSessionLocal() as db:
                conv = await crud.get_conversation(db, conversation_id)
            self._queue_types[conversation_id] = conv.kind if conv is not None else None
            if priority:
                self._queue.insert(0, conversation_id)
            else:
                self._queue.append(conversation_id)
        elif priority:
            self._queue.remove(conversation_id)
            self._queue.insert(0, conversation_id)
        self._ensure_worker()
        self._poke()
        await self._broadcast()

    def is_queued(self, conversation_id: int) -> bool:
        return conversation_id in self._queue or (self.current_chat or {}).get("conversation_id") == conversation_id

    async def wait_for_conversation(self, conversation_id: int, timeout: float) -> Optional[HistoryCoverage]:
        """Wait up to ``timeout`` seconds for a queued/running conversation to
        finish, then return its coverage row (complete or not; None if never
        walked)."""
        if self.is_queued(conversation_id) and timeout > 0:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.setdefault(conversation_id, []).append(fut)
            try:
                await asyncio.wait_for(asyncio.shield(fut), timeout)
            except asyncio.TimeoutError:
                pass
            finally:
                waiters = self._waiters.get(conversation_id)
                if waiters and fut in waiters:
                    waiters.remove(fut)
        async with AsyncSessionLocal() as db:
            return (await crud.get_coverage(db, [conversation_id])).get(conversation_id)

    def _resolve(self, conversation_id: int) -> None:
        for fut in self._waiters.pop(conversation_id, []):
            if not fut.done():
                fut.set_result(None)

    # -- worker -----------------------------------------------------------------

    async def _wait_wake(self, timeout: Optional[float]) -> None:
        ev = self._event()
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        ev.clear()

    def _should_yield(self) -> bool:
        return bool(self._queue) or self.paused

    async def _run(self) -> None:
        self.started_at = datetime.utcnow()
        self.state = "running"
        await self._broadcast(force=True)
        try:
            while True:
                try:
                    await self._step()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("DM archive step failed; retrying shortly")
                    await self._set_state("idle")
                    await self._wait_wake(30)
        finally:
            self.current_chat = None
            if self.state != "stopped":
                self.state = "stopped"

    async def _step(self) -> None:
        client = await tg_client.connected_client(require=False)
        if client is None:
            await self._set_state("idle")
            await self._wait_wake(NO_CLIENT_RETRY_SECONDS)
            return
        if not await self._enumerate_dialogs():
            return
        await self._refresh_counts()

        if self._queue:
            conversation_id = self._queue.pop(0)
            await self._process(client, conversation_id, preemptible=False)
            self._queue_types.pop(conversation_id, None)
            return
        if self.paused:
            await self._set_state("paused")
            await self._broadcast()
            await self._wait_wake(None)
            return

        conversation_id, retry_in = await self._next_backlog()
        if conversation_id is not None:
            await self._process(client, conversation_id, preemptible=True)
            return

        now = datetime.utcnow()
        since_topup = (now - self.last_topup_at).total_seconds() if self.last_topup_at else None
        if since_topup is None or since_topup >= TOPUP_INTERVAL_SECONDS:
            await self._topup_pass(client)
            return

        await self._set_state("idle")
        await self._broadcast()
        wait = TOPUP_INTERVAL_SECONDS - since_topup
        if retry_in is not None:
            wait = min(wait, retry_in)
        await self._wait_wake(max(1.0, wait))

    async def _enumerate_dialogs(self) -> bool:
        """Step 1: every dialog in every folder. Returns False when it must be
        retried (FloodWait slept out, or a transient failure)."""

        now = datetime.utcnow()
        fresh = timedelta(seconds=DIALOGS_FRESH_SECONDS)
        if self._dialogs_enumerated_at is not None:
            return True
        if tg_sync.last_full_dialog_sync is not None and now - tg_sync.last_full_dialog_sync < fresh:
            self._dialogs_enumerated_at = tg_sync.last_full_dialog_sync
            return True
        await self._set_state("running")
        self.current_chat = None
        try:
            await tg_sync.sync_dialogs(limit=None, include_archived=True)
        except FloodWaitError as e:
            await self._flood_sleep(e.seconds)
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("DM archive: dialog enumeration failed; retrying in 60s", exc_info=True)
            await self._wait_wake(60)
            return False
        self._dialogs_enumerated_at = datetime.utcnow()
        return True

    async def _next_backlog(self) -> tuple[Optional[int], Optional[float]]:
        """Most recently active incomplete DM that isn't backing off, plus the
        seconds until the next backed-off DM becomes eligible."""
        now = datetime.utcnow()
        cov = HistoryCoverage
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Conversation.id, cov.error, cov.attempts, cov.last_walk_at)
                .outerjoin(cov, cov.conversation_id == Conversation.id)
                .where(_dm_filter(), or_(cov.conversation_id.is_(None), cov.history_complete == False))  # noqa: E712
                .order_by(
                    Conversation.last_activity_at.is_(None), Conversation.last_activity_at.desc(), Conversation.id,
                )
            )).all()
        retry_in: Optional[float] = None
        for conversation_id, error, attempts, last_walk_at in rows:
            if error is None or last_walk_at is None:
                return conversation_id, None
            due = last_walk_at + _retry_after(attempts or 1)
            if due <= now:
                return conversation_id, None
            wait = (due - now).total_seconds()
            retry_in = wait if retry_in is None else min(retry_in, wait)
        return None, retry_in

    async def _topup_pass(self, client) -> None:
        """Top up complete DMs whose activity moved since their last top-up."""
        cov = HistoryCoverage
        async with AsyncSessionLocal() as db:
            due = (await db.execute(
                select(Conversation.id)
                .join(cov, cov.conversation_id == Conversation.id)
                .where(
                    _dm_filter(),
                    cov.history_complete == True,  # noqa: E712
                    or_(
                        cov.last_topup_at.is_(None),
                        and_(
                            Conversation.last_activity_at.isnot(None),
                            Conversation.last_activity_at > cov.last_topup_at,
                        ),
                    ),
                )
                .order_by(Conversation.last_activity_at.is_(None), Conversation.last_activity_at.desc())
            )).scalars().all()
        for conversation_id in due:
            if self._should_yield():
                return  # resumed on the next pass; nothing marked done
            await self._process(client, conversation_id, preemptible=True)
        self.last_topup_at = datetime.utcnow()
        await self._broadcast(force=True)

    # -- one chat ------------------------------------------------------------------

    async def _process(self, client, conversation_id: int, *, preemptible: bool) -> None:
        """Walk / top up one Telegram conversation."""
        async with AsyncSessionLocal() as db:
            conv = await crud.get_conversation(db, conversation_id)
            if conv is None or conv.account is None or conv.account.channel != ids.CHANNEL:
                self._resolve(conversation_id)
                return
            covered = await crud.get_or_create_coverage(db, conversation_id)
            await db.commit()
            complete = covered.history_complete
        peer = ids.peer_id_of(conv)
        self.current_chat = {"conversation_id": conversation_id, "title": conv.title}
        await self._set_state("running")
        await self._broadcast()
        forum = bool(conv.is_forum)
        changed = False
        try:
            while True:
                try:
                    if not complete:
                        changed |= await self._walk(client, conversation_id, peer, forum, preemptible)
                        complete = True
                    changed |= await self._topup(client, conversation_id, peer, forum, preemptible)
                    break
                except FloodWaitError as e:
                    await self._flood_sleep(e.seconds)
            await self._record_success(conversation_id)
            self._resolve(conversation_id)
        except _Preempted:
            pass  # cursor saved with the last page; resumed later
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info("DM archive: chat %s failed: %s", conversation_id, e)
            await self._record_error(conversation_id, e)
            self._resolve(conversation_id)
        finally:
            self.current_chat = None
            if changed:
                await broadcast_conversations_synced([conversation_id])
            await self._refresh_counts()
            await self._broadcast()

    async def _acquire(self) -> None:
        """One history RPC worth of tokens, showing a shared FloodWait as such."""
        remaining = coordinator.limiter.flood_remaining
        if remaining > 0:
            self.flood_wait_until = datetime.utcnow() + timedelta(seconds=remaining)
            await self._set_state("waiting_flood")
        await coordinator.limiter.acquire(1)
        if self.state == "waiting_flood":
            self.flood_wait_until = None
            await self._set_state("running")

    async def _flood_sleep(self, seconds: int) -> None:
        coordinator.limiter.flood(seconds)
        self.flood_wait_until = datetime.utcnow() + timedelta(seconds=seconds)
        logger.warning("DM archive: FloodWait %ss; waiting it out", seconds)
        await self._set_state("waiting_flood")
        await asyncio.sleep(seconds + 1)
        self.flood_wait_until = None
        await self._set_state("running")

    async def _walk(self, client, conversation_id: int, peer: int, forum: bool, preemptible: bool) -> bool:
        """Full walk newest→oldest from the saved cursor. Returns whether
        anything new was stored. Raises _Preempted at a page boundary."""
        async with AsyncSessionLocal() as db:
            cov = await crud.get_or_create_coverage(db, conversation_id)
            cursor = cov.walk_cursor_seq
            top = cov.newest_contiguous_seq if cursor is not None else None
        fresh = cursor is None
        changed = False
        batch: list = []
        fetched = 0
        await self._acquire()
        async for msg in client.iter_messages(peer, limit=None, offset_id=cursor or 0):
            fetched += 1
            if fetched > 1 and fetched % PAGE_SIZE == 1:
                await self._acquire()  # Telethon fetched another page
            if fresh and top is None:
                top = msg.id  # newest message at walk start; later ones → top-up
            batch.append(msg)
            if len(batch) >= PAGE_SIZE:
                changed |= await self._save_walk_page(conversation_id, batch, top, forum)
                batch = []
                if preemptible and self._should_yield():
                    raise _Preempted()
        if batch:
            changed |= await self._save_walk_page(conversation_id, batch, top, forum)
        async with AsyncSessionLocal() as db:
            cov = await crud.get_or_create_coverage(db, conversation_id)
            cov.history_complete = True
            if cov.newest_contiguous_seq is None:
                cov.newest_contiguous_seq = top or 0  # empty chat: top-up from the start
            cov.last_walk_at = datetime.utcnow()
            await db.commit()
        return changed

    async def _save_walk_page(self, conversation_id: int, batch: list, top: Optional[int], forum: bool) -> bool:
        lowest = min(m.id for m in batch)
        async with AsyncSessionLocal() as db:
            res = await crud.save_messages(db, await rows_for(db, conversation_id, batch, forum=forum), commit=False)
            cov = await crud.get_or_create_coverage(db, conversation_id)
            cov.walk_cursor_seq = lowest
            cov.oldest_seq = lowest if cov.oldest_seq is None else min(cov.oldest_seq, lowest)
            if cov.newest_contiguous_seq is None:
                cov.newest_contiguous_seq = top
            cov.last_walk_at = datetime.utcnow()
            await db.commit()  # page + cursor in one transaction
        return bool(res["created"] or res["updated"])

    async def _topup(self, client, conversation_id: int, peer: int, forum: bool, preemptible: bool) -> bool:
        """Everything above newest_contiguous_seq, oldest first, advancing
        it page by page."""
        async with AsyncSessionLocal() as db:
            cov = await crud.get_or_create_coverage(db, conversation_id)
            min_id = cov.newest_contiguous_seq or 0
        changed = False
        batch: list = []
        fetched = 0
        await self._acquire()
        async for msg in client.iter_messages(peer, limit=None, min_id=min_id, reverse=True):
            fetched += 1
            if fetched > 1 and fetched % PAGE_SIZE == 1:
                await self._acquire()
            batch.append(msg)
            if len(batch) >= PAGE_SIZE:
                changed |= await self._save_topup_page(conversation_id, batch, forum)
                batch = []
                if preemptible and self._should_yield():
                    raise _Preempted()
        changed |= await self._save_topup_page(conversation_id, batch, forum, done=True)
        return changed

    async def _save_topup_page(self, conversation_id: int, batch: list, forum: bool, done: bool = False) -> bool:
        async with AsyncSessionLocal() as db:
            res = await crud.save_messages(db, await rows_for(db, conversation_id, batch, forum=forum), commit=False)
            cov = await crud.get_or_create_coverage(db, conversation_id)
            if batch:
                highest = max(m.id for m in batch)
                cov.newest_contiguous_seq = max(cov.newest_contiguous_seq or 0, highest)
                if cov.oldest_seq is None:
                    cov.oldest_seq = min(m.id for m in batch)
            if done:
                cov.last_topup_at = datetime.utcnow()
            await db.commit()
        return bool(res["created"] or res["updated"])

    async def _record_success(self, conversation_id: int) -> None:
        async with AsyncSessionLocal() as db:
            cov = await crud.get_or_create_coverage(db, conversation_id)
            cov.error = None
            cov.attempts = 0
            await db.commit()

    async def _record_error(self, conversation_id: int, exc: Exception) -> None:
        async with AsyncSessionLocal() as db:
            cov = await crud.get_or_create_coverage(db, conversation_id)
            cov.error = f"{type(exc).__name__}: {exc}"[:500]
            cov.attempts = (cov.attempts or 0) + 1
            cov.last_walk_at = datetime.utcnow()
            await db.commit()


archive = DMArchive()

# Module-level API (the contract other modules code against)
get_status = archive.get_status
refresh_status = archive.refresh_status
request_conversation = archive.request_conversation
wait_for_conversation = archive.wait_for_conversation
start = archive.start
pause = archive.pause
stop = archive.stop
auto_start = archive.auto_start
