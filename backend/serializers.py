"""JSON shapes shared by every route and WebSocket event
(docs/multichannel/SCHEMA_V2.md, "Payload shapes"). Build payloads only
through these so the frontend sees one shape per entity."""

import json
from typing import Any, Iterable, Optional

from backend.db import crud
from backend.db.models import Conversation, Identity, Message, MessageAttachment, Person
from backend.serialization import iso_utc


def media_url(rel_path: Optional[str]) -> Optional[str]:
    """URL of a file stored under DATA_DIR (served by /api/media)."""
    return f"/api/media/{rel_path}" if rel_path else None


def parse_json(raw: Optional[str]) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def attachment_out(a: MessageAttachment) -> dict:
    return {
        "id": a.id,
        "kind": a.kind,
        "filename": a.filename,
        "mime_type": a.mime_type,
        "size": a.size,
        "url": media_url(a.local_path),
    }


def identity_out(i: Identity) -> dict:
    return {
        "id": i.id,
        "person_id": i.person_id,
        "channel": i.channel,
        "external_id": i.external_id,
        "display_name": i.display_name,
        "first_name": i.first_name,
        "last_name": i.last_name,
        "username": i.username,
        "phone": i.phone,
        "bio": i.bio,
        "avatar_url": media_url(i.avatar_path),
        "is_contact": bool(i.is_contact),
        "is_bot": bool(i.is_bot),
        "is_deleted": bool(i.is_deleted),
        "is_self": bool(i.is_self),
    }


def sender_out(m: Message) -> Optional[dict]:
    ident = m.sender_identity
    if ident is None:
        if m.sender_name is None:
            return None
        return {"identity_id": None, "person_id": None, "name": m.sender_name, "avatar_url": None}
    return {
        "identity_id": ident.id,
        "person_id": ident.person_id,
        "name": m.sender_name or ident.display_name or ident.username,
        "avatar_url": media_url(ident.avatar_path),
    }


def scam_fields(m: Message) -> dict:
    """Scam state of one message as stored, plus the derived effective verdict
    (the one serialization of it, used by every API response and WS event)."""
    return {
        "scam_score": m.scam_score,
        "scam_reason": m.scam_reason,
        "scam_category": m.scam_category,
        "scam_manual_verdict": m.scam_manual_verdict,
        "scam_status": m.scam_status,
        "scam_error": m.scam_error,
        "effective_is_scam": bool(m.effective_is_scam),
    }


def message_out(row, participants: Optional[list] = None) -> dict:
    """A Message, or a reply-preview row ``(Message, reply_id,
    reply_sender_name, reply_text, reply_media_type)`` from
    ``crud.with_reply_preview``. ``participants`` are the message's
    from/to/cc/bcc identities (``crud.message_participants``; channels
    without them get an empty list)."""
    if isinstance(row, Message):
        m, reply_id, reply_sender, reply_text, reply_kind = row, None, None, None, None
    else:
        m, reply_id, reply_sender, reply_text, reply_kind = row[0], row[1], row[2], row[3], row[4]
    reply_preview = None
    if m.reply_to_external_id is not None and reply_id is not None:
        reply_preview = {"sender_name": reply_sender, "text": reply_text, "media_type": reply_kind}
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "external_id": m.external_id,
        "seq": m.seq,
        "sender": sender_out(m),
        "subject": m.subject,
        "text": m.text,
        "text_html": m.text_html,
        "date": iso_utc(m.date),
        "is_outgoing": m.is_outgoing,
        "kind": m.kind or "message",
        "meta": parse_json(m.meta_json),
        "attachments": [attachment_out(a) for a in m.attachments],
        "reactions": parse_json(m.reactions_json),
        "my_reaction": m.my_reaction,
        "importance_score": m.importance_score,
        "user_flagged": m.user_flagged,
        "reply_to_message_id": reply_id,
        "reply_to_external_id": m.reply_to_external_id,
        "reply_to_preview": reply_preview,
        "topic_id": m.topic_id,
        "is_pinned": m.is_pinned,
        "grouped_id": m.grouped_id,
        "delivery_state": m.delivery_state or "sent",
        "is_edited": m.is_edited,
        "fwd_from_name": m.fwd_from_name,
        "participants": participants or [],
        **scam_fields(m),
    }


async def messages_out(db, rows: list) -> list[dict]:
    """``message_out`` for a batch, with participants loaded in one query."""
    ids = [r.id if isinstance(r, Message) else r[0].id for r in rows]
    parts = await crud.message_participants(db, ids)
    return [message_out(r, parts.get(i)) for r, i in zip(rows, ids)]


def channel_of(conv: Conversation) -> Optional[str]:
    return conv.account.channel if conv.account is not None else None


def conversation_out(conv: Conversation, *, capabilities: Iterable[str], with_last_message: bool = True) -> dict:
    """``capabilities`` are what the conversation's connector supports in it
    (runtime state the caller resolves, e.g. ``api.views.conversation_json``)."""
    peer = conv.peer_identity
    avatar = conv.avatar_path or (peer.avatar_path if peer is not None else None)
    title = conv.title
    if (not title or title.isdigit()) and conv.username:
        title = conv.username
    d = {
        "id": conv.id,
        "account_id": conv.account_id,
        "channel": channel_of(conv),
        "kind": conv.kind,
        "title": title,
        "username": conv.username,
        "avatar_url": media_url(avatar),
        "peer": {"person_id": peer.person_id, "identity_id": peer.id} if peer is not None else None,
        "is_monitored": conv.is_monitored,
        "is_hidden": conv.is_hidden,
        "is_briefing": conv.is_briefing,
        "is_tracked": conv.is_tracked,
        "is_forum": bool(conv.is_forum),
        "slow_mode_seconds": conv.slow_mode_seconds or 0,
        "is_pinned": conv.is_pinned,
        "is_archived": conv.is_archived,
        "is_bot": bool(conv.is_bot),
        "is_deleted_account": bool(conv.is_deleted_account),
        "is_self": bool(conv.is_self),
        "history_complete": bool(conv.history_complete),
        "is_muted": conv.is_muted,
        "muted_until": iso_utc(conv.muted_until),
        "is_marked_unread": conv.is_marked_unread,
        "unread_count": conv.unread_count or 0,
        "members_count": conv.members_count,
        "last_synced_at": iso_utc(conv.last_synced_at),
        "last_activity_at": iso_utc(conv.last_activity_at),
        "capabilities": sorted(capabilities),
        "labels": parse_json(conv.labels_json) or [],
    }
    if with_last_message and (conv.last_message_text is not None or conv.last_message_at is not None):
        text = conv.last_message_text or ""
        d["last_message"] = {
            "text": text[:100],
            "sender_name": conv.last_message_sender,
            "date": iso_utc(conv.last_message_at),
        }
    return d


def person_out(person: Person, identities: Iterable[Identity]) -> dict:
    """Core person shape; CRM routes add positions/notes/tags on top."""
    idents = list(identities)
    avatar = next((i.avatar_path for i in idents if i.avatar_path), None)
    return {
        "id": person.id,
        "name": person.name,
        "display_name": person.display_name,
        "avatar_url": media_url(avatar),
        "is_contact": bool(person.is_contact),
        "is_important": bool(person.is_important),
        "is_self": bool(person.is_self),
        "identities": [identity_out(i) for i in idents],
        "twitter_handle": person.twitter_handle,
        "linkedin_url": person.linkedin_url,
        "notes": person.notes,
        "relationship_status": person.relationship_status,
        "relationship_status_manual": bool(person.relationship_status_manual),
        "last_interaction_at": iso_utc(person.last_interaction_at),
        "last_outgoing_at": iso_utc(person.last_outgoing_at),
        "last_incoming_at": iso_utc(person.last_incoming_at),
    }
