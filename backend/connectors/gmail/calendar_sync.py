"""Google Calendar sync for a Gmail account (same OAuth grant, scope
``calendar.readonly``).

The account's Gmail connector runs one ``CalendarSync`` loop next to its mail
loop and shares its ``GmailApi`` (token refresh included). Synced calendars:
the primary one, or the ids in the ``calendar_ids`` pref for the account
(``{"<account_id>": [...]}``).

First sync of a calendar: ``events.list`` from 30 days back to 90 days ahead
(recurring events expanded), page by page; the last page's ``nextSyncToken``
is stored in ``accounts.sync_state_json["calendar"]`` (merged, the mail keys
stay). Then every 5 minutes an incremental ``events.list`` with that token;
410 Gone means the token expired — drop it and sync the window again.
Incremental results only carry events that changed, so new instances of an
unchanged recurring meeting (or old events that move into the window) would
never arrive: the window is pulled again once a day (``window_synced_at``).
A window pull drops the stored events inside the window that Google no
longer returns; events outside it (history, far future) are kept.
Cancelled events are deleted. Attendees and the organizer become ``gmail``
identities (external id = lowercased email), so they land on people and
link by email.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.connectors.gmail.api import GmailApi, GmailAuthError, GmailError, GmailNotFound
from backend.people import identities as person_identities

logger = logging.getLogger(__name__)

CHANNEL = "gmail"
POLL_INTERVAL = 5 * 60.0
BACKOFF_BASE = 30.0
BACKOFF_MAX = 30 * 60.0
WINDOW_PAST_DAYS = 30
WINDOW_AHEAD_DAYS = 90
WINDOW_REFRESH = timedelta(days=1)
PAGE_SIZE = 250
PRIMARY = "primary"
STATE_KEY = "calendar"


def selected_calendars(account_id: int) -> list[str]:
    """The calendars synced for an account (default: the primary one)."""
    from backend.prefs import get_pref

    prefs = get_pref("calendar_ids", {})
    chosen = prefs.get(str(account_id)) if isinstance(prefs, dict) else None
    if isinstance(chosen, list):
        ids = [str(c) for c in chosen if isinstance(c, str) and c.strip()]
        if ids:
            return list(dict.fromkeys(ids))
    return [PRIMARY]


def set_selected_calendars(account_id: int, calendar_ids: list[str]) -> list[str]:
    from backend.prefs import update_prefs

    ids = list(dict.fromkeys(c.strip() for c in calendar_ids if c and c.strip())) or [PRIMARY]

    def mutate(prefs: dict) -> None:
        current = prefs.get("calendar_ids")
        current = dict(current) if isinstance(current, dict) else {}
        current[str(account_id)] = ids
        prefs["calendar_ids"] = current

    update_prefs(mutate)
    return ids


# -- Google event → row fields ---------------------------------------------------------

def _parse_instant(value: str, tz_name: Optional[str] = None) -> datetime:
    """RFC 3339 → naive UTC. A value without an offset is local to ``tz_name``."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
    if dt.tzinfo is None:
        tz: Any = timezone.utc
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(tz_name)
            except Exception:
                pass
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def parse_when(when: Optional[dict]) -> "tuple[Optional[datetime], bool]":
    """An event's ``start`` / ``end`` → (naive UTC instant, all_day). An
    all-day ``date`` is kept as that date at midnight (not converted)."""
    if not when:
        return None, False
    if when.get("date"):
        return datetime.strptime(when["date"], "%Y-%m-%d"), True
    if when.get("dateTime"):
        return _parse_instant(when["dateTime"], when.get("timeZone")), False
    return None, False


def _email(person: Optional[dict]) -> Optional[str]:
    email = str((person or {}).get("email") or "").strip().lower()
    # Calendars (…@group.calendar.google.com) and rooms (…@resource.calendar.google.com) aren't people
    if not email or "@" not in email or email.endswith("calendar.google.com"):
        return None
    return email


def event_fields(event: dict, calendar_id: str) -> dict:
    start, all_day = parse_when(event.get("start"))
    end, _ = parse_when(event.get("end"))
    updated = None
    if event.get("updated"):
        try:
            updated = _parse_instant(event["updated"])
        except ValueError:
            updated = None
    return {
        "calendar_id": calendar_id,
        "title": event.get("summary") or None,
        "description": event.get("description") or None,
        "location": event.get("location") or None,
        "start_at": start,
        "end_at": end,
        "all_day": all_day,
        "status": event.get("status") or None,
        "html_link": event.get("htmlLink") or None,
        "updated_at": updated,
    }


def _window_due(synced_at: Optional[str]) -> bool:
    """Whether the calendar's window should be pulled again."""
    try:
        return datetime.utcnow() - datetime.fromisoformat(synced_at or "") >= WINDOW_REFRESH
    except (TypeError, ValueError):
        return True


class CalendarSync:
    """The calendar loop of one Gmail account."""

    def __init__(self, account_id: int, api: GmailApi) -> None:
        self.account_id = account_id
        self.api = api
        self.error: Optional[str] = None
        self.last_sync_at: Optional[datetime] = None
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        failures = 0
        while True:
            try:
                await self.sync_once()
                failures = 0
                self.error = None
                delay = POLL_INTERVAL
            except asyncio.CancelledError:
                raise
            except GmailAuthError as e:
                # The mail loop reports the lost grant; a re-auth restarts both.
                self.error = f"Sign in again: {e}"
                return
            except GmailError as e:
                failures += 1
                self.error = str(e)
                delay = min(e.retry_after or BACKOFF_BASE * 2 ** (failures - 1), BACKOFF_MAX)
                logger.info("Calendar sync for account %s failed: %s", self.account_id, e)
            except Exception as e:
                logger.exception("Calendar sync failed for account %s", self.account_id)
                failures += 1
                self.error = str(e) or type(e).__name__
                delay = min(BACKOFF_BASE * 2 ** (failures - 1), BACKOFF_MAX)
            await self._sleep(delay)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def sync_once(self) -> bool:
        """Sync every selected calendar once. Returns whether anything changed."""
        from backend.connectors.gmail.connector import load_sync_state, merge_sync_state
        from backend.db import crud
        from backend.db.database import AsyncSessionLocal

        calendars = selected_calendars(self.account_id)
        state = (await load_sync_state(self.account_id)).get(STATE_KEY) or {}
        tokens: dict = dict(state.get("sync_tokens") or {})
        windows: dict = dict(state.get("window_synced_at") or {})
        changed = False
        async with AsyncSessionLocal() as db:
            dropped = await crud.prune_calendar_events(db, self.account_id, keep_calendars=calendars)
            await db.commit()
        changed |= bool(dropped)
        tokens = {c: t for c, t in tokens.items() if c in calendars}
        windows = {c: w for c, w in windows.items() if c in calendars}
        for cal in calendars:
            token = tokens.get(cal) if not _window_due(windows.get(cal)) else None
            try:
                token, n, full = await self._sync_calendar(cal, token)
            except GmailNotFound:
                logger.info("Calendar %s of account %s not found; skipped", cal, self.account_id)
                tokens.pop(cal, None)
                windows.pop(cal, None)
                continue
            changed |= bool(n)
            if token:
                tokens[cal] = token
            if full:
                windows[cal] = datetime.utcnow().isoformat()
            await merge_sync_state(self.account_id, calendar={
                "sync_tokens": tokens, "window_synced_at": windows, "last_sync_at": datetime.utcnow().isoformat(),
            })
        self.last_sync_at = datetime.utcnow()
        await merge_sync_state(self.account_id, calendar={
            "sync_tokens": tokens, "window_synced_at": windows, "last_sync_at": self.last_sync_at.isoformat(),
        })
        if changed:
            from backend.ws.manager import ws_manager
            await ws_manager.broadcast({"type": "calendar_synced", "account_id": self.account_id})
        return changed

    async def _sync_calendar(self, calendar_id: str, sync_token: Optional[str]) -> "tuple[Optional[str], int, bool]":
        """Incremental with ``sync_token``, else the full window. Returns the
        new sync token, how many events were stored or removed, and whether
        the window was pulled."""
        if sync_token:
            try:
                token, n = await self._pull(calendar_id, {"syncToken": sync_token}, full=None)
                return token, n, False
            except GmailError as e:
                if e.status != 410:
                    raise
                logger.info("Calendar sync token expired for %s (account %s); full resync",
                            calendar_id, self.account_id)
        now = datetime.utcnow()
        start, end = now - timedelta(days=WINDOW_PAST_DAYS), now + timedelta(days=WINDOW_AHEAD_DAYS)
        window = {"timeMin": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "timeMax": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
        token, n = await self._pull(calendar_id, window, full=(start, end))
        return token, n, True

    async def _pull(
        self, calendar_id: str, params: dict, *, full: "Optional[tuple[datetime, datetime]]",
    ) -> "tuple[Optional[str], int]":
        """Page through ``events.list``. ``full`` = the window (naive UTC) of
        a window pull, whose stored events Google didn't return are dropped."""
        from backend.db import crud
        from backend.db.database import AsyncSessionLocal

        seen: set = set()
        count = 0
        page: Optional[str] = None
        token: Optional[str] = None
        while True:
            q: dict[str, Any] = {**params, "singleEvents": "true", "maxResults": PAGE_SIZE}
            if full:
                q["showDeleted"] = "false"
            if page:
                q["pageToken"] = page
            resp = await self.api.list_events(calendar_id, q)
            async with AsyncSessionLocal() as db:
                for event in resp.get("items") or []:
                    ext = str(event.get("id") or "")
                    if not ext:
                        continue
                    if event.get("status") == "cancelled":
                        count += await crud.delete_calendar_event(db, self.account_id, ext)
                        continue
                    seen.add(ext)
                    await self._store(db, event, calendar_id)
                    count += 1
                await db.commit()
            page = resp.get("nextPageToken")
            if not page:
                token = resp.get("nextSyncToken")
                break
        if full:
            count += await self._prune_window(calendar_id, full, seen)
        return token, count

    async def _prune_window(self, calendar_id: str, window: "tuple[datetime, datetime]", seen: set) -> int:
        """Delete this calendar's stored events that start inside ``window``
        but weren't in the window pull (deleted or moved out at Google)."""
        from sqlalchemy import delete, select

        from backend.db.database import AsyncSessionLocal
        from backend.db.models import CalendarEvent

        start, end = window
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(CalendarEvent.id, CalendarEvent.external_id).where(
                CalendarEvent.account_id == self.account_id, CalendarEvent.calendar_id == calendar_id,
                CalendarEvent.start_at >= start, CalendarEvent.start_at < end,
            ))).all()
            stale = [i for i, ext in rows if ext not in seen]
            for i in range(0, len(stale), 500):
                await db.execute(delete(CalendarEvent).where(CalendarEvent.id.in_(stale[i:i + 500])))
            await db.commit()
        return len(stale)

    async def _store(self, db, event: dict, calendar_id: str) -> None:
        from backend.db import crud

        async def identity_id(person: Optional[dict]) -> Optional[int]:
            email = _email(person)
            if email is None:
                return None
            ident = await person_identities.upsert_identity(
                db, channel=CHANNEL, external_id=email, commit=False,
                display_name=str((person or {}).get("displayName") or "").strip() or None,
            )
            return ident.id

        fields = event_fields(event, calendar_id)
        organizer = event.get("organizer")
        organizer_id = await identity_id(organizer)
        fields["organizer_identity_id"] = organizer_id
        attendees = []
        for a in event.get("attendees") or []:
            if a.get("resource"):
                continue
            iid = await identity_id(a)
            if iid is not None:
                attendees.append((iid, a.get("responseStatus") or None, bool(a.get("organizer")) or iid == organizer_id))
        if organizer_id is not None and all(iid != organizer_id for iid, _, _ in attendees):
            attendees.insert(0, (organizer_id, None, True))
        await crud.upsert_calendar_event(db, self.account_id, str(event["id"]), fields, attendees)
