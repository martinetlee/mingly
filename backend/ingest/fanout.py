"""What happens after a message from any channel has been persisted.

Connectors save the row, then call into here so every channel gets the same
follow-up: catch-up reopen, workspace triage, a WebSocket delta for the
frontend, Shield scanning and native notifications. Nothing here talks to a
channel's network client.
"""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import CatchUpItem
from backend.notify import send_native_notification
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)


def _serialize(saved) -> dict:
    from backend.api.chats import _serialize_msg
    return _serialize_msg((saved, None, None, None))


async def enqueue_scam(chat_id: int, tg_ids: list[int], source: str) -> None:
    try:
        from backend.scam.queue import enqueue_targets
    except ImportError:
        logger.debug("backend.scam.queue not available; skipping scam enqueue")
        return
    try:
        await enqueue_targets(chat_id, tg_ids, source=source)
    except Exception:
        logger.exception("Scam enqueue failed for chat %s msgs %s", chat_id, tg_ids)


async def _reopen_for_incoming(chat_id: int, saved) -> None:
    """An incoming message reopens a handled catch-up item and stamps
    workspace triage for the chat."""
    async with AsyncSessionLocal() as db:
        catchup_item = (await db.execute(
            select(CatchUpItem).where(
                CatchUpItem.chat_telegram_id == chat_id,
                CatchUpItem.status == "handled",
            )
        )).scalar_one_or_none()
        if catchup_item:
            catchup_item.status = "pending"
            catchup_item.handled_at = None
            catchup_item.updated_at = datetime.utcnow()
            await db.commit()
        try:
            await crud.mark_workspace_triage(db, chat_id, saved.date)
        except Exception:
            logger.warning("workspace triage stamp failed for chat %s", chat_id, exc_info=True)


async def message_received(chat, saved, *, sender_profile: Optional[dict] = None) -> None:
    """Fan out a newly stored live message.

    ``chat`` is the conversation row, ``saved`` the persisted message.
    ``sender_profile`` carries channel hints for Shield ({"bio", "is_contact"})
    when the connector resolved the sender.
    """
    if not saved.is_outgoing:
        await _reopen_for_incoming(chat.telegram_id, saved)

    # Full serialized message lets the frontend apply the event as a delta.
    await ws_manager.broadcast({
        "type": "new_message",
        "chat_id": chat.telegram_id,
        "message_id": saved.id,
        "chat_name": chat.name,
        "sender_name": saved.sender_name,
        "text": (saved.text or "")[:120],
        "is_tracked": chat.is_tracked,
        "is_outgoing": saved.is_outgoing,
        "message": _serialize(saved),
    })

    if saved.is_outgoing:
        return

    if saved.text:
        if sender_profile is not None and saved.sender_id:
            try:
                from backend.scam.queue import note_sender_profile
                note_sender_profile(saved.sender_id, **sender_profile)
            except ImportError:
                pass
        await enqueue_scam(chat.telegram_id, [saved.tg_message_id], "realtime")

    from backend.api.settings import get_notif_pref
    pref = get_notif_pref()
    if pref == "all" or (pref == "tracked" and chat.is_tracked):
        body_text = (saved.text or "")[:100]
        body = f"{saved.sender_name}: {body_text}" if saved.sender_name else body_text
        send_native_notification(chat.name, body, chat_id=chat.telegram_id)


async def message_edited(chat_id: int, saved, *, text_changed: bool) -> None:
    """Fan out an edit to a stored message (text, reactions, …)."""
    await ws_manager.broadcast({
        "type": "message_edited",
        "chat_id": chat_id,
        "tg_message_id": saved.tg_message_id,
        "message": _serialize(saved),
    })
    if text_changed and not saved.is_outgoing and saved.text:
        await enqueue_scam(chat_id, [saved.tg_message_id], "edit")
