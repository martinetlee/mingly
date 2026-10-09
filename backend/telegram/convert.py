"""Single Telethon message → ``messages`` row conversion.

Every write path (history sync, live NewMessage/MessageEdited handlers,
mention sync, compose) builds its row with ``message_to_row`` so the stored
text/HTML/reactions/topic are identical no matter which path saw the message
first. Only Telegram-owned columns are produced here; local-only columns
(scam_*, my_reaction, importance, attachment local paths) are never part of
the row. Resolving the conversation and sender identity is the caller's job
(``backend.telegram.ids``).
"""

import json
import re
from datetime import datetime
from typing import Optional, Tuple

from telethon import utils as tl_utils
from telethon.tl import types

from backend.serialization import iso_utc
from backend.telegram.entities import is_forum as _entity_is_forum, sender_name as _sender_name
from backend.telegram.formatting import entities_to_html

# Forum chats have an implicit "General" topic whose messages carry no topic
# header. Telegram addresses it as topic id 1.
GENERAL_TOPIC_ID = 1


def raw_text(msg) -> str:
    """Unformatted message text (entity offsets refer to this string)."""
    return getattr(msg, "message", None) or ""


def is_service(msg) -> bool:
    action = getattr(msg, "action", None)
    return action is not None and not isinstance(action, types.MessageActionEmpty)


def is_storable(msg) -> bool:
    """Everything Telegram shows in a chat: text, media (polls, locations,
    contacts… included) and service messages (joins, pins, calls…). Only
    empty shells (MessageEmpty, no content at all) are skipped."""
    if msg is None or isinstance(msg, types.MessageEmpty):
        return False
    media = getattr(msg, "media", None)
    if isinstance(media, types.MessageMediaEmpty):
        media = None
    return bool(raw_text(msg) or media or is_service(msg))


def media_type(msg) -> Optional[str]:
    """Kind of the message's own file, or None. Only photo/document media
    count: Telethon's ``msg.photo`` / ``msg.document`` also return a link
    preview's image or video and a service action's photo ("changed the
    group photo"), neither of which is a file of the message."""
    if is_service(msg) or not isinstance(
        getattr(msg, "media", None), (types.MessageMediaPhoto, types.MessageMediaDocument)
    ):
        return None
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
    """Return (topic_id, Telegram id of the replied-to message).

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


# ---------------------------------------------------------------------------
# Service messages and structured media → readable text + meta
# ---------------------------------------------------------------------------

def _twe(value) -> str:
    """Plain text of a TextWithEntities (newer layers) or a bare string."""
    if value is None:
        return ""
    return getattr(value, "text", value) if not isinstance(value, str) else value


def _duration(seconds: Optional[int]) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _period(seconds: int) -> str:
    for unit, size in (("month", 2592000), ("week", 604800), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''}"
    return f"{seconds} seconds"


def _action_name(action) -> str:
    name = type(action).__name__
    if name.startswith("MessageAction"):
        name = name[len("MessageAction"):]
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _names(entities, ids) -> str:
    out = []
    by_id = {getattr(e, "id", None): e for e in (entities or []) if e is not None}
    for uid in ids or []:
        e = by_id.get(uid)
        out.append((_sender_name(e) if e is not None else None) or f"user {uid}")
    return ", ".join(out)


def service_text(msg, sender=None) -> Tuple[str, dict]:
    """Readable one-line description of a service message plus its meta."""
    action = msg.action
    out = bool(getattr(msg, "out", False))
    actor = "You" if out else (_sender_name(sender) or "Someone")
    ents = getattr(msg, "action_entities", None)
    meta: dict = {"action": _action_name(action)}
    A = types

    if isinstance(action, A.MessageActionPhoneCall):
        kind = "Video call" if action.video else "Call"
        reason = action.reason
        meta.update(duration=action.duration, video=bool(action.video))
        if isinstance(reason, A.PhoneCallDiscardReasonMissed):
            meta["reason"] = "missed"
            text = f"Cancelled {kind.lower()}" if out else f"Missed {kind.lower()}"
        elif isinstance(reason, A.PhoneCallDiscardReasonBusy):
            meta["reason"] = "busy"
            text = f"Declined {kind.lower()}"
        else:
            text = f"{'Outgoing' if out else 'Incoming'} {kind.lower()}"
            if action.duration:
                text += f" ({_duration(action.duration)})"
        return text, meta
    if isinstance(action, A.MessageActionConferenceCall):
        meta.update(duration=action.duration, video=bool(action.video), missed=bool(action.missed))
        if action.missed:
            return "Missed group call", meta
        return "Group call" + (f" ({_duration(action.duration)})" if action.duration else ""), meta
    if isinstance(action, A.MessageActionGroupCall):
        meta["duration"] = action.duration
        if action.duration:
            return f"Voice chat ended ({_duration(action.duration)})", meta
        return f"{actor} started a voice chat", meta
    if isinstance(action, A.MessageActionGroupCallScheduled):
        meta["schedule_date"] = iso_utc(action.schedule_date)
        return f"{actor} scheduled a voice chat", meta
    if isinstance(action, A.MessageActionInviteToGroupCall):
        meta["user_ids"] = list(action.users)
        return f"{actor} invited {_names(ents, action.users)} to the voice chat", meta
    if isinstance(action, A.MessageActionChatAddUser):
        meta["user_ids"] = list(action.users)
        if getattr(msg, "sender_id", None) in action.users:
            return f"{actor} joined the group", meta
        return f"{actor} added {_names(ents, action.users)}", meta
    if isinstance(action, A.MessageActionChatDeleteUser):
        meta["user_id"] = action.user_id
        if getattr(msg, "sender_id", None) == action.user_id:
            return f"{actor} left the group", meta
        return f"{actor} removed {_names(ents, [action.user_id])}", meta
    if isinstance(action, A.MessageActionChatJoinedByLink):
        meta["inviter_id"] = action.inviter_id
        return f"{actor} joined the group via invite link", meta
    if isinstance(action, A.MessageActionChatJoinedByRequest):
        return f"{actor} was accepted into the group", meta
    if isinstance(action, A.MessageActionChatCreate):
        meta.update(title=action.title, user_ids=list(action.users))
        return f'{actor} created the group "{action.title}"', meta
    if isinstance(action, A.MessageActionChannelCreate):
        meta["title"] = action.title
        return f'Channel "{action.title}" created', meta
    if isinstance(action, A.MessageActionChatEditTitle):
        meta["title"] = action.title
        return f'{actor} changed the group name to "{action.title}"', meta
    if isinstance(action, A.MessageActionChatEditPhoto):
        return f"{actor} changed the group photo", meta
    if isinstance(action, A.MessageActionChatDeletePhoto):
        return f"{actor} removed the group photo", meta
    if isinstance(action, A.MessageActionChatMigrateTo):
        meta["channel_id"] = tl_utils.get_peer_id(A.PeerChannel(action.channel_id))
        return "Group upgraded to a supergroup", meta
    if isinstance(action, A.MessageActionChannelMigrateFrom):
        meta.update(title=action.title, chat_id=-action.chat_id)
        return f'Supergroup created from the group "{action.title}"', meta
    if isinstance(action, A.MessageActionPinMessage):
        return f"{actor} pinned a message", meta
    if isinstance(action, A.MessageActionHistoryClear):
        return "Chat history was cleared", meta
    if isinstance(action, A.MessageActionScreenshotTaken):
        return f"{actor} took a screenshot", meta
    if isinstance(action, A.MessageActionContactSignUp):
        return f"{actor} joined Telegram", meta
    if isinstance(action, A.MessageActionSetMessagesTTL):
        meta["period"] = action.period
        if not action.period:
            return f"{actor} disabled auto-delete", meta
        return f"{actor} set messages to auto-delete after {_period(action.period)}", meta
    if isinstance(action, A.MessageActionTopicCreate):
        meta["title"] = action.title
        return f'{actor} created the topic "{action.title}"', meta
    if isinstance(action, A.MessageActionTopicEdit):
        meta.update(title=action.title, closed=action.closed, hidden=action.hidden)
        if action.title:
            return f'{actor} renamed the topic to "{action.title}"', meta
        if action.closed is not None:
            return f"{actor} {'closed' if action.closed else 'reopened'} the topic", meta
        if action.hidden is not None:
            return f"{actor} {'hid' if action.hidden else 'unhid'} the topic", meta
        return f"{actor} edited the topic", meta
    if isinstance(action, A.MessageActionGameScore):
        meta.update(game_id=action.game_id, score=action.score)
        return f"{actor} scored {action.score}", meta
    if isinstance(action, A.MessageActionCustomAction):
        return action.message or "Service message", meta
    if isinstance(action, (A.MessageActionPaymentSent, A.MessageActionPaymentSentMe)):
        meta.update(currency=action.currency, total_amount=action.total_amount)
        return f"Payment of {action.total_amount / 100:.2f} {action.currency}", meta
    if isinstance(action, (A.MessageActionGiftPremium, A.MessageActionGiftCode)):
        meta["days"] = getattr(action, "days", None)
        return f"{actor} gifted Telegram Premium", meta
    if isinstance(action, (A.MessageActionStarGift, A.MessageActionStarGiftUnique, A.MessageActionGiftStars)):
        return f"{actor} sent a gift", meta
    if isinstance(action, A.MessageActionSetChatTheme):
        return f"{actor} changed the chat theme", meta
    if isinstance(action, A.MessageActionSetChatWallPaper):
        return f"{actor} changed the chat wallpaper", meta
    if isinstance(action, A.MessageActionSuggestProfilePhoto):
        return f"{actor} suggested a profile photo", meta
    if isinstance(action, A.MessageActionBotAllowed):
        return "You allowed this bot to message you", meta
    if isinstance(action, A.MessageActionGeoProximityReached):
        meta["distance"] = action.distance
        return f"Within {action.distance} m of each other", meta
    if isinstance(action, A.MessageActionWebViewDataSent):
        return f"Data sent to bot: {action.text}", meta
    if isinstance(action, A.MessageActionBoostApply):
        meta["boosts"] = action.boosts
        return f"{actor} boosted the group", meta
    if isinstance(action, A.MessageActionRequestedPeer):
        return f"{actor} shared a chat with the bot", meta
    # Anything newer than this table: the action's own name, still readable
    return meta["action"].replace("_", " ").capitalize(), meta


def _poll_meta(media) -> Tuple[str, dict]:
    poll = media.poll
    results = getattr(media, "results", None)
    votes = {}
    for r in (getattr(results, "results", None) or []):
        votes[bytes(r.option)] = r.voters
    question = _twe(poll.question)
    options = [{"text": _twe(a.text), "voters": votes.get(bytes(a.option))} for a in poll.answers]
    lines = [f"{'Quiz' if poll.quiz else 'Poll'}: {question}"]
    for o in options:
        lines.append(f"• {o['text']}" + (f" ({o['voters']})" if o["voters"] is not None else ""))
    return "\n".join(lines), {
        "question": question,
        "options": options,
        "total_voters": getattr(results, "total_voters", None),
        "closed": bool(poll.closed),
        "quiz": bool(poll.quiz),
        "multiple_choice": bool(poll.multiple_choice),
    }


def media_meta(msg) -> Tuple[Optional[str], dict]:
    """(fallback text, meta) for content that isn't a file: polls, locations,
    venues, contacts, games, dice, stories, checklists, invoices, giveaways.
    The text is used only when the message has no caption of its own."""
    media = getattr(msg, "media", None)
    A = types
    if media is None or isinstance(media, A.MessageMediaEmpty):
        return None, {}
    if isinstance(media, A.MessageMediaPoll):
        text, poll = _poll_meta(media)
        return text, {"poll": poll}
    if isinstance(media, A.MessageMediaVenue):
        geo = media.geo
        venue = {"title": media.title, "address": media.address,
                 "lat": getattr(geo, "lat", None), "long": getattr(geo, "long", None)}
        return f"Venue: {media.title}" + (f", {media.address}" if media.address else ""), {"venue": venue}
    if isinstance(media, (A.MessageMediaGeo, A.MessageMediaGeoLive)):
        geo = media.geo
        lat, lon = getattr(geo, "lat", None), getattr(geo, "long", None)
        loc = {"lat": lat, "long": lon}
        label = "Location"
        if isinstance(media, A.MessageMediaGeoLive):
            loc["live_period"] = media.period
            label = "Live location"
        text = f"{label}: {lat:.5f}, {lon:.5f}" if lat is not None and lon is not None else label
        return text, {"location": loc}
    if isinstance(media, A.MessageMediaContact):
        name = f"{media.first_name or ''} {media.last_name or ''}".strip()
        contact = {"first_name": media.first_name, "last_name": media.last_name,
                   "phone": media.phone_number, "user_id": media.user_id or None}
        parts = [p for p in (name, media.phone_number) if p]
        return "Contact: " + (", ".join(parts) or "shared contact"), {"contact": contact}
    if isinstance(media, A.MessageMediaGame):
        title = getattr(media.game, "title", None) or "game"
        return f"Game: {title}", {"game": {"title": title}}
    if isinstance(media, A.MessageMediaDice):
        return f"{media.emoticon} {media.value}", {"dice": {"emoticon": media.emoticon, "value": media.value}}
    if isinstance(media, A.MessageMediaStory):
        story = {"peer_id": tl_utils.get_peer_id(media.peer) if media.peer else None, "id": media.id,
                 "via_mention": bool(media.via_mention)}
        caption = _twe(getattr(media.story, "caption", None)) if media.story is not None else ""
        return ("Story: " + caption) if caption else "Story", {"story": story}
    if isinstance(media, getattr(A, "MessageMediaToDo", ())):
        title = _twe(media.todo.title)
        items = [_twe(i.title) for i in media.todo.list]
        done = {c.id for c in (media.completions or [])}
        todo = {"title": title, "items": [{"text": _twe(i.title), "done": i.id in done} for i in media.todo.list]}
        return "\n".join([f"Checklist: {title}"] + [f"• {t}" for t in items]), {"todo": todo}
    if isinstance(media, A.MessageMediaInvoice):
        inv = {"title": media.title, "description": media.description,
               "currency": media.currency, "total_amount": media.total_amount}
        return f"Invoice: {media.title} ({media.total_amount / 100:.2f} {media.currency})", {"invoice": inv}
    if isinstance(media, A.MessageMediaGiveaway):
        return f"Giveaway: {media.quantity} prizes", {"giveaway": {"quantity": media.quantity}}
    if isinstance(media, A.MessageMediaUnsupported):
        return "Unsupported message (update Telegram to view)", {"unsupported": True}
    return None, {}


def forward_info(msg) -> Tuple[Optional[str], Optional[dict]]:
    """(display name of the original author, forward meta) or (None, None)."""
    hdr = getattr(msg, "fwd_from", None)
    if hdr is None:
        return None, None
    name = None
    fwd = getattr(msg, "forward", None)
    if fwd is not None:
        ent = getattr(fwd, "sender", None) or getattr(fwd, "chat", None)
        if ent is not None:
            name = _sender_name(ent)
    name = name or hdr.from_name or hdr.post_author or getattr(hdr, "saved_from_name", None)
    from_id = tl_utils.get_peer_id(hdr.from_id) if hdr.from_id else None
    meta = {
        "from_id": from_id,
        "from_name": name,
        "date": iso_utc(hdr.date),
        "channel_post": hdr.channel_post,
        "post_author": hdr.post_author,
    }
    return name, meta


def attachments_of(msg) -> list[dict]:
    """The message's file as an attachment list (Telegram: zero or one)."""
    kind = media_type(msg)
    if kind is None:
        return []
    doc = getattr(msg, "document", None)
    return [{
        "kind": kind,
        "filename": media_filename(msg),
        "mime_type": getattr(doc, "mime_type", None) if doc is not None else ("image/jpeg" if kind == "photo" else None),
        "size": media_size(msg),
        "external_ref": None,  # refetched by message id
    }]


def message_to_row(
    msg,
    conversation_id: int,
    sender_identity_id: Optional[int] = None,
    sender=None,
    *,
    forum: Optional[bool] = None,
) -> dict:
    """Build the Telegram-owned columns of a ``messages`` row (plus its
    ``attachments``) for a message of ``conversation_id``."""
    if forum is None:
        forum = _entity_is_forum(getattr(msg, "chat", None))
    if sender is None:
        sender = getattr(msg, "sender", None)
    text = raw_text(msg)
    text_html = entities_to_html(text, getattr(msg, "entities", None))
    topic_id, reply_to_id = topic_and_reply(msg, forum)

    meta: dict = {}
    kind = "message"
    if is_service(msg):
        kind = "service"
        text, meta["service"] = service_text(msg, sender)
        text_html = None
    else:
        fallback, extra = media_meta(msg)
        meta.update(extra)
        if not text and fallback:
            text, text_html = fallback, None
    fwd_name, fwd_meta = forward_info(msg)
    if fwd_meta is not None:
        meta["forward"] = fwd_meta
    # Posted "as" a channel or chat (send-as, anonymous admin): no identity,
    # but keep the peer for Shield (read back via ids.sender_peer_of).
    from_id = getattr(msg, "from_id", None)
    if isinstance(from_id, (types.PeerChannel, types.PeerChat)):
        meta["sender_peer"] = tl_utils.get_peer_id(from_id)
    return {
        "conversation_id": conversation_id,
        "external_id": str(msg.id),
        "seq": msg.id,
        "sender_identity_id": sender_identity_id,
        "sender_name": _sender_name(sender),
        "text": text,
        "text_html": text_html,
        "kind": kind,
        "meta_json": json.dumps(meta, ensure_ascii=False, default=str) if meta else None,
        "fwd_from_name": fwd_name,
        "reactions_json": extract_reactions(msg),
        "date": _naive_utc(msg.date) or datetime.utcnow(),
        "is_outgoing": bool(getattr(msg, "out", False)),
        "grouped_id": getattr(msg, "grouped_id", None),
        "reply_to_external_id": str(reply_to_id) if reply_to_id is not None else None,
        "topic_id": topic_id,
        "is_pinned": bool(getattr(msg, "pinned", False)),
        "is_edited": bool(getattr(msg, "edit_date", None)),
        # Anything that reached us from Telegram is already delivered.
        "delivery_state": "sent",
        "attachments": attachments_of(msg),
    }


async def rows_for(db, conversation_id: int, msgs, *, forum: bool) -> list[dict]:
    """Rows for a batch of storable messages of one conversation, with each
    sender's identity resolved (created on first sight)."""
    from backend.telegram.ids import sender_identity_ids

    msgs = [m for m in msgs if is_storable(m)]
    senders = await sender_identity_ids(db, msgs)
    return [message_to_row(m, conversation_id, senders.get(m.id), forum=forum) for m in msgs]
