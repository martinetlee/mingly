"""Regressions for the API-layer review: account logout keeps the session's
sockets, message-window jumps on unfetchable ids, connector file sends own
their temp files, Telegram mute times, Telegram user → person lookups,
address-book sync without Telegram, date overflow, limit clamps, reaction
errors and reply previews on own sends."""

import asyncio
import io
import os
import time
from datetime import datetime

import pytest
from sqlalchemy import func, select
from starlette.datastructures import UploadFile

from backend.db.models import Person
from tests import factories as f


class _FakeTelegramClient:
    """Records Telethon requests; ``fail`` makes every request raise."""

    def __init__(self, fail: bool = False):
        self.requests = []
        self.fail = fail

    async def get_input_entity(self, peer):
        return peer

    async def get_entity(self, peer):
        raise ValueError("Could not find the input entity")

    async def __call__(self, request):
        if self.fail:
            raise RuntimeError("REACTION_INVALID")
        self.requests.append(request)
        return None


def _telegram_online(monkeypatch, module, client):
    async def _client(require: bool = True):
        return client

    monkeypatch.setattr("backend.telegram.client.connected_client", _client)


# ── accounts: logout ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_account_logout_keeps_websockets(api, monkeypatch):
    """Logging one account out must not close the session's sockets with
    4001: the frontend treats that as "vault locked"."""
    from backend.connectors.manager import manager
    from backend.ws.manager import ws_manager

    class _Fake:
        channel = "signal"
        logged_out = False

        async def logout(self):
            self.logged_out = True

    fake = _Fake()
    closed = []

    async def _close_all(*args, **kwargs):
        closed.append((args, kwargs))

    monkeypatch.setattr(manager, "get", lambda key: fake)
    monkeypatch.setattr(ws_manager, "close_all", _close_all)
    r = await api.post("/api/accounts/signal/logout")
    assert r.status_code == 200
    assert fake.logged_out
    assert closed == []


# ── message window: jumps the channel can't fetch ────────────────────────────

@pytest.mark.asyncio
async def test_window_external_id_on_other_channel_is_not_found(api, db, monkeypatch):
    from backend.telegram.coordinator import coordinator

    async def _boom(*args, **kwargs):
        raise AssertionError("fetch_around must not be called")

    monkeypatch.setattr(coordinator, "fetch_around", _boom)
    sms = await f.conversation(db, "+15551234", title="Bob", channel="sms")
    await f.message(db, sms, 1, "yo")
    r = await api.get(f"/api/conversations/{sms.id}/messages/window", params={"around_external_id": "p:0/ABCD-GUID"})
    assert r.status_code == 200
    body = r.json()
    assert body["anchor_found"] is False
    assert body["anchor_reason"] == "not_found"
    assert body["messages"] == []


@pytest.mark.asyncio
async def test_window_non_integer_telegram_id_is_not_found(api, db, monkeypatch):
    from backend.telegram.coordinator import coordinator

    async def _boom(*args, **kwargs):
        raise AssertionError("fetch_around must not be called")

    monkeypatch.setattr(coordinator, "fetch_around", _boom)
    tg = await f.conversation(db, "77", title="Alice", last_synced_at=datetime.utcnow())
    await f.message(db, tg, 1, "hi")
    r = await api.get(f"/api/conversations/{tg.id}/messages/window", params={"around_external_id": "abc"})
    assert r.status_code == 200
    assert r.json()["anchor_reason"] == "not_found"


@pytest.mark.asyncio
async def test_window_stored_external_id_on_other_channel_still_jumps(api, db):
    sms = await f.conversation(db, "+15551234", title="Bob", channel="sms")
    msg = await f.message(db, sms, 1, "yo")
    r = await api.get(f"/api/conversations/{sms.id}/messages/window", params={"around_external_id": "1"})
    body = r.json()
    assert body["anchor_found"] is True
    assert body["anchor_reason"] is None
    assert [m["id"] for m in body["messages"]] == [msg.id]


# ── compose: connector file sends own their temp files ───────────────────────

@pytest.mark.asyncio
async def test_connector_file_send_keeps_files_when_request_is_cancelled(db, monkeypatch):
    from backend.api import compose
    from backend.connectors.base import SentFiles

    conv = await f.conversation(db, "t1", title="Mail", channel="gmail", kind="email_thread")
    seen = {}
    started = asyncio.Event()
    finished = asyncio.Event()

    class _Connector:
        async def send_files(self, c, draft):
            started.set()
            await asyncio.sleep(0.05)
            seen["paths"] = [x.path for x in draft.files]
            seen["exists"] = [os.path.exists(x.path) for x in draft.files]
            finished.set()
            return SentFiles(message_ids=[], files=[])

    monkeypatch.setattr(compose, "_sender", lambda c: _Connector())
    upload = UploadFile(io.BytesIO(b"data"), filename="a.pdf")
    request = asyncio.ensure_future(compose.send_files(
        conversation_id=conv.id, text="hi", reply_to_message_id=None, topic_id=None,
        client_nonce="nonce-cancel", files=[upload], subject=None, to=[], cc=[],
    ))
    await started.wait()
    request.cancel()  # the client went away; the shielded send carries on
    await asyncio.wait_for(finished.wait(), 2)
    await asyncio.sleep(0.01)
    assert seen["exists"] == [True]
    assert not any(os.path.exists(p) for p in seen["paths"])  # cleaned up by the send


# ── Telegram mute ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mute_until_is_a_utc_timestamp(api, db, monkeypatch):
    """The naive-UTC muted_until must not be read as local time."""
    from backend.api import conversations

    client = _FakeTelegramClient()
    _telegram_online(monkeypatch, conversations, client)
    conv = await f.conversation(db, "77", title="Alice")
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Taipei"  # UTC+8: a local-time read is 8h off
    time.tzset()
    try:
        r = await api.post(f"/api/conversations/{conv.id}/mute", json={"muted": True, "duration": "1h"})
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    assert r.status_code == 200
    until = client.requests[0].settings.mute_until
    assert abs(until - (time.time() + 3600)) < 60


# ── Telegram user id → person ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_user_person_creates_no_placeholders(api, db, monkeypatch):
    from backend.api import telegram

    async def _offline(require: bool = True):
        return None

    monkeypatch.setattr("backend.telegram.client.connected_client", _offline)
    before = (await db.execute(select(func.count()).select_from(Person))).scalar()
    for uid in (-5, 0, 123456789):
        r = await api.get(f"/api/telegram/users/{uid}/person")
        assert r.status_code == 404, uid
    after = (await db.execute(select(func.count()).select_from(Person))).scalar()
    assert after == before

    ident = await f.identity(db, "77", display_name="Alice")
    r = await api.get("/api/telegram/users/77/person")
    assert r.status_code == 200
    assert r.json() == {"person_id": ident.person_id, "identity_id": ident.id}


# ── people: address-book sync ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_people_sync_without_telegram_is_empty_not_503(api, monkeypatch):
    from backend.connectors.manager import manager

    telegram = manager.get("telegram")
    assert telegram is not None
    monkeypatch.setattr(telegram, "is_configured", lambda: False)
    r = await api.post("/api/people/sync")
    assert r.status_code == 200
    assert r.json() == {"imported": 0, "total": 0}


# ── dates out of range ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_out_of_range_dates_are_400(api, db):
    conv = await f.conversation(db, "77", title="Alice")
    r = await api.get("/api/calendar/events", params={
        "start": "0001-01-01T00:00:00+05:00", "end": "0001-01-02T00:00:00+05:00",
    })
    assert r.status_code == 400
    r = await api.get(f"/api/conversations/{conv.id}/messages/window", params={"around_date": "0001-01-01T00:00:00+05:00"})
    assert r.status_code == 400


# ── limits ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_negative_limits_are_clamped_or_rejected(api, db):
    a = await f.conversation(db, "77", title="Alice")
    await f.conversation(db, "88", title="Bob")
    await f.message(db, a, 1, "hello https://a.example", attachments=[f.photo()])
    await f.message(db, a, 2, "hello https://b.example", attachments=[f.photo()])

    r = await api.get("/api/conversations", params={"limit": -1})
    assert r.status_code == 200 and len(r.json()) == 1
    r = await api.get(f"/api/conversations/{a.id}/messages/search", params={"q": "hello", "limit": -1})
    assert r.status_code == 200 and len(r.json()) == 1
    for media_filter in ("media", "links"):
        r = await api.get(f"/api/conversations/{a.id}/shared-media", params={"media_filter": media_filter, "limit": -1})
        assert r.status_code == 200 and len(r.json()) == 1
    r = await api.get("/api/search", params={"q": "hello", "limit": -1})
    assert r.status_code == 422


# ── reactions ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_telegram_reaction_is_400_like_pin(api, db, monkeypatch):
    from backend.api import conversations

    _telegram_online(monkeypatch, conversations, _FakeTelegramClient(fail=True))
    conv = await f.conversation(db, "77", title="Alice")
    msg = await f.message(db, conv, 1, "hi")
    r = await api.post(f"/api/messages/{msg.id}/react", json={"emoji": "👍"})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Cannot send reaction")


@pytest.mark.asyncio
async def test_failed_connector_reaction_is_400(api, db, monkeypatch):
    from backend.api import conversations
    from backend.connectors.base import Capability

    class _Connector:
        def conversation_capabilities(self, c):
            return frozenset({Capability.REACT})

        async def react(self, c, m, emoji):
            raise RuntimeError("signal-cli: rate limited")

    monkeypatch.setattr(conversations, "connector_with", lambda conv, role, what, capability=None: _Connector())
    conv = await f.conversation(db, "+15551234", title="Bob", channel="signal")
    msg = await f.message(db, conv, 1, "hi")
    r = await api.post(f"/api/messages/{msg.id}/react", json={"emoji": "👍"})
    assert r.status_code == 400


# ── own Telegram sends carry their reply preview ─────────────────────────────

@pytest.mark.asyncio
async def test_own_send_payload_has_reply_preview(db, monkeypatch):
    from backend.api import compose
    from backend.telegram import outgoing

    conv = await f.conversation(db, "77", title="Alice")
    original = await f.message(db, conv, 1, "the question")

    async def _rows_for(db_, conversation_id, msgs, forum=False):
        return [dict(conversation_id=conversation_id, external_id="2", seq=2, text="the answer",
                     date=datetime(2026, 1, 1, 13, 0), is_outgoing=True)]

    monkeypatch.setattr(outgoing, "rows_for", _rows_for)
    saved_ids = await outgoing._persist_sent(conv, [object()], reply_to_seq=1, topic_id=None)
    out = await compose._stored_messages(saved_ids)
    assert len(out) == 1
    assert out[0]["reply_to_external_id"] == "1"
    assert out[0]["reply_to_message_id"] == original.id
    assert out[0]["reply_to_preview"]["text"] == "the question"


# ── people: merged-away ids point at the survivor ────────────────────────────

@pytest.mark.asyncio
async def test_merged_away_person_404_carries_merged_into(api, db):
    from backend.people import linking

    a = await f.identity(db, "71", display_name="A")
    b = await f.identity(db, "72", display_name="B")
    c = await f.identity(db, "73", display_name="C")
    unrelated = await f.identity(db, "74", display_name="D")
    pa, pb, pc, pd = a.person_id, b.person_id, c.person_id, unrelated.person_id  # identities move with merges
    await linking.merge(db, pb, pa)  # A → B
    await db.commit()
    await linking.merge(db, pc, pb)  # B → C: A's old id now leads to C
    await db.commit()

    for gone in (pa, pb):
        for path in (f"/api/people/{gone}", f"/api/people/{gone}/timeline", f"/api/people/{gone}/notes",
                     f"/api/calendar/people/{gone}/upcoming"):
            r = await api.get(path)
            assert r.status_code == 404, path
            assert r.json()["merged_into"] == pc, path
            assert r.json()["detail"] == "Person not found"

    r = await api.get("/api/people/999999")
    assert r.status_code == 404
    assert "merged_into" not in r.json()

    # An undone merge no longer redirects
    merge_bc = (await api.get(f"/api/people/{pc}/merges")).json()
    undo_id = next(m["id"] for m in merge_bc if m["from_person_id"] == pb)
    assert (await api.post(f"/api/people/merges/{undo_id}/undo")).status_code == 200
    r = await api.get(f"/api/people/{pb}")
    assert r.status_code == 404 and "merged_into" not in r.json()
    assert (await api.get(f"/api/people/{pd}")).status_code == 200


# ── accounts: cancel a sign-in flow ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_auth_flow_ends_it_and_is_idempotent(api, monkeypatch):
    from backend.connectors.base import AuthStep
    from backend.connectors.manager import manager

    abandoned = []

    class _Connector:
        channel = "signal"
        account_key = "signal:test"
        account_id = None

        async def begin_auth(self, params):
            return AuthStep(flow_id="flow-1", kind="qr", title="Link", qr_payload="sgnl://x", poll=True)

        async def abandon_flow(self, flow_id):
            abandoned.append(flow_id)

    fake = _Connector()
    monkeypatch.setattr(manager, "_connectors", {"signal:test": fake})
    r = await api.post("/api/accounts/signal:test/auth", json={})
    assert r.status_code == 200 and r.json()["flow_id"] == "flow-1"

    r = await api.delete("/api/accounts/signal:test/auth/flow-1")
    assert r.status_code == 204 and r.content == b""
    assert abandoned == ["flow-1"]
    r = await api.post("/api/accounts/signal:test/auth/flow-1", json={})
    assert r.status_code == 404  # the flow is gone

    r = await api.delete("/api/accounts/signal:test/auth/flow-1")  # again: still 204, no second abandon
    assert r.status_code == 204
    assert abandoned == ["flow-1"]
    r = await api.delete("/api/accounts/whatever/auth/never-existed")
    assert r.status_code == 204
