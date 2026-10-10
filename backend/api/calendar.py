"""Calendar space (/api/calendar): events synced from the Google Calendars of
connected Gmail accounts (``backend.connectors.gmail.calendar_sync``).

Event shape (``event_out``)::

    {id, account_id, calendar_id, title, description, location,
     start_at, end_at,      # ISO UTC ("…Z"); all-day events: "YYYY-MM-DD" (end exclusive)
     all_day, status, html_link,
     organizer: Attendee | null,
     attendees: [Attendee]}  # organizer first
    Attendee = {identity_id, person_id, name, email, response_status, is_organizer, is_self}
"""

from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.lookups import person_or_404
from backend.db import crud
from backend.db.database import get_db
from backend.db.models import Account
from backend.serialization import iso_utc
from backend.connectors.gmail.api import GmailError
from backend.connectors.gmail.calendar_sync import PRIMARY, selected_calendars, set_selected_calendars
from backend.connectors.manager import manager

router = APIRouter()

MAX_RANGE_DAYS = 400
UPCOMING_LIMIT = 20
TIMELINE_MEETINGS_LIMIT = 200


def parse_instant(raw: str, name: str = "date") -> datetime:
    """ISO 8601 (offset or Z; none = UTC) → naive UTC; 400 when invalid."""
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail=f"Invalid {name}: {raw!r}")
    return dt


def _parse_day(raw: Optional[str], name: str) -> Optional[date]:
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {name}: {raw!r}")


def local_now() -> datetime:
    """Now in the machine's local time zone (the user's: Mingly runs locally)."""
    return datetime.now().astimezone()


def _when(dt: Optional[datetime], all_day: bool) -> Optional[str]:
    if dt is None:
        return None
    return dt.date().isoformat() if all_day else iso_utc(dt)


def _attendee_out(att, ident, name) -> dict:
    return {
        "identity_id": ident.id,
        "person_id": ident.person_id,
        "name": name or ident.display_name or ident.external_id,
        "email": ident.external_id,
        "response_status": att.response_status if att is not None else None,
        "is_organizer": bool(att.is_organizer) if att is not None else True,
        "is_self": bool(ident.is_self),
    }


def event_out(ev, attendees: list) -> dict:
    """``attendees``: this event's ``crud.calendar_event_attendees`` rows."""
    people = [_attendee_out(a, i, n) for a, i, n in attendees]
    organizer = next((p for p in people if p["identity_id"] == ev.organizer_identity_id), None)
    return {
        "id": ev.id,
        "account_id": ev.account_id,
        "calendar_id": ev.calendar_id,
        "title": ev.title,
        "description": ev.description,
        "location": ev.location,
        "start_at": _when(ev.start_at, ev.all_day),
        "end_at": _when(ev.end_at, ev.all_day),
        "all_day": bool(ev.all_day),
        "status": ev.status,
        "html_link": ev.html_link,
        "organizer": organizer,
        "attendees": people,
    }


async def events_out(db: AsyncSession, events: list) -> list[dict]:
    rows = await crud.calendar_event_attendees(db, [e.id for e in events])
    return [event_out(e, rows.get(e.id, [])) for e in events]


async def timeline_meetings(
    db: AsyncSession, person_id: int, *, before: Optional[datetime], oldest: Optional[datetime],
) -> list:
    """Meetings for one page of the person's timeline, as
    ``(start_at, {"kind": "meeting", "event": …})``: those that started in
    [oldest message of the page, ``before`` or now) — ``oldest`` None when
    the page is the last one (every earlier meeting belongs to it)."""
    upper = before if before is not None else datetime.utcnow()
    events = await crud.person_meetings(db, person_id, before=upper, after=oldest, limit=TIMELINE_MEETINGS_LIMIT)
    out = await events_out(db, events)
    return [(e.start_at, {"kind": "meeting", "event": o}) for e, o in zip(events, out)]


# -- routes ----------------------------------------------------------------------------

@router.get("/events")
async def list_events(
    start: str,
    end: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    account_id: Optional[int] = None,
    person_id: Optional[int] = None,
    limit: int = 1000,
    db: AsyncSession = Depends(get_db),
):
    """Events overlapping [start, end) (ISO instants), earliest first. All-day
    events match by date: ``from_date`` ≤ date < ``to_date`` (YYYY-MM-DD, the
    viewer's local days; default: the dates of start / end)."""
    s, e = parse_instant(start, "start"), parse_instant(end, "end")
    if e <= s:
        raise HTTPException(status_code=400, detail="end must be after start")
    if e - s > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(status_code=400, detail=f"The range is limited to {MAX_RANGE_DAYS} days")
    events = await crud.list_calendar_events(
        db, s, e, day_start=_parse_day(from_date, "from_date"), day_end=_parse_day(to_date, "to_date"),
        account_id=account_id, person_id=person_id, limit=max(1, min(limit, 2000)),
    )
    return {"events": await events_out(db, events)}


@router.get("/people/{person_id}/upcoming")
async def person_upcoming(person_id: int, limit: int = 5, db: AsyncSession = Depends(get_db)):
    """Meetings with this person that aren't over yet, earliest first."""
    await person_or_404(db, person_id)
    now = local_now()
    events = await crud.person_meetings(
        db, person_id, upcoming_from=now.astimezone(timezone.utc).replace(tzinfo=None), upcoming_date=now.date(),
        limit=max(1, min(limit, UPCOMING_LIMIT)),
    )
    return {"events": await events_out(db, events)}


def _sync_of(account_id: int):

    connector = manager.for_account("gmail", account_id)
    return connector, getattr(connector, "calendar", None)


@router.get("/accounts")
async def calendar_accounts(db: AsyncSession = Depends(get_db)):
    """Gmail accounts and their calendar sync: ``[{account_id, label,
    calendar_ids, syncing, last_sync_at, error}]``."""

    accounts = await crud.get_accounts(db, channel="gmail")
    out = []
    for acc in accounts:
        _, sync = _sync_of(acc.id)
        out.append({
            "account_id": acc.id,
            "label": acc.label or acc.external_id,
            "calendar_ids": selected_calendars(acc.id),
            "syncing": sync is not None,
            "last_sync_at": iso_utc(sync.last_sync_at) if sync is not None else None,
            "error": sync.error if sync is not None else None,
        })
    return out


def _running_sync(account_id: int):
    connector, sync = _sync_of(account_id)
    if connector is None or sync is None:
        raise HTTPException(status_code=409, detail="This Gmail account is not connected")
    return sync


@router.get("/accounts/{account_id}/calendars")
async def list_calendars(account_id: int):
    """The account's calendars at Google: ``[{id, summary, primary, selected, color}]``."""

    sync = _running_sync(account_id)
    try:
        items = await sync.api.calendar_list()
    except GmailError as e:
        raise HTTPException(status_code=502, detail=str(e))
    chosen = set(selected_calendars(account_id))
    return [{
        "id": c["id"],
        "summary": c.get("summaryOverride") or c.get("summary") or c["id"],
        "primary": bool(c.get("primary")),
        "selected": c["id"] in chosen or (bool(c.get("primary")) and PRIMARY in chosen),
        "color": c.get("backgroundColor"),
    } for c in items if c.get("id")]


class CalendarSelection(BaseModel):
    calendar_ids: List[str]


@router.put("/accounts/{account_id}/calendars")
async def select_calendars(account_id: int, body: CalendarSelection, db: AsyncSession = Depends(get_db)):
    """Choose which calendars of the account are synced (empty = primary only)."""

    acc = await db.get(Account, account_id)
    if acc is None or acc.channel != "gmail":
        raise HTTPException(status_code=404, detail="Account not found")
    ids = set_selected_calendars(account_id, body.calendar_ids)
    _, sync = _sync_of(account_id)
    if sync is not None:
        sync.wake()
    return {"calendar_ids": ids}


@router.post("/sync")
async def sync_now():
    """Sync every connected account's calendars now."""

    n = 0
    for c in manager.all():
        sync = getattr(c, "calendar", None)
        if c.channel == "gmail" and sync is not None:
            sync.wake()
            n += 1
    return {"accounts": n}
