"""Signed-in channel accounts."""

from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Account

__all__ = ["get_account", "get_accounts", "get_or_create_channel_account", "set_account_owner"]


async def get_account(db: AsyncSession, account_id: int) -> Optional[Account]:
    return (await db.execute(select(Account).where(Account.id == account_id))).scalar_one_or_none()


async def get_accounts(db: AsyncSession, channel: Optional[str] = None) -> list[Account]:
    q = select(Account).order_by(Account.id)
    if channel is not None:
        q = q.where(Account.channel == channel)
    return list((await db.execute(q)).scalars().all())


async def get_or_create_channel_account(db: AsyncSession, channel: str) -> Account:
    """The account of a single-account channel (the oldest one), created on
    first use. Race-free: concurrent first calls insert against
    ``uq_account_channel_unclaimed`` and all read back the same row. Flushes,
    doesn't commit (the caller's transaction decides). Multi-account channels
    look accounts up by id instead."""
    async def _oldest() -> Optional[Account]:
        return (await db.execute(
            select(Account).where(Account.channel == channel).order_by(Account.id).limit(1)
        )).scalar_one_or_none()

    existing = await _oldest()
    if existing is not None:
        return existing
    await db.execute(
        sqlite_insert(Account).values(channel=channel, created_at=datetime.utcnow())
        .on_conflict_do_nothing(index_elements=[Account.channel], index_where=Account.external_id.is_(None))
    )
    account = await _oldest()
    assert account is not None
    return account


async def set_account_owner(
    db: AsyncSession, account_id: int, *, external_id: str, label: Optional[str], self_identity_id: Optional[int]
) -> Optional[Account]:
    """Record who is signed in on the account (after sign-in or on start)."""
    account = await get_account(db, account_id)
    if account is None:
        return None
    account.external_id = external_id
    account.label = label
    account.self_identity_id = self_identity_id
    await db.commit()
    await db.refresh(account)
    return account
