"""Briefing runs and entries. ``sources_json`` holds
``[{conversation_id, message_id}]``."""

from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import delete, desc, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import BriefingEntry, BriefingRun

__all__ = [
    "create_briefing_run",
    "add_briefing_entry",
    "finish_briefing_run",
    "count_briefing_entries",
    "list_briefing_runs",
    "get_briefing_run",
    "get_briefing_entry",
    "get_entries_for_runs",
    "get_briefing_run_entries",
    "delete_briefing_run",
    "latest_finished_briefing_run",
]


async def create_briefing_run(db: AsyncSession, days: int = 1, status: str = "done") -> BriefingRun:
    run = BriefingRun(days=days, generated_at=datetime.utcnow(), status=status)
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def add_briefing_entry(
    db: AsyncSession,
    run_id: int,
    *,
    conversation_id: Optional[int],
    conversation_title: str,
    category: str,
    summary: str,
    person_id: Optional[int] = None,
    sources_json: Optional[str] = None,
) -> BriefingEntry:
    """An entry about a conversation (tracked / briefing) or, for
    ``important_contact``, about a person (``conversation_id`` None)."""
    entry = BriefingEntry(
        run_id=run_id,
        conversation_id=conversation_id,
        person_id=person_id,
        conversation_title=conversation_title,
        category=category,
        summary=summary,
        sources_json=sources_json,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def finish_briefing_run(
    db: AsyncSession, run_id: int, status: str, error: Optional[str] = None
) -> None:
    await db.execute(
        update(BriefingRun)
        .where(BriefingRun.id == run_id)
        .values(status=status, error=error, finished_at=datetime.utcnow())
    )
    await db.commit()


async def count_briefing_entries(db: AsyncSession, run_id: int) -> int:
    return (await db.execute(
        select(func.count()).select_from(BriefingEntry).where(BriefingEntry.run_id == run_id)
    )).scalar() or 0


async def list_briefing_runs(db: AsyncSession, include_running: bool = True) -> list[BriefingRun]:
    q = select(BriefingRun)
    if not include_running:
        q = q.where(BriefingRun.status != "running")
    result = await db.execute(q.order_by(desc(BriefingRun.generated_at)))
    return list(result.scalars().all())


async def get_briefing_run(db: AsyncSession, run_id: int) -> Optional[BriefingRun]:
    return (await db.execute(select(BriefingRun).where(BriefingRun.id == run_id))).scalar_one_or_none()


async def get_briefing_entry(db: AsyncSession, entry_id: int, run_id: Optional[int] = None) -> Optional[BriefingEntry]:
    q = select(BriefingEntry).where(BriefingEntry.id == entry_id)
    if run_id is not None:
        q = q.where(BriefingEntry.run_id == run_id)
    return (await db.execute(q)).scalar_one_or_none()


async def get_entries_for_runs(db: AsyncSession, run_ids: Iterable[int]) -> dict[int, list[BriefingEntry]]:
    """Entries of several runs in one query, grouped by run id (ordered by id)."""
    ids = list(run_ids)
    out: dict[int, list[BriefingEntry]] = {rid: [] for rid in ids}
    if not ids:
        return out
    result = await db.execute(
        select(BriefingEntry).where(BriefingEntry.run_id.in_(ids)).order_by(BriefingEntry.id)
    )
    for e in result.scalars().all():
        out.setdefault(e.run_id, []).append(e)
    return out


async def get_briefing_run_entries(db: AsyncSession, run_id: int) -> list[BriefingEntry]:
    result = await db.execute(
        select(BriefingEntry).where(BriefingEntry.run_id == run_id).order_by(BriefingEntry.id)
    )
    return list(result.scalars().all())


async def delete_briefing_run(db: AsyncSession, run_id: int) -> bool:
    run = await get_briefing_run(db, run_id)
    if not run:
        return False
    await db.execute(delete(BriefingEntry).where(BriefingEntry.run_id == run_id))
    await db.delete(run)
    await db.commit()
    return True


async def latest_finished_briefing_run(db: AsyncSession) -> Optional[BriefingRun]:
    """The newest run that finished (fully or partly)."""
    return (await db.execute(
        select(BriefingRun)
        .where(BriefingRun.status.in_(("done", "partial")))
        .order_by(desc(BriefingRun.generated_at)).limit(1)
    )).scalar_one_or_none()
