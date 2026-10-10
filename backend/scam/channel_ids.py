"""Shield's bridge to channel ids.

Shield works on internal ids. Two things still speak Telegram ids: the
known-scammer signatures / ``are_flagged_scams`` lookups (Telegram user ids)
and the cloud contract v1 (peer ids, message ids, user ids). Both go
through here, which delegates to ``backend.telegram.ids`` — the only place
Telegram ids meet internal ids — and only ever for Telegram rows.

Cloud contract v2 (docs/multichannel/SHIELD_CLOUD_CONTRACT_V2.md) is
channel-neutral: channels whose ids are public identifiers already sent by v1
(``PUBLIC_ID_CHANNELS``) keep sending them as ``id``; every other handle is an
HMAC-SHA256 under a per-install salt kept in the vault (``handle_hash``), and
their conversations / messages are opaque per-install ``ref``s.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend import secrets as vault
from backend.db.models import Account, Conversation, Identity, Message
from backend.telegram import ids as tg_ids

TELEGRAM = "telegram"

# Channels whose user / chat / message ids the cloud already receives in v1
PUBLIC_ID_CHANNELS = frozenset({TELEGRAM})

HANDLE_SALT_KEY = "shield_cloud_handle_salt"


async def telegram_user_ids(db: AsyncSession, identity_ids: Iterable[int]) -> dict[int, int]:
    """{identity_id: Telegram user id} for the Telegram identities among the ids."""
    ids = [i for i in set(identity_ids) if i]
    if not ids:
        return {}

    return await tg_ids.user_ids_for(db, ids)


async def identity_ids_for_telegram_users(db: AsyncSession, user_ids: Iterable[int]) -> dict[int, int]:
    """{Telegram user id: identity_id} for the users that are stored."""
    ids = [u for u in set(user_ids) if u]
    if not ids:
        return {}

    return await tg_ids.identity_ids_for_users(db, ids)


async def telegram_peer_ids(db: AsyncSession, conversation_ids: Iterable[int]) -> dict[int, int]:
    """{conversation_id: Telegram peer id} for the Telegram conversations among the ids."""
    ids = [c for c in set(conversation_ids) if c]
    if not ids:
        return {}

    return await tg_ids.peer_ids_for(db, ids)


def telegram_message_id(message) -> int:
    """The Telegram message id of a stored message in a Telegram conversation."""

    return tg_ids.tg_message_id_of(message)


@dataclass
class TelegramScanIds:
    """Telegram ids of a Telegram conversation's messages, for the payloads
    that still speak them (external detection API v1, the local prompt,
    known-scammer signatures)."""
    peer_id: int
    message_ids: dict  # message_id -> Telegram message id
    senders: dict  # message_id -> Telegram sender id (user id, or the chat posting as itself) or None


async def telegram_scan_ids(db: AsyncSession, conversation, messages) -> Optional["TelegramScanIds"]:
    """Telegram ids for ``messages`` of ``conversation``; None when it is not
    a Telegram conversation.

    The sender is what Telethon's ``msg.sender_id`` was: the user; else the
    channel / chat the message was posted as (``ids.sender_peer_of``); else,
    for a post without a recorded sender (broadcast channel posts, anonymous
    group admins, whose sender name is the chat's title, and rows stored
    before the sender peer was recorded), the chat itself."""
    if conversation is None or conversation.account is None or conversation.account.channel != TELEGRAM:
        return None

    users = await tg_ids.user_ids_for(db, {m.sender_identity_id for m in messages if m.sender_identity_id})
    peer = tg_ids.peer_id_of(conversation)
    senders = {m.id: _telegram_sender(conversation, peer, m, users) for m in messages}
    return TelegramScanIds(peer, {m.id: tg_ids.tg_message_id_of(m) for m in messages}, senders)


def _telegram_sender(conversation, peer: int, message, users: dict) -> Optional[int]:

    if message.sender_identity_id:
        return users.get(message.sender_identity_id)
    posted_as = tg_ids.sender_peer_of(message)
    if posted_as is not None:
        return posted_as
    if conversation.kind == "channel" or (message.sender_name and message.sender_name == conversation.title):
        return peer
    return None


def telegram_sender_condition(conversation, sender: int):
    """SQL form of ``_telegram_sender`` for identity-less messages of a
    Telegram ``conversation``: the messages whose Telegram sender is
    ``sender`` (a channel / chat peer id; user senders have identities)."""
    from sqlalchemy import and_, func, or_


    posted_as = func.json_extract(Message.meta_json, "$.sender_peer")
    no_identity = Message.sender_identity_id.is_(None)
    match = and_(no_identity, posted_as == sender)
    if sender != tg_ids.peer_id_of(conversation):
        return match
    as_chat = posted_as.is_(None) if conversation.kind == "channel" else and_(
        posted_as.is_(None), Message.sender_name == conversation.title,
    )
    return or_(match, and_(no_identity, as_chat))


# ---------------------------------------------------------------------------
# Cloud contract v2: install salt, hashed handles, opaque refs
# ---------------------------------------------------------------------------

def handle_salt() -> bytes:
    """The per-install salt for v2 handle hashes, created in the vault on
    first use. Resetting the vault makes a new salt (a new install for the
    cloud). Raises RuntimeError while the vault is locked."""

    stored = vault.get_secret(HANDLE_SALT_KEY)
    if stored:
        return bytes.fromhex(stored)
    if vault.vault_status() != "unlocked":
        raise RuntimeError("Vault is locked")
    salt = os.urandom(32)
    vault.save_secret(HANDLE_SALT_KEY, salt.hex())
    return salt


def _hmac(salt: bytes, text: str) -> str:
    return hmac.new(salt, text.encode("utf-8"), hashlib.sha256).hexdigest()


def handle_hash(salt: bytes, channel: str, external_id) -> str:
    """HMAC-SHA256 of ``"<channel>:<external_id>"`` (external id trimmed and
    lowercased: email addresses, E.164 numbers, Signal ACIs)."""
    return _hmac(salt, f"{channel}:{str(external_id).strip().lower()}")


def opaque_ref(salt: bytes, kind: str, internal_id: int) -> str:
    """Stable per-install id of a conversation or message (``kind``)."""
    return _hmac(salt, f"ref/{kind}/{internal_id}")


def handle_object(salt: bytes, channel: str, external_id) -> dict:
    """``{"id"}`` on public-id channels, ``{"handle_hash"}`` otherwise."""
    if channel in PUBLIC_ID_CHANNELS:
        return {"id": str(external_id)}
    return {"handle_hash": handle_hash(salt, channel, external_id)}


@dataclass
class CloudRefs:
    """v2 objects of the given rows; deleted rows are absent from the maps."""
    handles: dict = field(default_factory=dict)  # identity_id -> handle object
    identity_channels: dict = field(default_factory=dict)  # identity_id -> channel
    conversations: dict = field(default_factory=dict)  # conversation_id -> {"kind", "id"|"ref"}
    conversation_channels: dict = field(default_factory=dict)  # conversation_id -> channel
    messages: dict = field(default_factory=dict)  # message_id -> {"id"|"ref"}
    message_conversations: dict = field(default_factory=dict)  # message_id -> conversation_id

    def channel_of(self, *, conversation_id=None, message_id=None, identity_id=None) -> Optional[str]:
        """The row's channel, by its conversation, then message, then identity."""
        if conversation_id in self.conversation_channels:
            return self.conversation_channels[conversation_id]
        if message_id in self.message_conversations:
            return self.conversation_channels.get(self.message_conversations[message_id])
        return self.identity_channels.get(identity_id)


async def cloud_refs(
    db: AsyncSession, salt: bytes, *, identity_ids=(), conversation_ids=(), message_ids=()
) -> CloudRefs:
    """v2 handle / conversation / message objects for the given internal ids."""

    refs = CloudRefs()
    iids = [i for i in set(identity_ids) if i]
    if iids:
        for iid, channel, ext in (await db.execute(
            select(Identity.id, Identity.channel, Identity.external_id).where(Identity.id.in_(iids))
        )).all():
            refs.handles[iid] = handle_object(salt, channel, ext)
            refs.identity_channels[iid] = channel

    mids = [m for m in set(message_ids) if m]
    msg_rows = (await db.execute(
        select(Message.id, Message.conversation_id, Message.seq).where(Message.id.in_(mids))
    )).all() if mids else []
    cids = {c for c in set(conversation_ids) if c} | {r.conversation_id for r in msg_rows}
    if cids:
        for cid, kind, ext, channel in (await db.execute(
            select(Conversation.id, Conversation.kind, Conversation.external_id, Account.channel)
            .join(Account, Account.id == Conversation.account_id).where(Conversation.id.in_(cids))
        )).all():
            ident = {"id": str(ext)} if channel in PUBLIC_ID_CHANNELS else {"ref": opaque_ref(salt, "conversation", cid)}
            refs.conversations[cid] = {"kind": kind, **ident}
            refs.conversation_channels[cid] = channel
    for r in msg_rows:
        channel = refs.conversation_channels.get(r.conversation_id)
        if channel is None:
            continue
        refs.message_conversations[r.id] = r.conversation_id
        refs.messages[r.id] = (
            {"id": str(telegram_message_id(r))} if channel == TELEGRAM else {"ref": opaque_ref(salt, "message", r.id)}
        )
    return refs
