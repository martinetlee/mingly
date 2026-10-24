"""Calendar events (Google Calendar) and their attendees.

Times are naive UTC, except all-day events: their ``start_at`` / ``end_at``
are the event's dates at midnight (end exclusive), not converted — an
all-day event is a calendar date, not an instant.
"""

from datetime import date, datetime, timedelta
from typing import Iterable, Optional, cast

from sqlalchemy import and_, delete, exists, or_, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import CalendarAttendee, CalendarEvent, Identity, Person

__all__ = [
    "upsert_calendar_event",
    "delete_calendar_event",
    "prune_calendar_events",
    "has_calendar_events",
    "list_calendar_events",
    "calendar_event_attendees",
    "person_meetings",
]

EVENT_FIELDS = ("calendar_id", "title", "description", "location", "start_at", "end_at", "all_day", "status",
                "organizer_identity_id", "html_link", "updated_at", "conference_url", "conference_name",
                "my_response", "recurring_event_id", "recurrence", "color", "transparency")


async def upsert_calendar_event(
    db: AsyncSession,
    account_id: int,
    external_id: str,
    fields: dict,
    attendees: list,
) -> CalendarEvent:
    """Create or update the event ``(account_id, external_id)`` and replace
    its attendees with ``attendees`` = ``[(identity_id, response_status,
    is_organizer)]`` (one row per identity). Doesn't commit."""
    unknown = set(fields) - set(EVENT_FIELDS)
    if unknown:
        raise TypeError(f"upsert_calendar_event: unknown fields {sorted(unknown)}")
    ev = (await db.execute(select(CalendarEvent).where(
        CalendarEvent.account_id == account_id, CalendarEvent.external_id == external_id,
    ))).scalar_one_or_none()
    if ev is None:
        ev = CalendarEvent(account_id=account_id, external_id=external_id)
        db.add(ev)
    for k, v in fields.items():
        setattr(ev, k, v)
    await db.flush()
    await db.execute(delete(CalendarAttendee).where(CalendarAttendee.event_id == ev.id))
    seen: set = set()
    for identity_id, response_status, is_organizer in attendees:
        if identity_id in seen:
            continue
        seen.add(identity_id)
        db.add(CalendarAttendee(event_id=ev.id, identity_id=identity_id, response_status=response_status,
                                is_organizer=bool(is_organizer)))
    await db.flush()
    return ev


async def delete_calendar_event(db: AsyncSession, account_id: int, external_id: str) -> bool:
    """Remove a cancelled / deleted event. Doesn't commit."""
    res = cast(CursorResult, await db.execute(delete(CalendarEvent).where(
        CalendarEvent.account_id == account_id, CalendarEvent.external_id == external_id,
    )))
    return bool(res.rowcount)


async def prune_calendar_events(
    db: AsyncSession,
    account_id: int,
    *,
    keep_calendars: Optional[Iterable[str]] = None,
    calendar_id: Optional[str] = None,
    keep_external_ids: Optional[Iterable[str]] = None,
) -> int:
    """Drop an account's events of calendars no longer synced
    (``keep_calendars``), or the events of ``calendar_id`` a full sync didn't
    return (``keep_external_ids``). Doesn't commit. Returns rows deleted."""
    q = delete(CalendarEvent).where(CalendarEvent.account_id == account_id)
    if keep_calendars is not None:
        keep = list(keep_calendars)
        q = q.where(or_(CalendarEvent.calendar_id.is_(None), CalendarEvent.calendar_id.notin_(keep)))
    else:
        keep_ids = list(keep_external_ids or [])
        q = q.where(CalendarEvent.calendar_id == calendar_id)
        if keep_ids:
            # Large sets: compare in Python rather than build a huge IN clause
            ids = (await db.execute(select(CalendarEvent.id, CalendarEvent.external_id).where(
                CalendarEvent.account_id == account_id, CalendarEvent.calendar_id == calendar_id,
            ))).all()
            keep_set = set(keep_ids)
            stale = [i for i, ext in ids if ext not in keep_set]
            n = 0
            for i in range(0, len(stale), 500):
                res = cast(CursorResult, await db.execute(
                    delete(CalendarEvent).where(CalendarEvent.id.in_(stale[i:i + 500]))
                ))
                n += res.rowcount
            return n
    return cast(CursorResult, await db.execute(q)).rowcount


async def has_calendar_events(db: AsyncSession) -> bool:
    return bool((await db.execute(select(exists().where(CalendarEvent.id.isnot(None))))).scalar())


def _overlaps(start: datetime, end: datetime, day_start: Optional[date] = None, day_end: Optional[date] = None):
    """Timed events overlapping the instant range [start, end); all-day events
    overlapping the date range [day_start, day_end) (default: the dates of
    ``start`` / ``end``)."""
    if day_end is None:
        day_end = end.date() if end.time() == datetime.min.time() else end.date() + timedelta(days=1)
    d0 = datetime.combine(day_start or start.date(), datetime.min.time())
    d1 = datetime.combine(day_end, datetime.min.time())
    return or_(
        and_(CalendarEvent.all_day.is_(False), CalendarEvent.start_at < end,
             or_(CalendarEvent.end_at > start, and_(CalendarEvent.end_at.is_(None), CalendarEvent.start_at >= start))),
        and_(CalendarEvent.all_day.is_(True), CalendarEvent.start_at < d1,
             or_(CalendarEvent.end_at > d0, and_(CalendarEvent.end_at.is_(None), CalendarEvent.start_at >= d0))),
    )


async def list_calendar_events(
    db: AsyncSession,
    start: datetime,
    end: datetime,
    *,
    day_start: Optional[date] = None,
    day_end: Optional[date] = None,
    account_id: Optional[int] = None,
    person_id: Optional[int] = None,
    limit: int = 1000,
) -> list[CalendarEvent]:
    """Events overlapping [start, end) (naive UTC; all-day events by the
    local dates ``day_start`` ≤ date < ``day_end``), earliest first.
    ``person_id`` keeps meetings that person attends (not declined)."""
    q = select(CalendarEvent).where(_overlaps(start, end, day_start, day_end))
    if account_id is not None:
        q = q.where(CalendarEvent.account_id == account_id)
    if person_id is not None:
        q = q.where(_attended_by(person_id))
    q = q.order_by(CalendarEvent.start_at, CalendarEvent.id).limit(limit)
    return list((await db.execute(q)).scalars().all())


def _attended_by(person_id: int):
    return exists().where(
        CalendarAttendee.event_id == CalendarEvent.id,
        CalendarAttendee.identity_id == Identity.id,
        Identity.person_id == person_id,
        or_(CalendarAttendee.response_status.is_(None), CalendarAttendee.response_status != "declined"),
    )


async def calendar_event_attendees(db: AsyncSession, event_ids: Iterable[int]) -> dict:
    """``{event_id: [(CalendarAttendee, Identity, person name)]}`` — organizer
    first, then by name."""
    ids = list(set(event_ids))
    out: dict = {i: [] for i in ids}
    for i in range(0, len(ids), 500):
        rows = (await db.execute(
            select(CalendarAttendee, Identity, Person.name)
            .join(Identity, Identity.id == CalendarAttendee.identity_id)
            .join(Person, Person.id == Identity.person_id)
            .where(CalendarAttendee.event_id.in_(ids[i:i + 500]))
        )).all()
        for att, ident, name in rows:
            out[att.event_id].append((att, ident, name))
    for rows in out.values():
        rows.sort(key=lambda r: (not r[0].is_organizer, (r[2] or r[1].external_id or "").lower()))
    return out


async def person_meetings(
    db: AsyncSession,
    person_id: int,
    *,
    before: Optional[datetime] = None,
    after: Optional[datetime] = None,
    upcoming_from: Optional[datetime] = None,
    upcoming_date: Optional[date] = None,
    limit: int = 200,
) -> list[CalendarEvent]:
    """Meetings the person attends (not declined).

    Past mode (``before`` / ``after``): events starting in [after, before),
    newest first — the person's timeline. Upcoming mode (``upcoming_from``,
    with the local ``upcoming_date`` for all-day events): events not yet over,
    earliest first."""
    q = select(CalendarEvent).where(_attended_by(person_id))
    if upcoming_from is not None:
        today = datetime.combine(upcoming_date or upcoming_from.date(), datetime.min.time())
        q = q.where(or_(
            and_(CalendarEvent.all_day.is_(False),
                 or_(CalendarEvent.end_at > upcoming_from,
                     and_(CalendarEvent.end_at.is_(None), CalendarEvent.start_at >= upcoming_from))),
            and_(CalendarEvent.all_day.is_(True),
                 or_(CalendarEvent.end_at > today, and_(CalendarEvent.end_at.is_(None), CalendarEvent.start_at >= today))),
        )).order_by(CalendarEvent.start_at, CalendarEvent.id)
    else:
        if before is not None:
            q = q.where(CalendarEvent.start_at < before)
        if after is not None:
            q = q.where(CalendarEvent.start_at >= after)
        q = q.order_by(CalendarEvent.start_at.desc(), CalendarEvent.id.desc())
    return list((await db.execute(q.limit(limit))).scalars().all())
