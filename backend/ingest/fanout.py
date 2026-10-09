"""What happens after a message from any channel has been persisted.

Connectors save the row, then call into here so every channel gets the same
follow-up: catch-up reopen, workspace triage, a WebSocket delta for the
frontend, Shield scanning and native notifications. Nothing here talks to a
channel's network client; everything is keyed by internal ids.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import CatchUpItem, Conversation, Message
from backend.notify import send_native_notification
from backend.serializers import message_out
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)


async def enqueue_scam(conversation_id: int, message_ids: list[int], source: str) -> None:
    try:
        from backend.scam.queue import enqueue_targets
    except ImportError:
        logger.debug("backend.scam.queue not available; skipping scam enqueue")
        return
    try:
        await enqueue_targets(conversation_id, message_ids, source=source)
    except Exception:
        logger.exception("Scam enqueue failed for conversation %s msgs %s", conversation_id, message_ids)


async def stamp_triage(conversation_id: int, date: datetime) -> None:
    """Incoming activity surfaces the conversation for workspace board triage."""
    try:
        async with AsyncSessionLocal() as db:
            await crud.mark_workspace_triage(db, conversation_id, date)
    except Exception:
        logger.warning("workspace triage stamp failed for conversation %s", conversation_id, exc_info=True)


async def _reopen_catchup(conversation_id: int) -> None:
    """An incoming message reopens a handled catch-up item."""
    async with AsyncSessionLocal() as db:
        item = (await db.execute(
            select(CatchUpItem).where(
                CatchUpItem.conversation_id == conversation_id,
                CatchUpItem.status == "handled",
            )
        )).scalar_one_or_none()
        if item:
            item.status = "pending"
            item.handled_at = None
            item.updated_at = datetime.utcnow()
            await db.commit()


async def message_received(conv: Conversation, saved: Message, *, sender_profile: Optional[dict] = None) -> None:
    """Fan out a newly stored live message.

    ``sender_profile`` carries channel hints for Shield ({"bio",
    "is_contact"}) when the connector resolved the sender.
    """
    if not saved.is_outgoing:
        await _reopen_catchup(conv.id)
        await stamp_triage(conv.id, saved.date)

    # Full serialized message lets the frontend apply the event as a delta.
    await ws_manager.broadcast({
        "type": "new_message",
        "conversation_id": conv.id,
        "message_id": saved.id,
        "conversation_title": conv.title,
        "sender_name": saved.sender_name,
        "text": (saved.text or saved.subject or "")[:120],
        "is_tracked": conv.is_tracked,
        "is_outgoing": saved.is_outgoing,
        "message": message_out(saved),
    })

    if saved.is_outgoing:
        return

    if saved.text:
        if sender_profile is not None and saved.sender_identity_id:
            try:
                from backend.scam.queue import note_sender_profile
                note_sender_profile(saved.sender_identity_id, **sender_profile)
            except ImportError:
                pass
        await enqueue_scam(conv.id, [saved.id], "realtime")

    from backend.api.settings import get_notif_pref
    pref = get_notif_pref()
    if pref == "all" or (pref == "tracked" and conv.is_tracked):
        body_text = (saved.text or saved.subject or "")[:100]
        body = f"{saved.sender_name}: {body_text}" if saved.sender_name else body_text
        send_native_notification(conv.title or "", body, conversation_id=conv.id)


async def message_edited(conversation_id: int, saved: Message, *, text_changed: bool) -> None:
    """Fan out an edit to a stored message (text, reactions, …)."""
    await ws_manager.broadcast({
        "type": "message_edited",
        "conversation_id": conversation_id,
        "message_id": saved.id,
        "message": message_out(saved),
    })
    if text_changed and not saved.is_outgoing and saved.text:
        await enqueue_scam(conversation_id, [saved.id], "edit")
