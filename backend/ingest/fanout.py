"""What happens after a message from any channel has been persisted.

Connectors save the row, then call into here so every channel gets the same
follow-up: catch-up reopen, workspace triage, a WebSocket delta for the
frontend, message inspection and native notifications. Nothing here talks to
a channel's network client; everything is keyed by internal ids.

Inspection is an extension point: features that judge incoming text (Shield)
register a ``MessageInspector`` (see ``backend/wiring.py``); this module never
imports them.
"""

import logging
from datetime import datetime
from typing import Optional, Protocol

from backend import prefs
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Message
from backend.notify import send_native_notification
from backend.serializers import message_out, messages_out
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)


class MessageInspector(Protocol):
    """A feature that inspects stored incoming messages (e.g. Shield)."""

    async def inspect(self, conversation_id: int, message_ids: list[int], source: str) -> None:
        """Judge these messages; ``source`` is realtime | edit."""

    def note_sender_profile(self, identity_id: int, *, bio: Optional[str] = None,
                            is_contact: Optional[bool] = None) -> None:
        """Channel hints about a sender, for the next inspection of their message."""

    async def inspect_backlog(self) -> None:
        """A channel caught up: inspect recent messages that were missed."""


_inspectors: list[MessageInspector] = []


def add_message_inspector(inspector: MessageInspector) -> None:
    if inspector not in _inspectors:
        _inspectors.append(inspector)


def remove_message_inspector(inspector: MessageInspector) -> None:
    if inspector in _inspectors:
        _inspectors.remove(inspector)


async def inspect_messages(conversation_id: int, message_ids: list[int], source: str) -> None:
    for inspector in list(_inspectors):
        try:
            await inspector.inspect(conversation_id, message_ids, source)
        except Exception:
            logger.exception("Message inspection failed for conversation %s msgs %s", conversation_id, message_ids)


def note_sender_profile(identity_id: int, profile: dict) -> None:
    for inspector in list(_inspectors):
        try:
            inspector.note_sender_profile(identity_id, **profile)
        except Exception:
            logger.exception("Sender profile hint failed for identity %s", identity_id)


async def inspect_backlog() -> None:
    for inspector in list(_inspectors):
        try:
            await inspector.inspect_backlog()
        except Exception:
            logger.exception("Backlog inspection failed")


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
        await crud.reopen_handled_catchup(db, conversation_id)


def _channel(conv: Conversation) -> Optional[str]:
    from sqlalchemy.orm.exc import DetachedInstanceError

    from backend.serializers import channel_of
    try:
        return channel_of(conv)
    except DetachedInstanceError:  # account not loaded on a detached row
        return None


async def _serialized(saved: Message) -> dict:
    """``saved`` as stored reads serialize it: with its reply preview
    (``reply_to_message_id``, ``reply_to_preview``) and participants."""
    async with AsyncSessionLocal() as db:
        rows = await crud.get_reply_previews(db, [saved.id])
        if rows:
            return (await messages_out(db, rows))[0]
    return message_out(saved)  # deleted meanwhile: the row we have


async def message_received(conv: Conversation, saved: Message, *, sender_profile: Optional[dict] = None) -> None:
    """Fan out a newly stored live message.

    ``sender_profile`` carries channel hints for Shield ({"bio",
    "is_contact"}) when the connector resolved the sender.
    """
    if not saved.is_outgoing:
        await _reopen_catchup(conv.id)
        await stamp_triage(conv.id, saved.date)

    channel = _channel(conv)
    # Full serialized message (reply preview, participants — the same shape
    # stored reads return) lets the frontend apply the event as a delta.
    await ws_manager.broadcast({
        "type": "new_message",
        "conversation_id": conv.id,
        "channel": channel,
        "message_id": saved.id,
        "conversation_title": conv.title,
        "sender_name": saved.sender_name,
        "text": (saved.text or saved.subject or "")[:120],
        "is_tracked": conv.is_tracked,
        "is_outgoing": saved.is_outgoing,
        "message": await _serialized(saved),
    })

    if saved.is_outgoing:
        return

    if saved.text:
        if sender_profile is not None and saved.sender_identity_id:
            note_sender_profile(saved.sender_identity_id, sender_profile)
        await inspect_messages(conv.id, [saved.id], "realtime")

    pref = prefs.get_notif_pref(channel)
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
        "message": await _serialized(saved),
    })
    if text_changed and not saved.is_outgoing and saved.text:
        await inspect_messages(conversation_id, [saved.id], "edit")
