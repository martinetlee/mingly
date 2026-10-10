"""Background history sync when a conversation is opened (any channel with
``HistorySync``): retried through transient failures, reported over the
WebSocket."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from backend.connectors.roles import HistorySync
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation
from backend.jobs import get_running, run_single
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=15)


async def _local_message_count(conversation_id: int) -> int:
    async with AsyncSessionLocal() as db:
        return await crud.count_messages(db, conversation_id)


async def _auto_sync(connector: HistorySync, conv: Conversation, first_sync: bool) -> None:
    """Emits exactly one of: ``conversation_synced`` (something changed, or
    the first sync finished with history), ``conversation_sync_empty`` (a
    *completed* fetch found no retrievable history), ``conversation_sync_failed``
    (gave up / unavailable)."""
    conversation_id = conv.id
    limit = 100 if first_sync else 50
    last = None
    for attempt in range(1, 5):
        res = await connector.sync_history(conv, limit)
        last = res
        changed = res.changed
        if res.completed and first_sync and not res.exhausted:
            if await _local_message_count(conversation_id) < 5:
                # May be full of service messages — try a deeper fetch. Only
                # its own completion can prove there is no history.
                deep = await connector.sync_history(conv, 500)
                changed = changed or deep.changed
                res = last = deep
        if res.completed:
            if first_sync and await _local_message_count(conversation_id) == 0:
                await ws_manager.broadcast({"type": "conversation_sync_empty", "conversation_id": conversation_id})
                return
            if changed or first_sync:
                await ws_manager.broadcast({"type": "conversation_synced", "conversation_id": conversation_id})
            return
        if res.flood_wait:
            logger.info("auto-sync %s: FloodWait %ss (attempt %d)", conversation_id, res.flood_wait, attempt)
            await asyncio.sleep(min(res.flood_wait + 2, 600))
        elif res.status in ("unavailable", "not_found") and await connector.reachable():
            break  # the conversation itself can't be fetched (deleted, private, banned)
        else:
            await asyncio.sleep(3 * attempt)
    logger.warning("auto-sync %s gave up: %s", conversation_id, last.error if last else None)
    await ws_manager.broadcast({
        "type": "conversation_sync_failed",
        "conversation_id": conversation_id,
        "status": last.status if last else "error",
        "error": last.error if last else None,
    })


def maybe_auto_sync(connector: Optional[object], conv: Optional[Conversation], latest: bool = True) -> bool:
    """Start a background sync when the conversation was never synced (deep
    fetch, any view) or — for the latest view — its last sync is older than
    STALE_AFTER. Returns True when a sync is running."""
    if conv is None or conv.is_hidden or conv.is_deactivated or not isinstance(connector, HistorySync):
        return False
    key = f"autosync:{conv.id}"
    if get_running(key) is not None:
        return True
    first_sync = conv.last_synced_at is None
    stale = not first_sync and conv.last_synced_at < datetime.utcnow() - STALE_AFTER
    if first_sync or (stale and latest):
        run_single(key, lambda: _auto_sync(connector, conv, first_sync))
        return True
    return connector.is_syncing(conv)
