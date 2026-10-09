"""Builders for v2 rows in tests. Every area's tests create accounts,
conversations, people and messages through these so fixtures stay
consistent with the schema (docs/multichannel/SCHEMA_V2.md).

Telegram-flavoured defaults: a conversation's ``external_id`` is the peer id,
a message's ``external_id``/``seq`` the Telegram message id, an identity's
``external_id`` the user id.
"""

from datetime import datetime, timedelta
from typing import Optional

from backend.db import crud
from backend.db.models import Account, Conversation, Identity, Message, MessageAttachment

BASE_DATE = datetime(2026, 1, 1, 12, 0)


async def account(db, channel: str = "telegram") -> Account:
    acc = await crud.get_or_create_channel_account(db, channel)
    await db.commit()  # the helper only flushes; fixtures want it persisted
    return acc


async def identity(
    db, external_id="77", *, channel: str = "telegram", display_name: Optional[str] = "Alice", **profile
) -> Identity:
    """An identity (and its new person)."""
    return await crud.upsert_identity(
        db, channel=channel, external_id=str(external_id), display_name=display_name, **profile
    )


async def conversation(
    db,
    external_id="77",
    *,
    title: str = "Alice",
    kind: str = "direct",
    channel: str = "telegram",
    peer: Optional[Identity] = None,
    **fields,
) -> Conversation:
    """A conversation; ``direct`` ones get a peer identity (created from the
    same external id unless ``peer`` is given). Extra keyword arguments set
    columns directly (flags, last_activity_at, …)."""
    acc = await account(db, channel)
    if kind == "direct" and peer is None:
        peer = await identity(db, external_id, channel=channel, display_name=title)
    conv = await crud.upsert_conversation(
        db, account_id=acc.id, external_id=str(external_id), title=title, kind=kind,
        peer_identity_id=peer.id if peer is not None else None,
    )
    if fields:
        for k, v in fields.items():
            setattr(conv, k, v)
        await db.commit()
        await db.refresh(conv)
    return conv


async def message(
    db,
    conv: Conversation,
    seq: int,
    text: Optional[str] = "hi",
    *,
    sender: Optional[Identity] = None,
    date: Optional[datetime] = None,
    out: bool = False,
    attachments: Optional[list] = None,
    **fields,
) -> Message:
    """A message in ``conv`` with external id / seq ``seq``. Dates default to
    BASE_DATE + seq minutes so seq order is chronological."""
    row = dict(
        conversation_id=conv.id,
        external_id=str(seq),
        seq=seq,
        sender_identity_id=sender.id if sender is not None else None,
        sender_name=(sender.display_name if sender is not None else None),
        text=text,
        date=date or BASE_DATE + timedelta(minutes=seq),
        is_outgoing=out,
        **fields,
    )
    if attachments is not None:
        row["attachments"] = attachments
    return await crud.save_message(db, **row)


def photo(**kw) -> dict:
    """An attachment dict for ``message(attachments=[...])``."""
    return {"kind": "photo", "filename": None, "mime_type": "image/jpeg", "size": 1000, **kw}


async def attachment(db, msg: Message) -> MessageAttachment:
    await db.refresh(msg)
    return msg.attachments[0]
