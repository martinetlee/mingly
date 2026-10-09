"""Shield's bridge to channel ids.

Shield works on internal ids. Two things still speak Telegram ids: the
known-scammer signatures / ``are_flagged_scams`` lookups (Telegram user ids)
and the current cloud contract (peer ids, message ids, user ids). Both go
through here, which delegates to ``backend.telegram.ids`` — the only place
Telegram ids meet internal ids — and only ever for Telegram rows.
"""

from typing import Iterable

from sqlalchemy.ext.asyncio import AsyncSession

TELEGRAM = "telegram"


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
