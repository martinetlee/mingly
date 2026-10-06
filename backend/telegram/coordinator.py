"""Per-chat history sync coordination.

Every history fetch (chat open, manual sync, bulk/tag/mention sync, backfill,
pre-briefing sync) goes through ``coordinator.sync_chat``:

- one in-flight sync per (chat, mode): concurrent callers await the same
  future instead of skipping or stacking;
- a typed ``SyncResult`` so callers can tell "nothing new" from "skipped",
  "Telegram unavailable" and "failed" — only a *completed* empty fetch may be
  reported to the UI as "no history";
- a global token bucket for history RPCs plus a shared FloodWait gate, so a
  rate-limit hit by one sync pauses all of them instead of escalating.
"""

import asyncio
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import delete as sa_delete, select
from telethon.errors import FloodWaitError, RPCError

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Message
from backend.tasks import spawn
from backend.telegram.runtime import runtime
from backend.telegram.convert import GENERAL_TOPIC_ID, is_storable, message_to_row, media_type
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
PINNED_TTL_SECONDS = 15 * 60


class RateLimiter:
    """Token bucket for Telegram history RPCs plus a FloodWait gate."""

    def __init__(self, rate: float = 2.0, burst: int = 5):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def flood(self, seconds: int) -> None:
        self._blocked_until = max(self._blocked_until, time.monotonic() + seconds)

    @property
    def flood_remaining(self) -> int:
        return max(0, math.ceil(self._blocked_until - time.monotonic()))

    async def acquire(self, n: int = 1, max_wait: Optional[float] = None) -> bool:
        """Take ``n`` tokens, sleeping as needed. Returns False instead of
        waiting longer than ``max_wait`` seconds."""
        n = max(1, min(n, self.burst))
        if max_wait is not None and self.flood_remaining > max_wait:
            return False
        async with self._lock:
            while True:
                now = time.monotonic()
                wait = self._blocked_until - now
                if wait <= 0:
                    self._tokens = min(self.burst, self._tokens + (now - self._last) * self.rate)
                    self._last = now
                    if self._tokens >= n:
                        self._tokens -= n
                        return True
                    wait = (n - self._tokens) / self.rate
                if max_wait is not None and wait > max_wait:
                    return False
                await asyncio.sleep(wait)


@dataclass
class SyncResult:
    # changed | unchanged | skipped | unavailable | error | not_found
    status: str
    chat_id: int
    fetched: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    exhausted: bool = False
    flood_wait: int = 0
    error: Optional[str] = None
    added_ids: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return self.status == "changed"

    @property
    def completed(self) -> bool:
        return self.status in ("changed", "unchanged")

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("added_ids", None)
        return d


class SyncCoordinator:
    def __init__(self):
        self.limiter = RateLimiter()
        self._inflight: dict[tuple, asyncio.Task] = {}
        self._pinned_checked: dict[int, float] = {}

    def is_syncing(self, chat_id: int) -> bool:
        return any(k[1] == chat_id and not t.done() for k, t in self._inflight.items())

    def invalidate_pinned(self, chat_id: int) -> None:
        self._pinned_checked.pop(chat_id, None)

    async def sync_chat(
        self,
        chat_id: int,
        limit: int = 50,
        *,
        offset_id: int = 0,
        min_date: Optional[datetime] = None,
        topic_id: Optional[int] = None,
        max_wait: Optional[float] = None,
    ) -> SyncResult:
        """Fetch history for one chat and upsert it.

        - ``offset_id=0``: newest ``limit`` messages (also refreshes pins and
          ``last_synced_at``).
        - ``offset_id=N``: backfill the ``limit`` messages older than N.
        - ``min_date``: newest messages back to ``min_date`` (capped at ``limit``).
        - ``max_wait``: give up (status "skipped") rather than wait longer than
          this for the rate limiter / a FloodWait.
        Concurrent calls with the same (chat, mode, limit) share one run; a
        deeper request is never answered by a shallower in-flight one.
        """
        key = ("history", chat_id, offset_id, topic_id, min_date, limit)
        return await self._join(key, lambda: self._run(
            chat_id, max_wait, lambda client: self._fetch(client, chat_id, limit, offset_id, min_date, topic_id),
        ))

    async def fetch_around(
        self,
        chat_id: int,
        *,
        anchor_id: Optional[int] = None,
        date: Optional[datetime] = None,
        limit: int = 50,
    ) -> SyncResult:
        """Fetch a message (or a date) that isn't stored locally plus a window
        of ``limit`` messages around it from Telegram, and upsert them.
        Status "not_found" means Telegram has no such message / nothing at
        that date."""
        key = ("around", chat_id, anchor_id, date, limit)
        return await self._join(key, lambda: self._run(
            chat_id, 10, lambda client: self._fetch_around(client, chat_id, anchor_id, date, limit),
        ))

    async def _join(self, key: tuple, factory) -> SyncResult:
        task = self._inflight.get(key)
        if task is None or task.done():
            task = spawn(factory(), name=f"sync:{key[0]}:{key[1]}")
            self._inflight[key] = task
            task.add_done_callback(lambda t: self._inflight.pop(key, None) if self._inflight.get(key) is t else None)
        return await asyncio.shield(task)

    async def _run(self, chat_id, max_wait, body) -> SyncResult:
        client = await runtime.client(require=False)
        if client is None:
            return SyncResult("unavailable", chat_id, error="Telegram client not connected")
        # One token up front; _fetch charges one more per extra page fetched.
        if not await self.limiter.acquire(1, max_wait=max_wait):
            return SyncResult("skipped", chat_id, flood_wait=self.limiter.flood_remaining)
        try:
            return await body(client)
        except FloodWaitError as e:
            self.limiter.flood(e.seconds)
            logger.warning("sync chat %s: FloodWait %ss", chat_id, e.seconds)
            return SyncResult("error", chat_id, flood_wait=e.seconds, error=f"FloodWait {e.seconds}s")
        except (ValueError, RPCError) as e:
            # Unresolvable entity, private/banned channel, deleted account…
            logger.info("sync chat %s unavailable: %s", chat_id, e)
            return SyncResult("unavailable", chat_id, error=str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("sync chat %s failed", chat_id, exc_info=True)
            return SyncResult("error", chat_id, error=str(e))

    async def _fetch(self, client, chat_id, limit, offset_id, min_date, topic_id) -> SyncResult:
        result = SyncResult("unchanged", chat_id)
        latest = offset_id == 0 and min_date is None
        iter_kwargs = {"limit": limit, "offset_id": offset_id}
        topic_scoped = topic_id is not None and topic_id != GENERAL_TOPIC_ID
        if topic_scoped:
            iter_kwargs["reply_to"] = topic_id

        seen_ids: set[int] = set()
        oldest = newest = newest_incoming = None
        photos = []
        stopped_by_date = False

        async with AsyncSessionLocal() as db:
            chat = await crud.get_chat_by_telegram_id(db, chat_id)
            forum = bool(chat and chat.is_forum)
            page: list[dict] = []

            async def flush():
                res = await crud.save_messages(db, page)
                result.added += len(res["created"])
                result.updated += len(res["updated"])
                result.added_ids.extend(res["created"])
                page.clear()

            async for msg in client.iter_messages(chat_id, **iter_kwargs):
                result.fetched += 1
                if result.fetched > 1 and result.fetched % PAGE_SIZE == 1:
                    # Telethon fetched another history page: charge it, so a
                    # 1000-message pre-briefing sync costs 10 tokens, not 1.
                    await self.limiter.acquire(1)
                msg_date = msg.date.replace(tzinfo=None)
                if min_date is not None and msg_date < min_date:
                    stopped_by_date = True
                    break
                if not is_storable(msg):
                    continue
                seen_ids.add(msg.id)
                oldest = msg_date if oldest is None or msg_date < oldest else oldest
                newest = msg_date if newest is None or msg_date > newest else newest
                if not msg.out and (newest_incoming is None or msg_date > newest_incoming):
                    newest_incoming = msg_date
                page.append(message_to_row(msg, chat_id, forum=forum))
                if media_type(msg) == "photo":
                    photos.append(msg)
                if len(page) >= PAGE_SIZE:
                    await flush()
            if page:
                await flush()
            result.exhausted = result.fetched < limit and not stopped_by_date

            # Remove local rows deleted on Telegram. Bound the window on both
            # sides: below `oldest` we didn't fetch, and at/above `newest` a
            # message may have arrived live after iter_messages snapshotted.
            if seen_ids and oldest and newest and not topic_scoped:
                local = await db.execute(
                    select(Message.tg_message_id).where(
                        Message.chat_id == chat_id, Message.date >= oldest, Message.date < newest,
                    )
                )
                gone = {r[0] for r in local.all()} - seen_ids
                if gone:
                    await db.execute(sa_delete(Message).where(
                        Message.chat_id == chat_id, Message.tg_message_id.in_(list(gone)),
                    ))
                    await db.commit()
                    result.deleted = len(gone)
                    logger.info("sync chat %s: removed %d deleted messages", chat_id, len(gone))
                    await ws_manager.broadcast({
                        "type": "messages_deleted", "chat_id": chat_id, "message_ids": sorted(gone),
                    })

            if latest:
                result.added += await self._refresh_pinned(client, db, chat_id)
                await crud.update_chat_sync(db, chat_id)

            # Wake-ups discovered via sync (not live events) must also surface
            # for board triage.
            if newest_incoming is not None and result.added:
                try:
                    await crud.mark_workspace_triage(db, chat_id, newest_incoming)
                except Exception:
                    logger.warning("workspace triage stamp failed for chat %s", chat_id, exc_info=True)

            if photos:
                missing = await db.execute(
                    select(Message.tg_message_id).where(
                        Message.chat_id == chat_id,
                        Message.tg_message_id.in_([m.id for m in photos]),
                        Message.media_path.is_(None),
                    )
                )
                missing_ids = {r[0] for r in missing.all()}
            else:
                missing_ids = set()

        if missing_ids:
            from backend.telegram.media import enqueue_photo
            for m in photos:
                if m.id in missing_ids:
                    enqueue_photo(chat_id, m)

        if result.added or result.updated or result.deleted:
            result.status = "changed"
        return result

    async def _refresh_pinned(self, client, db, chat_id: int) -> int:
        """Refresh the chat's pinned message id (TTL-cached). FloodWaitError
        propagates; other RPC failures (no rights, deleted) are ignored.
        Returns the number of newly stored messages."""
        now = time.monotonic()
        if self._pinned_checked.get(chat_id, 0) > now:
            return 0
        # Up to three RPCs (entity, full info, pinned message): pay for them,
        # but skip the probe this round rather than queue behind a FloodWait.
        if not await self.limiter.acquire(2, max_wait=5):
            return 0
        from telethon.tl.functions.channels import GetFullChannelRequest
        from telethon.tl.functions.messages import GetFullChatRequest
        from telethon.tl.functions.users import GetFullUserRequest
        from telethon.tl.types import Channel, Chat, User

        try:
            entity = await client.get_entity(chat_id)
            if isinstance(entity, Channel):
                full = await client(GetFullChannelRequest(entity))
                pinned_id = getattr(full.full_chat, "pinned_msg_id", None)
            elif isinstance(entity, Chat):
                full = await client(GetFullChatRequest(chat_id=entity.id))
                pinned_id = getattr(full.full_chat, "pinned_msg_id", None)
            elif isinstance(entity, User):
                full = await client(GetFullUserRequest(entity))
                pinned_id = getattr(full.full_user, "pinned_msg_id", None)
            else:
                return 0
        except FloodWaitError:
            raise
        except (ValueError, RPCError) as e:
            logger.debug("pinned probe for %s failed: %s", chat_id, e)
            self._pinned_checked[chat_id] = now + PINNED_TTL_SECONDS
            return 0
        self._pinned_checked[chat_id] = now + PINNED_TTL_SECONDS

        await crud.set_chat_pinned_message(db, chat_id, pinned_id)
        if not pinned_id:
            return 0
        msgs = await client.get_messages(chat_id, ids=[pinned_id])
        pmsg = msgs[0] if msgs else None
        if pmsg is None or not is_storable(pmsg):
            return 0
        chat = await crud.get_chat_by_telegram_id(db, chat_id)
        res = await crud.save_messages(db, [message_to_row(pmsg, chat_id, forum=bool(chat and chat.is_forum))])
        return len(res["created"])


    async def _fetch_around(self, client, chat_id, anchor_id, date, limit) -> SyncResult:
        result = SyncResult("unchanged", chat_id)
        half = max(1, limit // 2)
        msgs = []
        if anchor_id is not None:
            found = await client.get_messages(chat_id, ids=[anchor_id])
            anchor = found[0] if found else None
            if anchor is None:
                return SyncResult("not_found", chat_id, error="Message does not exist on Telegram")
            await self.limiter.acquire(1)
            msgs = [m async for m in client.iter_messages(chat_id, offset_id=anchor_id, limit=half)]
            msgs.append(anchor)
            await self.limiter.acquire(1)
            msgs += [m async for m in client.iter_messages(chat_id, min_id=anchor_id, reverse=True, limit=half)]
            if not is_storable(anchor):
                result.status = "not_found"
                result.error = "Message has no displayable content"
        else:
            msgs = [m async for m in client.iter_messages(chat_id, offset_date=date, reverse=True, limit=half)]
            await self.limiter.acquire(1)
            msgs += [m async for m in client.iter_messages(chat_id, offset_date=date, limit=half)]
            if not msgs:
                return SyncResult("not_found", chat_id, error="No messages at that date")
        result.fetched = len(msgs)
        storable = [m for m in msgs if m is not None and is_storable(m)]
        async with AsyncSessionLocal() as db:
            chat = await crud.get_chat_by_telegram_id(db, chat_id)
            forum = bool(chat and chat.is_forum)
            res = await crud.save_messages(db, [message_to_row(m, chat_id, forum=forum) for m in storable])
        result.added = len(res["created"])
        result.updated = len(res["updated"])
        result.added_ids = res["created"]
        if result.status == "unchanged" and (result.added or result.updated):
            result.status = "changed"
        from backend.telegram.media import enqueue_photo
        for m in storable:
            if m.id in res["created"] and media_type(m) == "photo":
                enqueue_photo(chat_id, m)
        return result


coordinator = SyncCoordinator()


async def sync_messages(chat_id: int, limit: int = 50, **kwargs) -> SyncResult:
    """Convenience wrapper around ``coordinator.sync_chat``."""
    return await coordinator.sync_chat(chat_id, limit, **kwargs)


async def broadcast_chats_synced(chat_ids) -> None:
    ids = sorted(set(chat_ids))
    if ids:
        await ws_manager.broadcast({"type": "chats_synced", "chat_ids": ids})


async def sync_chats_since(chat_ids: list[int], since: datetime, max_per_chat: int = 1000) -> dict:
    """Bring local history for ``chat_ids`` up to date back to ``since``
    (naive UTC). Used before briefings so they never summarize a stale DB.

    Returns ``{chat_id: SyncResult}``. Never raises for per-chat failures;
    callers decide whether "unavailable"/"error" results matter.
    """
    results: dict[int, SyncResult] = {}
    for cid in chat_ids:
        results[cid] = await coordinator.sync_chat(cid, max_per_chat, min_date=since, max_wait=120)
    await broadcast_chats_synced(cid for cid, r in results.items() if r.changed)
    return results
