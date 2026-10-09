"""Google Calendar (Phase 5) against a fake Google (httpx.MockTransport):
events.list paging, syncToken incremental sync, 410 Gone → full resync,
cancelled events, attendees → identities / people, organizer, all-day
events, time zones → naive UTC, calendar selection, the /api/calendar
routes, meetings on the person timeline and today's meetings in briefings."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from sqlalchemy import select

from backend.briefing import pipeline
from backend.connectors.gmail import calendar_sync, oauth
from backend.connectors.gmail.api import GmailApi
from backend.connectors.gmail.calendar_sync import CalendarSync, parse_when
from backend.connectors.gmail.connector import load_sync_state, merge_sync_state
from backend.db import crud
from backend.db.models import Account, BriefingEntry, BriefingRun, CalendarAttendee, CalendarEvent, Identity
from tests import factories as f
from tests.llm_fakes import FakeProvider, patch_briefing_env, seed_chat, seed_msg

ME = "me@example.com"


def gevent(eid, start, end=None, *, summary="Sync", attendees=None, organizer=None, status="confirmed",
           location=None, all_day=False):
    key = "date" if all_day else "dateTime"
    ev = {
        "id": eid, "status": status, "summary": summary, "htmlLink": f"https://calendar.google.com/event?eid={eid}",
        "start": {key: start}, "end": {key: end or start}, "updated": "2026-10-01T10:00:00.000Z",
        "organizer": organizer or {"email": ME, "self": True},
    }
    if attendees is not None:
        ev["attendees"] = attendees
    if location:
        ev["location"] = location
    return ev


class FakeCalendar:
    """events.list (pages, sync tokens, 410) + calendarList + token refresh."""

    def __init__(self):
        self.full: dict[str, list[list]] = {}  # calendar -> pages of a full sync
        self.incremental: dict[str, list] = {}  # calendar -> items for the next syncToken request
        self.gone: set = set()  # calendars whose sync token is expired
        self.requests: list[tuple[str, dict]] = []
        self.calendars = [{"id": ME, "summary": ME, "primary": True, "backgroundColor": "#9fe1e7"},
                          {"id": "team@group.calendar.google.com", "summary": "Team"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        if url.netloc == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path.endswith("/users/me/calendarList"):
            return httpx.Response(200, json={"items": self.calendars})
        cal = unquote(url.path.split("/calendars/")[1].rsplit("/events", 1)[0])
        self.requests.append((cal, q))
        if "syncToken" in q:
            if cal in self.gone:
                self.gone.discard(cal)
                return httpx.Response(410, json={"error": {"message": "Sync token is no longer valid"}})
            items = self.incremental.pop(cal, [])
            return httpx.Response(200, json={"items": items, "nextSyncToken": f"{cal}-tok-inc"})
        pages = self.full.get(cal, [[]])
        i = int(q.get("pageToken", "0"))
        body = {"items": pages[i]}
        if i + 1 < len(pages):
            body["nextPageToken"] = str(i + 1)
        else:
            body["nextSyncToken"] = f"{cal}-tok-{len(self.requests)}"
        return httpx.Response(200, json=body)


@pytest.fixture
def gcal(monkeypatch):
    fake = FakeCalendar()
    monkeypatch.setattr(oauth, "transport", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def broadcasts(monkeypatch):
    from backend.ws.manager import ws_manager

    events = []

    async def capture(data):
        events.append(data)

    monkeypatch.setattr(ws_manager, "broadcast", capture)
    return events


async def _account(db) -> Account:
    acc = Account(channel="gmail", external_id=ME, label=ME,
                  sync_state_json=json.dumps({"history_id": "123", "last_sync_at": "2026-10-01T00:00:00"}))
    db.add(acc)
    await db.commit()
    me = await crud.upsert_identity(db, channel="gmail", external_id=ME, is_self=True)
    await crud.set_account_owner(db, acc.id, external_id=ME, label=ME, self_identity_id=me.id)
    return acc


def _sync(acc) -> CalendarSync:
    return CalendarSync(acc.id, GmailApi(oauth.Credentials("cid", "secret", "rt")))


async def _events(db) -> dict:
    rows = await db.execute(select(CalendarEvent).execution_options(populate_existing=True))
    return {e.external_id: e for e in rows.scalars().all()}


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parse_when_converts_to_naive_utc_and_keeps_all_day_dates():
    assert parse_when({"dateTime": "2026-10-12T09:30:00+02:00"}) == (datetime(2026, 10, 12, 7, 30), False)
    assert parse_when({"dateTime": "2026-10-12T09:30:00Z"}) == (datetime(2026, 10, 12, 9, 30), False)
    # No offset: local to the event's time zone (EDT = UTC-4 in October)
    assert parse_when({"dateTime": "2026-10-12T09:30:00", "timeZone": "America/New_York"}) == \
        (datetime(2026, 10, 12, 13, 30), False)
    assert parse_when({"date": "2026-10-12"}) == (datetime(2026, 10, 12), True)
    assert parse_when(None) == (None, False)


# ── sync ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_first_sync_pages_window_and_stores_sync_token_next_to_mail_state(db, gcal, broadcasts):
    acc = await _account(db)
    gcal.full["primary"] = [
        [gevent("e1", "2026-10-12T09:00:00+02:00", "2026-10-12T10:00:00+02:00", location="Room 1")],
        [gevent("e2", "2026-10-13", "2026-10-14", summary="Offsite", all_day=True)],
    ]
    assert await _sync(acc).sync_once() is True

    first, second = gcal.requests
    assert first[0] == "primary" and first[1]["singleEvents"] == "true" and "syncToken" not in first[1]
    t_min = datetime.strptime(first[1]["timeMin"], "%Y-%m-%dT%H:%M:%SZ")
    t_max = datetime.strptime(first[1]["timeMax"], "%Y-%m-%dT%H:%M:%SZ")
    assert abs((datetime.utcnow() - t_min) - timedelta(days=30)) < timedelta(minutes=1)
    assert abs((t_max - datetime.utcnow()) - timedelta(days=90)) < timedelta(minutes=1)
    assert second[1]["pageToken"] == "1" and second[1]["timeMin"] == first[1]["timeMin"]

    evs = await _events(db)
    e1, e2 = evs["e1"], evs["e2"]
    assert (e1.start_at, e1.end_at, e1.all_day) == (datetime(2026, 10, 12, 7), datetime(2026, 10, 12, 8), False)
    assert e1.location == "Room 1" and e1.calendar_id == "primary" and e1.html_link.endswith("eid=e1")
    assert (e2.start_at, e2.end_at, e2.all_day, e2.title) == (datetime(2026, 10, 13), datetime(2026, 10, 14), True, "Offsite")

    state = await load_sync_state(acc.id)
    assert state["history_id"] == "123"  # the mail state is untouched
    assert state["calendar"]["sync_tokens"] == {"primary": "primary-tok-2"}
    assert broadcasts == [{"type": "calendar_synced", "account_id": acc.id}]


@pytest.mark.asyncio
async def test_incremental_sync_uses_token_updates_and_removes_cancelled(db, gcal, broadcasts):
    acc = await _account(db)
    gcal.full["primary"] = [[gevent("e1", "2026-10-12T09:00:00Z"), gevent("e2", "2026-10-13T09:00:00Z")]]
    sync = _sync(acc)
    await sync.sync_once()
    gcal.requests.clear()

    gcal.incremental["primary"] = [
        gevent("e1", "2026-10-12T11:00:00Z", summary="Moved"),
        {"id": "e2", "status": "cancelled"},
        gevent("e3", "2027-06-01T09:00:00Z", summary="Far future"),
    ]
    await sync.sync_once()
    (cal, q), = gcal.requests
    assert q["syncToken"] == "primary-tok-1" and "timeMin" not in q and "timeMax" not in q
    evs = await _events(db)
    assert set(evs) == {"e1", "e3"}
    assert evs["e1"].title == "Moved" and evs["e1"].start_at == datetime(2026, 10, 12, 11)
    assert (await load_sync_state(acc.id))["calendar"]["sync_tokens"]["primary"] == "primary-tok-inc"

    broadcasts.clear()
    gcal.requests.clear()
    assert await sync.sync_once() is False  # nothing changed: no event
    assert broadcasts == []


@pytest.mark.asyncio
async def test_expired_sync_token_410_triggers_full_resync_and_prunes_stale(db, gcal, broadcasts):
    acc = await _account(db)
    gcal.full["primary"] = [[gevent("e1", "2026-10-12T09:00:00Z"), gevent("stale", "2026-10-13T09:00:00Z")]]
    sync = _sync(acc)
    await sync.sync_once()
    gcal.requests.clear()

    gcal.gone.add("primary")
    gcal.full["primary"] = [[gevent("e1", "2026-10-12T09:00:00Z"), gevent("e9", "2026-10-20T09:00:00Z")]]
    await sync.sync_once()
    assert ["syncToken" in q for _, q in gcal.requests] == [True, False]
    assert "timeMin" in gcal.requests[1][1]
    assert set(await _events(db)) == {"e1", "e9"}
    assert (await load_sync_state(acc.id))["calendar"]["sync_tokens"]["primary"].startswith("primary-tok-")


@pytest.mark.asyncio
async def test_attendees_become_identities_and_people_linked_by_email(db, gcal, broadcasts):
    acc = await _account(db)
    # Alice is already known from mail (gmail identity) and Telegram (same person)
    alice_tg = await f.identity(db, "77", display_name="Alice")
    alice_mail = await f.identity(db, "alice@example.com", channel="gmail", display_name="Alice")
    alice_mail.person_id = alice_tg.person_id
    await db.commit()
    gcal.full["primary"] = [[gevent(
        "e1", "2026-10-12T09:00:00Z",
        organizer={"email": "Bob@Example.com", "displayName": "Bob Stone"},
        attendees=[
            {"email": "bob@example.com", "displayName": "Bob Stone", "organizer": True, "responseStatus": "accepted"},
            {"email": "ALICE@example.com", "responseStatus": "tentative"},
            {"email": ME, "self": True, "responseStatus": "accepted"},
            {"email": "room-1@resource.calendar.google.com", "resource": True, "responseStatus": "accepted"},
            {"email": "carol@example.com", "displayName": "Carol", "responseStatus": "declined"},
        ],
    ), gevent("e2", "2026-10-13T09:00:00Z", organizer={"email": "team@group.calendar.google.com", "displayName": "Team"})]]
    await _sync(acc).sync_once()

    evs = await _events(db)
    idents = {i.external_id: i for i in (await db.execute(select(Identity))).scalars().all()}
    assert "room-1@resource.calendar.google.com" not in idents and "team@group.calendar.google.com" not in idents
    bob, carol = idents["bob@example.com"], idents["carol@example.com"]
    assert bob.channel == "gmail" and bob.display_name == "Bob Stone" and carol.display_name == "Carol"
    assert idents["alice@example.com"].id == alice_mail.id  # the existing identity, so Alice's person
    assert evs["e1"].organizer_identity_id == bob.id
    rows = (await db.execute(select(CalendarAttendee).where(CalendarAttendee.event_id == evs["e1"].id))).scalars().all()
    by_ident = {r.identity_id: r for r in rows}
    assert set(by_ident) == {bob.id, alice_mail.id, idents[ME].id, carol.id}
    assert by_ident[bob.id].is_organizer and by_ident[alice_mail.id].response_status == "tentative"
    assert evs["e2"].organizer_identity_id is None  # a group calendar isn't a person

    # Alice's person sees the meeting (Telegram + mail identities are one person)
    meetings = await crud.person_meetings(db, alice_tg.person_id, before=datetime(2027, 1, 1))
    assert [m.external_id for m in meetings] == ["e1"]
    # …Carol declined: not a meeting she attends
    assert await crud.person_meetings(db, carol.person_id, before=datetime(2027, 1, 1)) == []


@pytest.mark.asyncio
async def test_selected_calendars_sync_and_deselected_ones_are_dropped(db, gcal, broadcasts):
    acc = await _account(db)
    team = "team@group.calendar.google.com"
    calendar_sync.set_selected_calendars(acc.id, ["primary", team])
    gcal.full["primary"] = [[gevent("p1", "2026-10-12T09:00:00Z")]]
    gcal.full[team] = [[gevent("t1", "2026-10-12T10:00:00Z")]]
    sync = _sync(acc)
    await sync.sync_once()
    evs = await _events(db)
    assert evs["t1"].calendar_id == team and evs["p1"].calendar_id == "primary"
    assert set((await load_sync_state(acc.id))["calendar"]["sync_tokens"]) == {"primary", team}

    calendar_sync.set_selected_calendars(acc.id, [])  # back to the default: primary only
    assert calendar_sync.selected_calendars(acc.id) == ["primary"]
    await sync.sync_once()
    assert set(await _events(db)) == {"p1"}
    assert set((await load_sync_state(acc.id))["calendar"]["sync_tokens"]) == {"primary"}


@pytest.mark.asyncio
async def test_sync_state_merge_keeps_other_keys(db):
    acc = await _account(db)
    await merge_sync_state(acc.id, calendar={"sync_tokens": {"primary": "x"}})
    await merge_sync_state(acc.id, history_id="456")
    state = await load_sync_state(acc.id)
    assert state == {"history_id": "456", "last_sync_at": "2026-10-01T00:00:00", "calendar": {"sync_tokens": {"primary": "x"}}}
    # A corrupt value is replaced, not an error
    acc.sync_state_json = "{oops"
    await db.commit()
    await merge_sync_state(acc.id, calendar={})
    assert await load_sync_state(acc.id) == {"calendar": {}}


@pytest.mark.asyncio
async def test_auth_error_stops_the_loop(db, monkeypatch, broadcasts):
    acc = await _account(db)

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    monkeypatch.setattr(oauth, "transport", httpx.MockTransport(handler))
    sync = _sync(acc)
    await sync.run()  # returns instead of retrying forever
    assert sync.error and "Sign in again" in sync.error


@pytest.mark.asyncio
async def test_window_is_pulled_again_daily_and_keeps_history_outside_it(db, gcal, broadcasts):
    """Incremental sync only returns changed events, so new instances of an
    unchanged recurring meeting never arrive: the window is re-pulled daily."""
    acc = await _account(db)
    gcal.full["primary"] = [[gevent("w1", "2026-10-12T09:00:00Z"), gevent("gone", "2026-10-14T09:00:00Z")]]
    sync = _sync(acc)
    await sync.sync_once()
    gcal.incremental["primary"] = [gevent("far", "2027-06-01T09:00:00Z"), gevent("old", "2026-07-01T09:00:00Z")]
    await sync.sync_once()  # same day: incremental
    assert "syncToken" in gcal.requests[-1][1]

    state = (await load_sync_state(acc.id))["calendar"]
    day_ago = (datetime.utcnow() - timedelta(days=1, minutes=1)).isoformat()
    await merge_sync_state(acc.id, calendar=dict(state, window_synced_at={"primary": day_ago}))
    gcal.full["primary"] = [[gevent("w1", "2026-10-12T09:00:00Z"), gevent("w1_next", "2026-12-28T09:00:00Z")]]
    gcal.requests.clear()
    await sync.sync_once()
    (cal, q), = gcal.requests
    assert "syncToken" not in q and "timeMin" in q and "timeMax" in q
    # the new instance arrived; the in-window event Google no longer returns is gone;
    # events outside the window (history, far future) are kept
    assert set(await _events(db)) == {"w1", "w1_next", "far", "old"}
    state = (await load_sync_state(acc.id))["calendar"]
    assert state["window_synced_at"]["primary"] > day_ago
    assert state["sync_tokens"]["primary"].startswith("primary-tok-")


@pytest.mark.asyncio
async def test_calendar_retry_after_is_capped(db, monkeypatch, broadcasts):
    acc = await _account(db)

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(429, headers={"Retry-After": "86400"}, json={"error": {"message": "quota"}})

    monkeypatch.setattr(oauth, "transport", httpx.MockTransport(handler))
    sync = _sync(acc)
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        raise asyncio.CancelledError

    sync._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await sync.run()
    assert delays == [calendar_sync.BACKOFF_MAX]


# ── routes ────────────────────────────────────────────────────────────────────

async def _seeded(db):
    """Alice (person) in a past meeting, a meeting today and a future one; an all-day event."""
    acc = await _account(db)
    alice = await f.identity(db, "alice@example.com", channel="gmail", display_name="Alice")
    me = await crud.get_identity_by_external(db, "gmail", ME)
    now = datetime.utcnow().replace(microsecond=0)

    async def ev(ext, start, end, *, all_day=False, attendees=(), title=None):
        e = await crud.upsert_calendar_event(db, acc.id, ext, {
            "calendar_id": "primary", "title": title or ext, "start_at": start, "end_at": end, "all_day": all_day,
            "status": "confirmed", "organizer_identity_id": me.id, "html_link": f"https://calendar.google.com/{ext}",
        }, [(me.id, "accepted", True)] + list(attendees))
        await db.commit()
        return e

    events = {
        "past": await ev("past", now - timedelta(days=3), now - timedelta(days=3, hours=-1),
                         attendees=[(alice.id, "accepted", False)]),
        "soon": await ev("soon", now + timedelta(hours=2), now + timedelta(hours=3),
                         attendees=[(alice.id, "needsAction", False)]),
        "later": await ev("later", now + timedelta(days=10), now + timedelta(days=10, hours=1),
                          attendees=[(alice.id, "accepted", False)]),
        "declined": await ev("declined", now + timedelta(days=1), now + timedelta(days=1, hours=1),
                             attendees=[(alice.id, "declined", False)]),
        "allday": await ev("allday", datetime(2026, 11, 3), datetime(2026, 11, 4), all_day=True),
    }
    return acc, alice, events


@pytest.mark.asyncio
async def test_events_route_range_shape_and_all_day_by_date(db, api):
    acc, alice, evs = await _seeded(db)
    # A week in a UTC+8 viewer's days: 2026-11-02 00:00 local = 2026-11-01 16:00Z
    resp = await api.get("/api/calendar/events", params={
        "start": "2026-11-01T16:00:00Z", "end": "2026-11-03T16:00:00Z", "from_date": "2026-11-02", "to_date": "2026-11-04",
    })
    assert resp.status_code == 200
    (allday,) = resp.json()["events"]
    assert allday["all_day"] and allday["start_at"] == "2026-11-03" and allday["end_at"] == "2026-11-04"
    # …the day after (2026-11-04 local) doesn't show it, although the UTC instants overlap
    resp = await api.get("/api/calendar/events", params={
        "start": "2026-11-03T16:00:00Z", "end": "2026-11-04T16:00:00Z", "from_date": "2026-11-04", "to_date": "2026-11-05",
    })
    assert resp.json()["events"] == []

    now = datetime.utcnow()
    resp = await api.get("/api/calendar/events", params={
        "start": (now - timedelta(days=5)).isoformat() + "Z", "end": (now + timedelta(days=5)).isoformat() + "Z",
    })
    events = resp.json()["events"]
    assert [e["title"] for e in events] == ["past", "soon", "declined"]
    soon = events[1]
    assert soon["start_at"].endswith("Z") and soon["html_link"] == "https://calendar.google.com/soon"
    assert soon["organizer"]["email"] == ME and soon["organizer"]["is_self"]
    assert [a["email"] for a in soon["attendees"]] == [ME, "alice@example.com"]  # organizer first
    assert soon["attendees"][1] == {
        "identity_id": alice.id, "person_id": alice.person_id, "name": "Alice", "email": "alice@example.com",
        "response_status": "needsAction", "is_organizer": False, "is_self": False,
    }
    by_person = await api.get("/api/calendar/events", params={
        "start": (now - timedelta(days=5)).isoformat() + "Z", "end": (now + timedelta(days=5)).isoformat() + "Z",
        "person_id": alice.person_id,
    })
    assert [e["title"] for e in by_person.json()["events"]] == ["past", "soon"]

    bad = await api.get("/api/calendar/events", params={"start": "x", "end": "2026-11-04T00:00:00Z"})
    assert bad.status_code == 400
    backwards = await api.get("/api/calendar/events", params={"start": "2026-11-04T00:00:00Z", "end": "2026-11-01T00:00:00Z"})
    assert backwards.status_code == 400


@pytest.mark.asyncio
async def test_upcoming_meetings_route(db, api):
    _, alice, _ = await _seeded(db)
    resp = await api.get(f"/api/calendar/people/{alice.person_id}/upcoming")
    assert [e["title"] for e in resp.json()["events"]] == ["soon", "later"]  # not past, not declined
    assert (await api.get("/api/calendar/people/9999/upcoming")).status_code == 404


@pytest.mark.asyncio
async def test_accounts_route_and_calendar_selection(db, api, monkeypatch, gcal):
    from backend.connectors.manager import manager

    acc = await _account(db)
    resp = await api.get("/api/calendar/accounts")
    assert resp.json() == [{"account_id": acc.id, "label": ME, "calendar_ids": ["primary"], "syncing": False,
                            "last_sync_at": None, "error": None}]
    assert (await api.get(f"/api/calendar/accounts/{acc.id}/calendars")).status_code == 409

    sync = _sync(acc)
    sync.wake = lambda: setattr(sync, "woken", True)
    connector = type("C", (), {"channel": "gmail", "account_id": acc.id, "multi_account": True, "calendar": sync})()
    monkeypatch.setattr(manager, "for_account", lambda ch, aid: connector if aid == acc.id else None)
    cals = (await api.get(f"/api/calendar/accounts/{acc.id}/calendars")).json()
    assert cals[0] == {"id": ME, "summary": ME, "primary": True, "selected": True, "color": "#9fe1e7"}
    assert cals[1]["selected"] is False

    put = await api.put(f"/api/calendar/accounts/{acc.id}/calendars",
                        json={"calendar_ids": ["primary", "team@group.calendar.google.com"]})
    assert put.json() == {"calendar_ids": ["primary", "team@group.calendar.google.com"]}
    assert getattr(sync, "woken", False)
    assert calendar_sync.selected_calendars(acc.id) == ["primary", "team@group.calendar.google.com"]
    assert (await api.put("/api/calendar/accounts/999/calendars", json={"calendar_ids": []})).status_code == 404


# ── timeline ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_timeline_includes_started_meetings_alongside_messages(db, api):
    _, alice, evs = await _seeded(db)
    dm = await f.conversation(db, "77", title="Alice", peer=await f.identity(db, "77", display_name="Alice"))
    tg = await crud.get_identity_by_external(db, "telegram", "77")
    tg.person_id = alice.person_id
    await db.commit()
    now = datetime.utcnow()
    recent = await f.message(db, dm, 1, "recent", sender=tg, date=now - timedelta(days=1))
    old = await f.message(db, dm, 2, "old", sender=tg, date=now - timedelta(days=10))

    url = f"/api/people/{alice.person_id}/timeline"
    body = (await api.get(url)).json()
    kinds = [(it["kind"], it["message"]["id"] if it["kind"] == "message" else it["event"]["title"]) for it in body["items"]]
    # Started meetings only (not the upcoming ones), merged newest first
    assert kinds == [("message", recent.id), ("meeting", "past"), ("message", old.id)]
    meeting = body["items"][1]
    assert set(meeting) == {"kind", "event"} and meeting["event"]["html_link"].endswith("/past")
    assert body["items"][0]["conversation"]["kind"] == "direct"

    # Paging: a page holds the meetings between its oldest message and `before`
    first = (await api.get(url, params={"limit": 1})).json()
    assert [it["kind"] for it in first["items"]] == ["message"] and first["has_more"] is True
    oldest = first["items"][-1]["message"]
    second = (await api.get(url, params={"limit": 1, "before": oldest["date"], "before_id": oldest["id"]})).json()
    assert [(it["kind"]) for it in second["items"]] == ["meeting", "message"]
    assert second["has_more"] is False


# ── briefing ──────────────────────────────────────────────────────────────────

class _Job:
    def __init__(self):
        self.events = []

    def emit(self, event, data):
        self.events.append((event, data))


@pytest.mark.asyncio
async def test_todays_meetings_lists_local_day_with_people(db):
    acc = await _account(db)
    me = await crud.get_identity_by_external(db, "gmail", ME)
    ann = await f.identity(db, "ann@example.com", channel="gmail", display_name="Ann Lee")
    bob = await f.identity(db, "bob@example.com", channel="gmail", display_name="Bob")
    tz = timezone(timedelta(hours=8))
    now = datetime(2026, 10, 10, 8, 0, tzinfo=tz)  # local day 2026-10-10 = [10-09 16:00Z, 10-10 16:00Z)

    assert await pipeline.todays_meetings(db, now) is None  # no calendar data: no section

    async def ev(ext, start, end, title, attendees, all_day=False, location=None):
        await crud.upsert_calendar_event(db, acc.id, ext, {
            "calendar_id": "primary", "title": title, "start_at": start, "end_at": end, "all_day": all_day,
            "location": location, "organizer_identity_id": ann.id,
        }, attendees)
        await db.commit()

    await ev("yesterday", datetime(2026, 10, 9, 10), datetime(2026, 10, 9, 11), "Yesterday", [(ann.id, None, True)])
    assert await pipeline.todays_meetings(db, now) == "No meetings today."

    await ev("m1", datetime(2026, 10, 10, 1, 30), datetime(2026, 10, 10, 2), "1:1 *Ann*",
             [(ann.id, "accepted", True), (me.id, "accepted", False), (bob.id, "declined", False)], location="Cafe")
    await ev("m0", datetime(2026, 10, 9, 17), datetime(2026, 10, 9, 18), "Early standup", [(bob.id, "accepted", False)])
    await ev("ad", datetime(2026, 10, 10), datetime(2026, 10, 11), "Offsite", [], all_day=True)
    await ev("tomorrow", datetime(2026, 10, 10, 16), datetime(2026, 10, 10, 17), "Tomorrow", [(bob.id, None, False)])
    text = await pipeline.todays_meetings(db, now)
    assert text.splitlines() == [
        "- **01:00–02:00** Early standup — with Bob",
        "- **All day** Offsite",
        "- **09:30–10:00** 1:1 \\*Ann\\* — with Ann Lee (organizer) · Cafe",
    ]


@pytest.mark.asyncio
async def test_briefing_adds_todays_meetings_section(db, monkeypatch):
    tracked = await seed_chat(db, -1001, "Tracked", is_tracked=True)
    await seed_msg(db, tracked, 1, "hello tracked")
    monkeypatch.setattr(pipeline, "todays_meetings", AsyncMock(return_value="- **09:00–10:00** Sync — with Ann"))
    job = _Job()
    with patch_briefing_env(FakeProvider(summary="- Something [1]")):
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "done" and outcome.ok == 1
    entries = (await db.execute(select(BriefingEntry).order_by(BriefingEntry.id))).scalars().all()
    assert [(e.category, e.conversation_title) for e in entries] == [("meetings", "Today's meetings"), ("tracked", "Tracked")]
    assert entries[0].summary.startswith("- **09:00") and entries[0].conversation_id is None and entries[0].person_id is None
    done = [d for e, d in job.events if e == "chat_done"]
    assert done[0]["category"] == "meetings" and done[0]["sources"] == []


@pytest.mark.asyncio
async def test_briefing_with_only_meetings_needs_no_llm(db, monkeypatch):
    monkeypatch.setattr(pipeline, "todays_meetings", AsyncMock(return_value="No meetings today."))
    job = _Job()
    with patch_briefing_env(None):  # no LLM configured
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "done" and outcome.run_id is not None
    run = (await db.execute(select(BriefingRun))).scalar_one()
    assert run.status == "done"
    (entry,) = (await db.execute(select(BriefingEntry))).scalars().all()
    assert entry.category == "meetings" and entry.summary == "No meetings today."


@pytest.mark.asyncio
async def test_briefing_without_calendar_stays_empty(db):
    job = _Job()
    with patch_briefing_env(None):
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "empty"
    assert (await db.execute(select(BriefingRun))).first() is None
