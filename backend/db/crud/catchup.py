"""Catch-up triage items (snooze / handled / note per conversation) and the
stored catch-up summaries."""

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import CatchUpItem, CatchUpSummary, Message

__all__ = [
    "get_or_create_catchup_item", "list_active_catchup_items", "list_catchup_items",
    "catchup_summaries", "reopen_handled_catchup",
]


async def get_or_create_catchup_item(db: AsyncSession, conversation_id: int, person_id: Optional[int]) -> CatchUpItem:
    """The conversation's item, created pending on first use (INSERT OR
    IGNORE: two concurrent first actions can't collide on the unique
    conversation_id). ``person_id`` is refreshed (the peer may have been
    linked to another person since). Doesn't commit."""
    now = datetime.utcnow()
    await db.execute(
        sqlite_insert(CatchUpItem)
        .values(conversation_id=conversation_id, person_id=person_id, status="pending",
                created_at=now, updated_at=now)
        .on_conflict_do_nothing(index_elements=["conversation_id"])
    )
    item = (await db.execute(
        select(CatchUpItem).where(CatchUpItem.conversation_id == conversation_id)
    )).scalar_one()
    item.person_id = person_id
    return item


async def list_active_catchup_items(db: AsyncSession, now: datetime) -> list[CatchUpItem]:
    """Pending items and snoozed ones whose snooze is over, newest first."""
    return list((await db.execute(
        select(CatchUpItem).where(or_(
            CatchUpItem.status == "pending",
            and_(CatchUpItem.status == "snoozed", CatchUpItem.snoozed_until <= now),
        )).order_by(desc(CatchUpItem.updated_at))
    )).scalars().all())


async def list_catchup_items(db: AsyncSession) -> list[CatchUpItem]:
    return list((await db.execute(select(CatchUpItem).order_by(desc(CatchUpItem.updated_at)))).scalars().all())


async def catchup_summaries(db: AsyncSession, conversation_ids: Iterable[int]) -> tuple[dict, dict]:
    """``({conversation_id: CatchUpSummary}, {conversation_id: message count})``
    for the conversations that have a summary (two queries)."""
    ids = list(conversation_ids)
    if not ids:
        return {}, {}
    summaries = {
        s.conversation_id: s
        for s in (await db.execute(
            select(CatchUpSummary).where(CatchUpSummary.conversation_id.in_(ids))
        )).scalars().all()
    }
    totals = dict((await db.execute(
        select(Message.conversation_id, func.count(Message.id))
        .where(Message.conversation_id.in_(list(summaries)))
        .group_by(Message.conversation_id)
    )).all()) if summaries else {}
    return summaries, totals


async def reopen_handled_catchup(db: AsyncSession, conversation_id: int) -> bool:
    """A handled item goes back to pending (new incoming activity). Commits;
    returns whether one was reopened."""
    item = (await db.execute(
        select(CatchUpItem).where(CatchUpItem.conversation_id == conversation_id, CatchUpItem.status == "handled")
    )).scalar_one_or_none()
    if item is None:
        return False
    item.status = "pending"
    item.handled_at = None
    item.updated_at = datetime.utcnow()
    await db.commit()
    return True
