"""The one writer for channels built on normalized shapes (Gmail, Signal, …).

``ingest`` upserts a conversation and its messages, records every sender /
participant as an identity (and person), links identities across channels
(``backend.people.linking``), persists through ``crud.save_messages`` and
fans live messages out like Telegram's (``backend.ingest.fanout``).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, MessageParticipant
from backend.ingest import fanout
from backend.ingest.normalized import NormalizedConversation, NormalizedIdentity, NormalizedMessage
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

_PROFILE_FIELDS = ("display_name", "first_name", "last_name", "username", "phone", "is_contact", "is_self", "is_bot")
# Message fields where None means "not known in this delivery": left out of
# the upsert row so the stored value is kept (see backend.ingest.normalized).
_KEEP_IF_NONE = ("text", "subject", "text_html", "reply_to_external_id", "reactions_json")


@dataclass
class IngestResult:
    conversation_id: int
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)


def _seq(msg: NormalizedMessage) -> int:
    if msg.seq is not None:
        return msg.seq
    from calendar import timegm
    return timegm(msg.date.timetuple()) * 1000 + msg.date.microsecond // 1000


async def upsert_identity(db: AsyncSession, channel: str, ident: NormalizedIdentity) -> int:
    """Store / refresh an identity; returns its id (linking to an existing
    person happens inside ``crud.upsert_identity``)."""
    profile = {f: getattr(ident, f) for f in _PROFILE_FIELDS}
    stored = await crud.upsert_identity(db, channel=channel, external_id=ident.external_id, commit=False, **profile)
    return stored.id


class _Identities:
    """Identity resolution for one ``ingest`` call: the same handle with the
    same profile (a sender who is also on every message's From line) is
    written once, not once per message and role."""

    def __init__(self, db: AsyncSession, channel: str):
        self.db, self.channel = db, channel
        self._ids: dict[tuple, int] = {}

    async def id_of(self, ident: NormalizedIdentity) -> int:
        key = (ident.external_id,) + tuple(getattr(ident, f) for f in _PROFILE_FIELDS)
        if key not in self._ids:
            self._ids[key] = await upsert_identity(self.db, self.channel, ident)
        return self._ids[key]


async def upsert_conversation(
    db: AsyncSession, account_id: int, channel: str, conv: NormalizedConversation,
    identities: Optional[_Identities] = None,
) -> Conversation:
    peer_id = None
    if conv.peer is not None and not conv.clear_peer:
        peer_id = (await identities.id_of(conv.peer) if identities is not None
                   else await upsert_identity(db, channel, conv.peer))
    stored = await crud.upsert_conversation(
        db,
        account_id=account_id,
        external_id=conv.external_id,
        title=conv.title,
        kind=conv.kind,
        last_message_at=conv.last_message_at,
        commit=False,
        peer_identity_id=peer_id,
        members_count=conv.members_count,
        unread_count=conv.unread_count,
        is_archived=conv.is_archived,
    )
    if conv.clear_peer and stored.peer_identity_id is not None:
        stored.peer_identity_id = None
    if conv.labels is not None:
        stored.labels_json = json.dumps(sorted(set(conv.labels)))
    return stored


async def _row(identities: _Identities, conversation_id: int, msg: NormalizedMessage) -> dict:
    sender_id = await identities.id_of(msg.sender) if msg.sender is not None else None
    sender_name = msg.sender_name
    if sender_name is None and msg.sender is not None:
        sender_name = msg.sender.display_name or msg.sender.username or msg.sender.external_id
    row: dict = {
        "conversation_id": conversation_id,
        "external_id": msg.external_id,
        "seq": _seq(msg),
        "sender_identity_id": sender_id,
        "sender_name": sender_name,
        "date": msg.date,
        "is_outgoing": msg.is_outgoing,
        "is_edited": msg.is_edited,
        "kind": msg.kind,
        "delivery_state": msg.delivery_state,
    }
    for name in _KEEP_IF_NONE:
        value = getattr(msg, name)
        if value is not None:
            row[name] = value
    if msg.meta is not None:
        row["meta_json"] = json.dumps(msg.meta, ensure_ascii=False, default=str) if msg.meta else None
    if msg.attachments is not None:
        row["attachments"] = [
            {"kind": a.kind, "filename": a.filename, "mime_type": a.mime_type, "size": a.size,
             "external_ref": a.external_ref}
            for a in msg.attachments
        ]
    return row


async def _sync_participants(db: AsyncSession, identities: _Identities, message_id: int, msg: NormalizedMessage) -> None:
    """Make the stored from/to/cc/bcc rows match the message. Only the roles
    this delivery knows are touched: "from" when it carries a sender, the
    others when ``participants`` is not None."""
    wanted = set()
    if msg.sender is not None:
        wanted.add((await identities.id_of(msg.sender), "from"))
    for ident, role in msg.participants or ():
        wanted.add((await identities.id_of(ident), role))
    current = {
        (p.identity_id, p.role): p.id for p in (await db.execute(
            select(MessageParticipant).where(MessageParticipant.message_id == message_id)
        )).scalars().all()
    }

    def known(role: str) -> bool:
        return msg.sender is not None if role == "from" else msg.participants is not None

    for identity_id, role in wanted - set(current):
        db.add(MessageParticipant(message_id=message_id, identity_id=identity_id, role=role))
    stale = [pid for key, pid in current.items() if key not in wanted and known(key[1])]
    if stale:
        await db.execute(delete(MessageParticipant).where(MessageParticipant.id.in_(stale)))


async def ingest(
    account_id: int,
    channel: str,
    conversation: NormalizedConversation,
    messages: Iterable[NormalizedMessage],
    *,
    source: str = "backfill",
) -> IngestResult:
    """Persist one conversation and its messages in one transaction.

    ``source="realtime"`` fans newly created messages out (WS delta, Shield,
    catch-up, notifications); backfill only announces the conversation
    changed."""
    msgs = list(messages)
    async with AsyncSessionLocal() as db:
        identities = _Identities(db, channel)
        conv = await upsert_conversation(db, account_id, channel, conversation, identities)
        await db.flush()
        rows = [await _row(identities, conv.id, m) for m in msgs]
        res = await crud.save_messages(db, rows, commit=False)
        if any(m.participants is not None or m.sender is not None for m in msgs):
            ids = await crud.message_ids_by_external(db, conv.id, [m.external_id for m in msgs])
            for m in msgs:
                if m.external_id in ids:
                    await _sync_participants(db, identities, ids[m.external_id], m)
        await db.commit()
        result = IngestResult(conv.id, list(res["created"]), list(res["updated"]))
        created = await crud.get_messages_by_ids(db, result.created)
        conv = await crud.get_conversation(db, conv.id)

    if source == "realtime":
        for mid in result.created:
            await fanout.message_received(conv, created[mid])
    elif result.created or result.updated:
        await ws_manager.broadcast({"type": "conversations_synced", "conversation_ids": [conv.id]})
    return result


async def delete_messages(account_id: int, conversation_external_id: str, message_external_ids: Iterable[str]) -> None:
    """Remove messages the channel reports deleted and tell the frontend."""
    async with AsyncSessionLocal() as db:
        conv = await crud.get_conversation_by_external(db, account_id, conversation_external_id)
        if conv is None:
            return
        by_ext = await crud.message_ids_by_external(db, conv.id, message_external_ids)
        if not by_ext:
            return
        by_conv = await crud.delete_messages(db, by_ext.values())
    for conversation_id, message_ids in by_conv.items():
        await ws_manager.broadcast({
            "type": "messages_deleted", "conversation_id": conversation_id, "message_ids": message_ids,
        })



