"""Per-conversation Telegram history sync coordination.

Every history fetch (conversation open, manual sync, bulk/tag/mention sync,
backfill, pre-briefing sync) goes through ``coordinator.sync_conversation``:

- one in-flight sync per (conversation, mode): concurrent callers await the
  same future instead of skipping or stacking;
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

from sqlalchemy import select
from telethon.errors import FloodWaitError, RPCError

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Message, MessageAttachment
from backend.aio import LoopLock
from backend.tasks import spawn
from backend.telegram import ids
from backend.telegram.convert import GENERAL_TOPIC_ID, is_service, is_storable, media_type, rows_for
from backend.ws.manager import ws_manager
from backend.telegram import client as tg_client

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
        self._lock = LoopLock()  # the coordinator singleton is built at import

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
    conversation_id: int
    fetched: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    exhausted: bool = False
    flood_wait: int = 0
    error: Optional[str] = None
    added_ids: list = field(default_factory=list)  # internal message ids

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

    def is_syncing(self, conversation_id: int) -> bool:
        return any(k[1] == conversation_id and not t.done() for k, t in self._inflight.items())

    def invalidate_pinned(self, conversation_id: int) -> None:
        self._pinned_checked.pop(conversation_id, None)

    async def sync_conversation(
        self,
        conversation_id: int,
        limit: int = 50,
        *,
        offset_seq: int = 0,
        min_date: Optional[datetime] = None,
        topic_id: Optional[int] = None,
        max_wait: Optional[float] = None,
    ) -> SyncResult:
        """Fetch Telegram history for one conversation and upsert it.

        - ``offset_seq=0``: newest ``limit`` messages (also refreshes pins and
          ``last_synced_at``).
        - ``offset_seq=N``: backfill the ``limit`` messages older than the
          message with seq N (Telegram message id).
        - ``min_date``: newest messages back to ``min_date`` (capped at ``limit``).
        - ``max_wait``: give up (status "skipped") rather than wait longer than
          this for the rate limiter / a FloodWait.
        Concurrent calls with the same (conversation, mode, limit) share one
        run; a deeper request is never answered by a shallower in-flight one.
        """
        key = ("history", conversation_id, offset_seq, topic_id, min_date, limit)
        return await self._join(key, lambda: self._run(
            conversation_id, max_wait,
            lambda client, peer: self._fetch(client, conversation_id, peer, limit, offset_seq, min_date, topic_id),
        ))

    async def fetch_around(
        self,
        conversation_id: int,
        *,
        anchor_external_id: Optional[str] = None,
        date: Optional[datetime] = None,
        limit: int = 50,
    ) -> SyncResult:
        """Fetch a message (or a date) that isn't stored locally plus a window
        of ``limit`` messages around it from Telegram, and upsert them.
        Status "not_found" means Telegram has no such message / nothing at
        that date."""
        key = ("around", conversation_id, anchor_external_id, date, limit)
        anchor = int(anchor_external_id) if anchor_external_id is not None else None
        return await self._join(key, lambda: self._run(
            conversation_id, 10,
            lambda client, peer: self._fetch_around(client, conversation_id, peer, anchor, date, limit),
        ))

    async def _join(self, key: tuple, factory) -> SyncResult:
        task = self._inflight.get(key)
        if task is None or task.done():
            task = spawn(factory(), name=f"sync:{key[0]}:{key[1]}")
            self._inflight[key] = task
            task.add_done_callback(lambda t: self._inflight.pop(key, None) if self._inflight.get(key) is t else None)
        return await asyncio.shield(task)

    async def _run(self, conversation_id, max_wait, body) -> SyncResult:
        async with AsyncSessionLocal() as db:
            conv = await crud.get_conversation(db, conversation_id)
        if conv is None or conv.account is None or conv.account.channel != ids.CHANNEL:
            return SyncResult("not_found", conversation_id, error="Not a Telegram conversation")
        client = await tg_client.connected_client(require=False)
        if client is None:
            return SyncResult("unavailable", conversation_id, error="Telegram client not connected")
        # One token up front; _fetch charges one more per extra page fetched.
        if not await self.limiter.acquire(1, max_wait=max_wait):
            return SyncResult("skipped", conversation_id, flood_wait=self.limiter.flood_remaining)
        try:
            return await body(client, ids.peer_id_of(conv))
        except FloodWaitError as e:
            self.limiter.flood(e.seconds)
            logger.warning("sync conversation %s: FloodWait %ss", conversation_id, e.seconds)
            return SyncResult("error", conversation_id, flood_wait=e.seconds, error=f"FloodWait {e.seconds}s")
        except (ValueError, RPCError) as e:
            # Unresolvable entity, private/banned channel, deleted account…
            logger.info("sync conversation %s unavailable: %s", conversation_id, e)
            return SyncResult("unavailable", conversation_id, error=str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("sync conversation %s failed", conversation_id, exc_info=True)
            return SyncResult("error", conversation_id, error=str(e))

    async def _fetch(self, client, conversation_id, peer, limit, offset_seq, min_date, topic_id) -> SyncResult:
        result = SyncResult("unchanged", conversation_id)
        latest = offset_seq == 0 and min_date is None
        iter_kwargs = {"limit": limit, "offset_id": offset_seq}
        topic_scoped = topic_id is not None and topic_id != GENERAL_TOPIC_ID
        if topic_scoped:
            iter_kwargs["reply_to"] = topic_id

        seen: set[str] = set()
        oldest = newest = newest_incoming = None
        photos = []
        stopped_by_date = False

        async with AsyncSessionLocal() as db:
            conv = await crud.get_conversation(db, conversation_id)
            forum = bool(conv and conv.is_forum)
            page: list = []

            async def flush():
                res = await crud.save_messages(db, await rows_for(db, conversation_id, page, forum=forum))
                result.added += len(res["created"])
                result.updated += len(res["updated"])
                result.added_ids.extend(res["created"])
                page.clear()

            async for msg in client.iter_messages(peer, **iter_kwargs):
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
                seen.add(ids.message_key(msg.id))
                oldest = msg_date if oldest is None or msg_date < oldest else oldest
                newest = msg_date if newest is None or msg_date > newest else newest
                if not msg.out and not is_service(msg) and (newest_incoming is None or msg_date > newest_incoming):
                    newest_incoming = msg_date
                page.append(msg)
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
            if seen and oldest and newest and not topic_scoped:
                local = await db.execute(
                    select(Message.id, Message.external_id).where(
                        Message.conversation_id == conversation_id, Message.date >= oldest, Message.date < newest,
                    )
                )
                gone = {mid for mid, ext in local.all() if ext not in seen}
                if gone:
                    await crud.delete_messages(db, gone)
                    result.deleted = len(gone)
                    logger.info("sync conversation %s: removed %d deleted messages", conversation_id, len(gone))
                    await ws_manager.broadcast({
                        "type": "messages_deleted", "conversation_id": conversation_id, "message_ids": sorted(gone),
                    })

            if latest:
                result.added += await self._refresh_pinned(client, db, conversation_id, peer, forum)
                await crud.touch_synced(db, conversation_id)

            # Wake-ups discovered via sync (not live events) must also surface
            # for board triage.
            if newest_incoming is not None and result.added:
                from backend.ingest.fanout import stamp_triage
                await stamp_triage(conversation_id, newest_incoming)

            missing = await _photos_without_file(db, conversation_id, photos)

        if missing:
            from backend.telegram.media import enqueue_photo
            for m in photos:
                if m.id in missing:
                    enqueue_photo(conversation_id, missing[m.id], m)

        if result.added or result.updated or result.deleted:
            result.status = "changed"
        return result

    async def _refresh_pinned(self, client, db, conversation_id: int, peer: int, forum: bool) -> int:
        """Refresh the conversation's pinned message (TTL-cached). FloodWaitError
        propagates; other RPC failures (no rights, deleted) are ignored.
        Returns the number of newly stored messages."""
        now = time.monotonic()
        if self._pinned_checked.get(conversation_id, 0) > now:
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
            entity = await client.get_entity(peer)
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
            logger.debug("pinned probe for %s failed: %s", conversation_id, e)
            self._pinned_checked[conversation_id] = now + PINNED_TTL_SECONDS
            return 0
        self._pinned_checked[conversation_id] = now + PINNED_TTL_SECONDS

        if not pinned_id:
            await crud.set_pinned_message(db, conversation_id, None)
            return 0
        created = 0
        stored = await crud.get_message_by_external(db, conversation_id, ids.message_key(pinned_id))
        if stored is None:
            msgs = await client.get_messages(peer, ids=[pinned_id])
            pmsg = msgs[0] if msgs else None
            if pmsg is not None and is_storable(pmsg):
                res = await crud.save_messages(db, await rows_for(db, conversation_id, [pmsg], forum=forum))
                created = len(res["created"])
                stored = await crud.get_message_by_external(db, conversation_id, ids.message_key(pinned_id))
        await crud.set_pinned_message(db, conversation_id, stored.id if stored is not None else None)
        return created

    async def _fetch_around(self, client, conversation_id, peer, anchor_id, date, limit) -> SyncResult:
        result = SyncResult("unchanged", conversation_id)
        half = max(1, limit // 2)
        msgs = []
        if anchor_id is not None:
            found = await client.get_messages(peer, ids=[anchor_id])
            anchor = found[0] if found else None
            if anchor is None:
                return SyncResult("not_found", conversation_id, error="Message does not exist on Telegram")
            await self.limiter.acquire(1)
            msgs = [m async for m in client.iter_messages(peer, offset_id=anchor_id, limit=half)]
            msgs.append(anchor)
            await self.limiter.acquire(1)
            msgs += [m async for m in client.iter_messages(peer, min_id=anchor_id, reverse=True, limit=half)]
            if not is_storable(anchor):
                result.status = "not_found"
                result.error = "Message has no displayable content"
        else:
            msgs = [m async for m in client.iter_messages(peer, offset_date=date, reverse=True, limit=half)]
            await self.limiter.acquire(1)
            msgs += [m async for m in client.iter_messages(peer, offset_date=date, limit=half)]
            if not msgs:
                return SyncResult("not_found", conversation_id, error="No messages at that date")
        result.fetched = len(msgs)
        storable = [m for m in msgs if m is not None and is_storable(m)]
        async with AsyncSessionLocal() as db:
            conv = await crud.get_conversation(db, conversation_id)
            forum = bool(conv and conv.is_forum)
            res = await crud.save_messages(db, await rows_for(db, conversation_id, storable, forum=forum))
            photos = [m for m in storable if media_type(m) == "photo"]
            missing = await _photos_without_file(db, conversation_id, photos)
        result.added = len(res["created"])
        result.updated = len(res["updated"])
        result.added_ids = res["created"]
        if result.status == "unchanged" and (result.added or result.updated):
            result.status = "changed"
        from backend.telegram.media import enqueue_photo
        for m in photos:
            if m.id in missing:
                enqueue_photo(conversation_id, missing[m.id], m)
        return result


async def _photos_without_file(db, conversation_id: int, photos: list) -> dict[int, int]:
    """{telegram message id: attachment id} for photo messages whose file
    isn't downloaded yet."""
    if not photos:
        return {}
    rows = (await db.execute(
        select(Message.seq, MessageAttachment.id)
        .join(MessageAttachment, MessageAttachment.message_id == Message.id)
        .where(
            Message.conversation_id == conversation_id,
            Message.external_id.in_([ids.message_key(m.id) for m in photos]),
            MessageAttachment.local_path.is_(None),
        )
    )).all()
    return {int(seq): att_id for seq, att_id in rows}


coordinator = SyncCoordinator()


async def broadcast_conversations_synced(conversation_ids) -> None:
    ids_ = sorted(set(conversation_ids))
    if ids_:
        await ws_manager.broadcast({"type": "conversations_synced", "conversation_ids": ids_})


async def sync_conversations_since(conversation_ids: list[int], since: datetime, max_per_conversation: int = 1000) -> dict:
    """Bring local history for ``conversation_ids`` up to date back to
    ``since`` (naive UTC). Used before briefings so they never summarize a
    stale DB. Non-Telegram conversations come back "not_found".

    Returns ``{conversation_id: SyncResult}``. Never raises for
    per-conversation failures; callers decide whether "unavailable"/"error"
    results matter.
    """
    results: dict[int, SyncResult] = {}
    for cid in conversation_ids:
        results[cid] = await coordinator.sync_conversation(cid, max_per_conversation, min_date=since, max_wait=120)
    await broadcast_conversations_synced(cid for cid, r in results.items() if r.changed)
    return results

