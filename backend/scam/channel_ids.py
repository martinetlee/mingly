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

TELEGRAM = "telegram"

# Channels whose user / chat / message ids the cloud already receives in v1
PUBLIC_ID_CHANNELS = frozenset({TELEGRAM})

HANDLE_SALT_KEY = "shield_cloud_handle_salt"


async def telegram_user_ids(db: AsyncSession, identity_ids: Iterable[int]) -> dict[int, int]:
    """{identity_id: Telegram user id} for the Telegram identities among the ids."""
    ids = [i for i in set(identity_ids) if i]
    if not ids:
        return {}
    from backend.telegram import ids as tg_ids

    return await tg_ids.user_ids_for(db, ids)


async def identity_ids_for_telegram_users(db: AsyncSession, user_ids: Iterable[int]) -> dict[int, int]:
    """{Telegram user id: identity_id} for the users that are stored."""
    ids = [u for u in set(user_ids) if u]
    if not ids:
        return {}
    from backend.telegram import ids as tg_ids

    return await tg_ids.identity_ids_for_users(db, ids)


async def telegram_peer_ids(db: AsyncSession, conversation_ids: Iterable[int]) -> dict[int, int]:
    """{conversation_id: Telegram peer id} for the Telegram conversations among the ids."""
    ids = [c for c in set(conversation_ids) if c]
    if not ids:
        return {}
    from backend.telegram import ids as tg_ids

    return await tg_ids.peer_ids_for(db, ids)


def telegram_message_id(message) -> int:
    """The Telegram message id of a stored message in a Telegram conversation."""
    from backend.telegram import ids as tg_ids

    return tg_ids.tg_message_id_of(message)


# ---------------------------------------------------------------------------
# Cloud contract v2: install salt, hashed handles, opaque refs
# ---------------------------------------------------------------------------

def handle_salt() -> bytes:
    """The per-install salt for v2 handle hashes, created in the vault on
    first use. Resetting the vault makes a new salt (a new install for the
    cloud). Raises RuntimeError while the vault is locked."""
    from backend import secrets as vault

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
    from backend.db.models import Account, Conversation, Identity, Message

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
