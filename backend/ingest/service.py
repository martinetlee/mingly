"""The one writer for channels built on normalized shapes (Gmail, Signal, …).

``ingest`` upserts a conversation and its messages, records every sender /
participant as an identity (and person), links identities across channels
(``backend.people.linking``), persists through ``crud.save_messages`` and
fans live messages out like Telegram's (``backend.ingest.fanout``).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Message, MessageParticipant
from backend.ingest import fanout
from backend.ingest.normalized import NormalizedConversation, NormalizedIdentity, NormalizedMessage
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

_PROFILE_FIELDS = ("display_name", "first_name", "last_name", "username", "phone", "is_contact", "is_self", "is_bot")


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


async def upsert_conversation(
    db: AsyncSession, account_id: int, channel: str, conv: NormalizedConversation,
) -> Conversation:
    peer_id = await upsert_identity(db, channel, conv.peer) if conv.peer is not None else None
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
    if conv.labels is not None:
        stored.labels_json = json.dumps(sorted(set(conv.labels)))
    return stored


async def _row(db: AsyncSession, channel: str, conversation_id: int, msg: NormalizedMessage) -> dict:
    sender_id = await upsert_identity(db, channel, msg.sender) if msg.sender is not None else None
    sender_name = msg.sender_name
    if sender_name is None and msg.sender is not None:
        sender_name = msg.sender.display_name or msg.sender.username or msg.sender.external_id
    return {
        "conversation_id": conversation_id,
        "external_id": msg.external_id,
        "seq": _seq(msg),
        "sender_identity_id": sender_id,
        "sender_name": sender_name,
        "subject": msg.subject,
        "text": msg.text,
        "text_html": msg.text_html,
        "date": msg.date,
        "is_outgoing": msg.is_outgoing,
        "is_edited": msg.is_edited,
        "kind": msg.kind,
        "reply_to_external_id": msg.reply_to_external_id,
        "meta_json": json.dumps(msg.meta, ensure_ascii=False, default=str) if msg.meta else None,
        "reactions_json": msg.reactions_json,
        "delivery_state": msg.delivery_state,
        "attachments": [
            {"kind": a.kind, "filename": a.filename, "mime_type": a.mime_type, "size": a.size,
             "external_ref": a.external_ref}
            for a in msg.attachments
        ],
    }


async def _sync_participants(db: AsyncSession, channel: str, message_id: int, msg: NormalizedMessage) -> None:
    wanted = set()
    if msg.sender is not None:
        wanted.add((await upsert_identity(db, channel, msg.sender), "from"))
    for ident, role in msg.participants:
        wanted.add((await upsert_identity(db, channel, ident), role))
    current = {
        (p.identity_id, p.role): p.id for p in (await db.execute(
            select(MessageParticipant).where(MessageParticipant.message_id == message_id)
        )).scalars().all()
    }
    for identity_id, role in wanted - set(current):
        db.add(MessageParticipant(message_id=message_id, identity_id=identity_id, role=role))
    stale = [pid for key, pid in current.items() if key not in wanted]
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
        conv = await upsert_conversation(db, account_id, channel, conversation)
        await db.flush()
        rows = [await _row(db, channel, conv.id, m) for m in msgs]
        res = await crud.save_messages(db, rows, commit=False)
        if any(m.participants or m.sender for m in msgs):
            ids = await crud.message_ids_by_external(db, conv.id, [m.external_id for m in msgs])
            for m in msgs:
                if m.external_id in ids:
                    await _sync_participants(db, channel, ids[m.external_id], m)
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
        await db.execute(delete(Message).where(Message.id.in_(list(by_ext.values()))))
        await db.commit()
    await ws_manager.broadcast({
        "type": "messages_deleted", "conversation_id": conv.id, "message_ids": sorted(by_ext.values()),
    })


async def message_participants(db: AsyncSession, message_ids: Iterable[int]) -> dict[int, list[dict]]:
    """``{message_id: [{identity_id, person_id, role, display_name, external_id}]}``."""
    from backend.db.models import Identity

    ids = list(set(message_ids))
    out: dict[int, list[dict]] = {}
    if not ids:
        return out
    rows = (await db.execute(
        select(MessageParticipant.message_id, MessageParticipant.role, Identity)
        .join(Identity, Identity.id == MessageParticipant.identity_id)
        .where(MessageParticipant.message_id.in_(ids))
        .order_by(MessageParticipant.id)
    )).all()
    for mid, role, ident in rows:
        out.setdefault(mid, []).append({
            "identity_id": ident.id, "person_id": ident.person_id, "role": role,
            "display_name": ident.display_name, "external_id": ident.external_id,
        })
    return out

