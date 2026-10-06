"""Single Telethon message → ``messages`` row conversion.

Every write path (history sync, live NewMessage/MessageEdited handlers,
mention sync, compose) builds its row with ``message_to_row`` so the stored
text/HTML/reactions/topic are identical no matter which path saw the message
first. Only Telegram-owned columns are produced here; local-only columns
(scam_*, my_reaction, importance, media_path) are never part of the row.
"""

import json
from datetime import datetime
from typing import Optional, Tuple

from backend.telegram.entities import is_forum as _entity_is_forum, sender_name as _sender_name
from backend.telegram.formatting import entities_to_html

# Forum chats have an implicit "General" topic whose messages carry no topic
# header. Telegram addresses it as topic id 1.
GENERAL_TOPIC_ID = 1


def raw_text(msg) -> str:
    """Unformatted message text (entity offsets refer to this string)."""
    return getattr(msg, "message", None) or ""


def is_storable(msg) -> bool:
    """Service messages (joins, pins, topic creation…) carry no text or media."""
    return bool(raw_text(msg) or getattr(msg, "media", None))


def media_type(msg) -> Optional[str]:
    if getattr(msg, "photo", None):
        return "photo"
    if getattr(msg, "sticker", None):
        return "sticker"
    if getattr(msg, "video_note", None):
        return "video_note"
    if getattr(msg, "voice", None):
        return "voice"
    doc = getattr(msg, "document", None)
    if doc:
        mime = getattr(doc, "mime_type", "") or ""
        if mime == "image/gif":
            return "gif"
        for attr in getattr(doc, "attributes", None) or []:
            if hasattr(attr, "round_message"):
                continue
            name = type(attr).__name__
            if name == "DocumentAttributeAnimated":
                return "gif"
            if name == "DocumentAttributeVideo":
                return "video"
        if mime.startswith("video/"):
            return "video"
        if mime.startswith("audio/"):
            return "audio"
        return "document"
    if getattr(msg, "video", None):
        return "video"
    if getattr(msg, "audio", None):
        return "audio"
    if getattr(msg, "gif", None):
        return "gif"
    return None


def media_filename(msg) -> Optional[str]:
    doc = getattr(msg, "document", None)
    if not doc:
        return None
    for attr in getattr(doc, "attributes", None) or []:
        if getattr(attr, "file_name", None):
            return attr.file_name
    return None


def media_size(msg) -> Optional[int]:
    doc = getattr(msg, "document", None)
    if doc and getattr(doc, "size", None):
        return doc.size
    photo = getattr(msg, "photo", None)
    if photo and hasattr(photo, "sizes"):
        sizes = [s.size for s in photo.sizes if getattr(s, "size", None)]
        if sizes:
            return max(sizes)
    return None


def extract_reactions(msg) -> Optional[str]:
    reactions = getattr(msg, "reactions", None)
    results = getattr(reactions, "results", None) if reactions else None
    if not results:
        return None
    out = []
    for r in results:
        emoji = getattr(r.reaction, "emoticon", None) or getattr(r.reaction, "document_id", None)
        if emoji is None:
            continue
        out.append({"emoji": str(emoji), "count": r.count})
    return json.dumps(out) if out else None


def topic_and_reply(msg, forum: bool) -> Tuple[Optional[int], Optional[int]]:
    """Return (topic_id, reply_to_tg_message_id).

    In a forum, a plain message inside topic T has ``reply_to_msg_id == T`` and
    no ``reply_to_top_id`` — that is topic membership, not a reply. A real reply
    inside T has ``reply_to_top_id == T``. Messages without a forum header in a
    forum chat belong to General (topic 1).
    """
    reply = getattr(msg, "reply_to", None)
    reply_to_id = getattr(reply, "reply_to_msg_id", None) if reply else None
    if reply is not None and getattr(reply, "forum_topic", False):
        top = getattr(reply, "reply_to_top_id", None)
        if top:
            return top, reply_to_id
        return reply_to_id, None
    if forum:
        return GENERAL_TOPIC_ID, reply_to_id
    return None, reply_to_id


def _naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.replace(tzinfo=None) if dt is not None else None


def message_to_row(
    msg,
    chat_id: Optional[int] = None,
    sender=None,
    *,
    forum: Optional[bool] = None,
) -> dict:
    """Build the Telegram-owned columns of a ``messages`` row."""
    if chat_id is None:
        chat_id = msg.chat_id
    if forum is None:
        forum = _entity_is_forum(getattr(msg, "chat", None))
    if sender is None:
        sender = getattr(msg, "sender", None)
    text = raw_text(msg)
    topic_id, reply_to_id = topic_and_reply(msg, forum)
    from_id = getattr(msg, "from_id", None)
    sender_id = getattr(msg, "sender_id", None)
    if sender_id is None and from_id is not None:
        sender_id = getattr(from_id, "user_id", None)
    return {
        "tg_message_id": msg.id,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "sender_name": _sender_name(sender),
        "text": text,
        "text_html": entities_to_html(text, getattr(msg, "entities", None)),
        "reactions_json": extract_reactions(msg),
        "date": _naive_utc(msg.date) or datetime.utcnow(),
        "is_outgoing": bool(getattr(msg, "out", False)),
        "media_type": media_type(msg),
        "media_filename": media_filename(msg),
        "media_size": media_size(msg),
        "grouped_id": getattr(msg, "grouped_id", None),
        "reply_to_tg_message_id": reply_to_id,
        "topic_id": topic_id,
        "is_pinned": bool(getattr(msg, "pinned", False)),
        "is_edited": bool(getattr(msg, "edit_date", None)),
        # Anything that reached us from Telegram is already delivered.
        "delivery_state": "sent",
    }
