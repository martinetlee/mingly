"""The only place Telegram ids meet internal ids.

| Telegram                     | v2                                                   |
|------------------------------|------------------------------------------------------|
| marked peer id (dialog id)   | ``conversations.external_id = str(peer_id)``         |
| message id                   | ``messages.external_id = str(id)``, ``seq = id``     |
| user id                      | ``identities(channel="telegram", external_id=str(id))`` |

Telethon code resolves peers / users through these helpers and hands
internal ids to everything else.
"""

import json
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.tl.types import Channel, User

from backend.db import crud
from backend.db.models import Account, Conversation, Identity
from backend.telegram.entities import display_name, is_deactivated, is_forum, user_full_name
from backend.people import identities as person_identities

CHANNEL = "telegram"


def peer_key(peer_id: int) -> str:
    return str(peer_id)


def message_key(tg_message_id: int) -> str:
    return str(tg_message_id)


def peer_id_of(conv: Conversation) -> int:
    """The Telegram peer id of a Telegram conversation."""
    return int(conv.external_id)


def tg_message_id_of(message) -> int:
    """The Telegram message id of a stored Telegram message."""
    return int(message.seq)


def conversation_kind(entity) -> str:
    """'direct' for users, 'channel' for broadcast channels, else 'group'.
    Supergroups are Channel objects with ``megagroup=True``."""
    if isinstance(entity, User):
        return "direct"
    if isinstance(entity, Channel):
        return "group" if getattr(entity, "megagroup", False) else "channel"
    return "group"


async def account(db: AsyncSession) -> Account:
    return await crud.get_or_create_channel_account(db, CHANNEL)


async def account_id(db: AsyncSession) -> int:
    return (await account(db)).id


def is_telegram(conv: Conversation) -> bool:
    return conv.account is not None and conv.account.channel == CHANNEL


async def conversation_for(db: AsyncSession, peer_id: int) -> Optional[Conversation]:
    return await crud.get_conversation_by_external(db, await account_id(db), peer_key(peer_id))


async def conversation_ids_for(db: AsyncSession, peer_ids: Iterable[int]) -> dict[int, int]:
    """{peer_id: conversation_id} for the peers that are stored."""
    keys = {peer_key(p): p for p in set(peer_ids)}
    if not keys:
        return {}
    acc = await account_id(db)
    out: dict[int, int] = {}
    ks = list(keys)
    for i in range(0, len(ks), 500):
        res = await db.execute(
            select(Conversation.external_id, Conversation.id).where(
                Conversation.account_id == acc, Conversation.external_id.in_(ks[i:i + 500])
            )
        )
        out.update({keys[ext]: cid for ext, cid in res.all()})
    return out


async def peer_ids_for(db: AsyncSession, conversation_ids: Iterable[int]) -> dict[int, int]:
    """{conversation_id: peer_id} for Telegram conversations among the ids."""
    convs = await crud.get_conversations(db, conversation_ids)
    return {cid: peer_id_of(c) for cid, c in convs.items() if c.account is not None and c.account.channel == CHANNEL}


def _user_profile(user) -> dict:
    """Profile fields of a Telethon User. A "min" user (seen in a group
    without full access) carries no phone or contact flag: leave those
    unknown instead of overwriting them with False."""
    profile = dict(
        display_name=user_full_name(user) or getattr(user, "username", None) or None,
        first_name=getattr(user, "first_name", None),
        last_name=getattr(user, "last_name", None),
        username=getattr(user, "username", None),
        is_bot=bool(getattr(user, "bot", False)),
        is_deleted=bool(getattr(user, "deleted", False)),
    )
    if not getattr(user, "min", False):
        profile.update(
            phone=getattr(user, "phone", None),
            is_contact=bool(getattr(user, "contact", False)),
            is_self=bool(getattr(user, "is_self", False)) or None,
        )
    return profile


async def ensure_user_identity(db: AsyncSession, user, *, commit: bool = False, **extra) -> Identity:
    """Identity for a Telethon User, created (with its person) on first sight
    and refreshed with whatever profile the entity carries."""
    return await person_identities.upsert_identity(
        db, channel=CHANNEL, external_id=str(user.id), commit=commit, **{**_user_profile(user), **extra}
    )


async def identity_for_user_id(db: AsyncSession, user_id: int, *, create: bool = True) -> Optional[Identity]:
    """Identity for a bare Telegram user id (no entity at hand)."""
    existing = await crud.get_identity_by_external(db, CHANNEL, str(user_id))
    if existing is not None or not create:
        return existing
    return await person_identities.upsert_identity(db, channel=CHANNEL, external_id=str(user_id), commit=False)


async def identity_ids_for_users(db: AsyncSession, user_ids: Iterable[int]) -> dict[int, int]:
    """{user_id: identity_id} for the users that are stored."""
    ext = await crud.identity_ids_by_external(db, CHANNEL, (str(u) for u in user_ids))
    return {int(k): v for k, v in ext.items()}


async def user_ids_for(db: AsyncSession, identity_ids: Iterable[int]) -> dict[int, int]:
    """{identity_id: user_id} for Telegram identities among the ids."""
    idents = await crud.get_identities(db, identity_ids)
    return {iid: int(i.external_id) for iid, i in idents.items() if i.channel == CHANNEL}


async def upsert_dialog(
    db: AsyncSession,
    peer_id: int,
    entity,
    *,
    title: Optional[str] = None,
    last_message_at: Optional[datetime] = None,
    commit: bool = True,
    **channel_fields,
) -> Conversation:
    """Insert or update the conversation for a Telegram peer from its entity.
    A user peer also records (and links) the user's identity."""
    peer_identity_id = None
    is_user = isinstance(entity, User)
    if is_user:
        peer_identity_id = (await ensure_user_identity(db, entity)).id
    fields = dict(
        is_forum=is_forum(entity),
        is_deactivated=is_deactivated(entity),
        slow_mode_seconds=getattr(entity, "slowmode_seconds", None),
        members_count=getattr(entity, "participants_count", None),
        peer_identity_id=peer_identity_id,
    )
    if is_user:
        fields.update(
            is_bot=bool(getattr(entity, "bot", False)),
            is_deleted_account=bool(getattr(entity, "deleted", False)),
            is_self=bool(getattr(entity, "is_self", False)),
        )
    fields.update(channel_fields)
    return await crud.upsert_conversation(
        db,
        account_id=await account_id(db),
        external_id=peer_key(peer_id),
        title=title or display_name(entity, peer_id),
        kind=conversation_kind(entity),
        username=getattr(entity, "username", None),
        last_message_at=last_message_at,
        commit=commit,
        **fields,
    )


def sender_peer_of(message) -> Optional[int]:
    """Marked Telegram peer id of the channel / chat a stored message was
    posted as (``meta_json["sender_peer"]``, set by convert.message_to_row),
    or None for user senders and messages stored before it was recorded."""
    raw = getattr(message, "meta_json", None)
    if not raw:
        return None
    try:
        peer = json.loads(raw).get("sender_peer")
    except (ValueError, AttributeError):
        return None
    return peer if isinstance(peer, int) and not isinstance(peer, bool) else None


async def sender_identity_ids(db: AsyncSession, msgs: Iterable) -> dict[int, Optional[int]]:
    """{telegram message id: sender identity id} for a batch of messages.
    User senders get (or refresh) their identity; chats posting as
    themselves (channel posts, anonymous admins) have none. Stored
    identities are read in one query and only written when the entity
    carries something new."""
    msgs = list(msgs)
    user_ids = set()
    for msg in msgs:
        sender = getattr(msg, "sender", None)
        uid = getattr(msg, "sender_id", None)
        if isinstance(sender, User):
            user_ids.add(sender.id)
        elif sender is None and uid is not None and uid > 0:
            user_ids.add(uid)
    known = await crud.identities_by_external(db, CHANNEL, [str(u) for u in user_ids])

    out: dict[int, Optional[int]] = {}
    by_user: dict[int, Optional[int]] = {}
    for msg in msgs:
        sender = getattr(msg, "sender", None)
        uid = getattr(msg, "sender_id", None)
        if isinstance(sender, User):
            if sender.id not in by_user:
                stored = known.get(str(sender.id))
                if stored is not None and not crud.profile_differs(stored, _user_profile(sender)):
                    by_user[sender.id] = stored.id
                else:
                    by_user[sender.id] = (await ensure_user_identity(db, sender)).id
            out[msg.id] = by_user[sender.id]
        elif sender is None and uid is not None and uid > 0:
            if uid not in by_user:
                stored = known.get(str(uid))
                ident = stored if stored is not None else await identity_for_user_id(db, uid)
                by_user[uid] = ident.id if ident is not None else None
            out[msg.id] = by_user[uid]
        else:
            out[msg.id] = None
    return out
