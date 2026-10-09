"""Channel-neutral plumbing: the ingest fan-out every connector shares, the
connector contract + manager, the Telegram sign-in step machine, the
/api/accounts routes, and the migration runner's callable steps + backups."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError
from telethon.tl import types

from backend.connectors.base import AuthStep, Capability, Connector, ConnectorStatus
from backend.connectors.manager import ConnectorManager
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import CatchUpItem, Chat
from backend.ingest import fanout
from backend.telegram.convert import message_to_row

UTC = timezone.utc


async def _chat(db, telegram_id=77, **kw):
    kw.setdefault("name", "Alice")
    kw.setdefault("type", "private")
    chat = Chat(telegram_id=telegram_id, **kw)
    db.add(chat)
    await db.commit()
    return chat


async def _msg(db, mid, text="hello", out=False, chat_id=77):
    m = types.Message(
        id=mid, peer_id=types.PeerUser(chat_id), date=datetime(2026, 1, 1, 12, tzinfo=UTC),
        message=text, out=out,
    )
    return await crud.save_message(db, **message_to_row(m, chat_id))


class _Capture:
    def __init__(self):
        self.events = []

    async def __call__(self, data):
        self.events.append(data)


def _fanout_patches(pref="all"):
    capture = _Capture()
    scam = AsyncMock()
    notify = MagicMock()
    profile = MagicMock()
    return capture, scam, notify, profile, [
        patch.object(fanout.ws_manager, "broadcast", capture),
        patch.object(fanout, "enqueue_scam", scam),
        patch.object(fanout, "send_native_notification", notify),
        patch("backend.scam.queue.note_sender_profile", profile),
        patch("backend.api.settings.get_notif_pref", return_value=pref),
    ]


# ── ingest fan-out ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_incoming_message_fans_out_to_every_feature(db):
    chat = await _chat(db, is_tracked=True)
    db.add(CatchUpItem(chat_telegram_id=77, status="handled", handled_at=datetime.utcnow()))
    await db.commit()
    saved = await _msg(db, 1, "pay me")

    capture, scam, notify, profile, ps = _fanout_patches()
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        await fanout.message_received(chat, saved, sender_profile={"bio": "b", "is_contact": False})

    async with AsyncSessionLocal() as s:
        item = (await s.execute(select(CatchUpItem))).scalar_one()
    assert item.status == "pending" and item.handled_at is None
    [event] = capture.events
    assert event["type"] == "new_message" and event["chat_id"] == 77
    assert event["message_id"] == saved.id and event["is_tracked"] is True
    assert event["message"]["text"] == "pay me"
    profile.assert_called_once_with(saved.sender_id, bio="b", is_contact=False)
    scam.assert_awaited_once_with(77, [1], "realtime")
    notify.assert_called_once_with("Alice", "pay me", chat_id=77)


@pytest.mark.asyncio
async def test_outgoing_message_only_broadcasts(db):
    chat = await _chat(db)
    db.add(CatchUpItem(chat_telegram_id=77, status="handled"))
    await db.commit()
    saved = await _msg(db, 2, "on my way", out=True)

    capture, scam, notify, profile, ps = _fanout_patches()
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        await fanout.message_received(chat, saved, sender_profile={"bio": None, "is_contact": True})

    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(CatchUpItem.status))).scalar_one() == "handled"
    assert [e["type"] for e in capture.events] == ["new_message"]
    assert not scam.await_count and not notify.called and not profile.called


@pytest.mark.asyncio
async def test_notification_respects_tracked_preference(db):
    chat = await _chat(db, is_tracked=False)
    saved = await _msg(db, 3)
    capture, scam, notify, profile, ps = _fanout_patches(pref="tracked")
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        await fanout.message_received(chat, saved)
    assert not notify.called
    assert not profile.called  # no resolved sender → no Shield profile hint
    scam.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_rescans_only_when_text_changed(db):
    await _chat(db)
    saved = await _msg(db, 4)
    capture, scam, notify, profile, ps = _fanout_patches()
    with ps[0], ps[1], ps[2], ps[3], ps[4]:
        await fanout.message_edited(77, saved, text_changed=False)
        await fanout.message_edited(77, saved, text_changed=True)
    assert [e["type"] for e in capture.events] == ["message_edited", "message_edited"]
    assert capture.events[0]["tg_message_id"] == 4
    scam.assert_awaited_once_with(77, [4], "edit")


# ── connector manager ────────────────────────────────────────────────────────

class _Fake(Connector):
    channel = "fake"
    display_name = "Fake"
    capabilities = frozenset({Capability.SEND})

    def __init__(self, channel="fake", configured=True, start_result=True):
        self.channel = channel
        self._configured = configured
        self._start_result = start_result
        self.started = 0
        self.stopped = 0

    def is_configured(self):
        return self._configured

    async def start(self):
        self.started += 1
        if isinstance(self._start_result, Exception):
            raise self._start_result
        return self._start_result

    async def stop(self):
        self.stopped += 1

    def status(self):
        return ConnectorStatus("ready")

    async def begin_auth(self, params):
        return AuthStep("f", "done")

    async def continue_auth(self, flow_id, data):
        return AuthStep(flow_id, "done")

    async def logout(self):
        pass


@pytest.mark.asyncio
async def test_manager_starts_configured_connectors_and_isolates_failures():
    m = ConnectorManager()
    ok, broken, idle = _Fake("a"), _Fake("b", start_result=RuntimeError("down")), _Fake("c", configured=False)
    for c in (ok, broken, idle):
        m.register(c)
    assert await m.start_all() == {"a:default": True, "b:default": False}
    assert idle.started == 0
    await m.stop_all()
    assert (ok.stopped, broken.stopped, idle.stopped) == (1, 1, 1)
    assert m.get("a") is ok and m.get("a:default") is ok and m.get("zzz") is None
    with pytest.raises(ValueError):
        m.register(_Fake("a"))


def test_describe_lists_sorted_capabilities():
    d = _Fake("a").describe()
    assert d == {
        "account_key": "a:default", "channel": "a", "display_name": "Fake",
        "capabilities": ["send"], "configured": True, "status": {"state": "ready", "error": None},
    }


# ── Telegram sign-in step machine ────────────────────────────────────────────

def _tg_patches(conn, *, configured):
    from backend.telegram import connector as mod
    rt = mod.runtime
    me = MagicMock(username="ann", first_name="Ann")
    return [
        patch.object(mod.tg_client, "has_credentials", return_value=configured),
        patch.object(mod.vault, "save_secret"),
        patch.object(rt, "replace_client", AsyncMock(return_value=False)),
        patch.object(rt, "ensure_started", AsyncMock(return_value=True)),
        patch.object(rt, "send_code", AsyncMock(return_value="HASH")),
        patch.object(rt, "sign_in", AsyncMock(side_effect=SessionPasswordNeededError(None))),
        patch.object(rt, "sign_in_2fa", AsyncMock()),
        patch.object(mod.tg_client, "persist_session", AsyncMock()),
        patch.object(mod.tg_client, "get_me", AsyncMock(return_value=me)),
    ]


@pytest.mark.asyncio
async def test_telegram_flow_credentials_phone_code_2fa_done():
    from contextlib import ExitStack
    from backend.telegram.connector import TelegramConnector
    conn = TelegramConnector()
    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in _tg_patches(conn, configured=False)]
        step = await conn.begin_auth({})
        assert step.kind == "fields" and [f.name for f in step.fields] == ["api_id", "api_hash"]

        bad = await conn.continue_auth(step.flow_id, {"api_id": "abc", "api_hash": "h"})
        assert bad.message == "API ID must be a number" and bad.fields[0].name == "api_id"

        step = await conn.continue_auth(step.flow_id, {"api_id": " 123 ", "api_hash": "h"})
        assert [f.name for f in step.fields] == ["phone"]
        mocks[1].assert_any_call("tg_api_id", "123")

        step = await conn.continue_auth(step.flow_id, {"phone": "+15550001"})
        assert [f.name for f in step.fields] == ["code"]

        step = await conn.continue_auth(step.flow_id, {"code": "11111"})
        assert [f.name for f in step.fields] == ["password"]
        mocks[5].assert_awaited_with("+15550001", "11111", "HASH")

        done = await conn.continue_auth(step.flow_id, {"password": "pw"})
        assert done.kind == "done" and done.message == "Signed in as ann"
        mocks[3].assert_awaited()  # runtime started after sign-in

    from backend.connectors.base import AuthFlowError
    with pytest.raises(AuthFlowError):
        await conn.continue_auth(done.flow_id, {})


@pytest.mark.asyncio
async def test_telegram_flow_wrong_code_stays_on_code_step():
    from contextlib import ExitStack
    from backend.telegram.connector import TelegramConnector
    conn = TelegramConnector()
    with ExitStack() as stack:
        mocks = [stack.enter_context(p) for p in _tg_patches(conn, configured=True)]
        mocks[5].side_effect = PhoneCodeInvalidError(None)
        step = await conn.begin_auth({})
        assert [f.name for f in step.fields] == ["phone"]  # credentials already stored
        step = await conn.continue_auth(step.flow_id, {"phone": "+1"})
        again = await conn.continue_auth(step.flow_id, {"code": "0"})
    assert [f.name for f in again.fields] == ["code"]
    assert again.message == "Invalid code. Please try again."


@pytest.mark.asyncio
async def test_telegram_flow_keeps_a_bounded_number_of_open_flows():
    from backend.telegram import connector as mod
    conn = mod.TelegramConnector()
    with patch.object(mod.tg_client, "has_credentials", return_value=True):
        first = await conn.begin_auth({})
        for _ in range(mod.MAX_OPEN_FLOWS):
            await conn.begin_auth({})
    assert first.flow_id not in conn._flows and len(conn._flows) == mod.MAX_OPEN_FLOWS


# ── /api/accounts ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_accounts_routes_list_and_drive_auth(api):
    from backend.connectors.manager import manager
    from backend.telegram.connector import connector as telegram

    with patch.object(telegram, "is_configured", return_value=False):
        r = await api.get("/api/accounts")
        assert r.status_code == 200
        [tg] = [a for a in r.json() if a["channel"] == "telegram"]
        assert tg["account_key"] == "telegram:default" and tg["configured"] is False
        assert "forum_topics" in tg["capabilities"]

        step = (await api.post("/api/accounts/telegram/auth", json={})).json()
        assert step["kind"] == "fields" and step["fields"][0]["name"] == "api_id"

    assert (await api.post("/api/accounts/telegram/auth/nope", json={})).status_code == 404
    assert (await api.post("/api/accounts/whatsapp/auth", json={})).status_code == 404

    with patch.object(telegram, "logout", AsyncMock()) as logout:
        assert (await api.post("/api/accounts/telegram:default/logout")).status_code == 200
    logout.assert_awaited_once()
    assert manager.get("telegram") is telegram


@pytest.mark.asyncio
async def test_accounts_routes_require_session_token(db_engine):
    from backend.api.router import create_app
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1") as c:
        assert (await c.get("/api/accounts")).status_code == 401
        assert (await c.post("/api/accounts/telegram/auth", json={})).status_code == 401
        assert (await c.post("/api/accounts/telegram/logout")).status_code == 401


# ── migration runner: callable steps + backups ───────────────────────────────

async def _runner_db(tmp_path, migrations, *, fresh, seed_versions=()):
    from backend.db.migrations import areas, runner

    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with eng.connect() as c:
        await c.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        await c.exec_driver_sql("INSERT INTO t (v) VALUES ('old')")
        await c.exec_driver_sql(
            "CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE, color TEXT, "
            "is_default BOOLEAN, created_at DATETIME)"
        )
        await c.commit()
    orig_core, orig_areas = list(runner.MIGRATIONS), list(areas.MODULES)
    runner.MIGRATIONS[:] = migrations
    areas.MODULES[:] = []
    try:
        async with eng.connect() as c:
            await runner.run_migrations(c, fresh=fresh)
    finally:
        runner.MIGRATIONS[:] = orig_core
        areas.MODULES[:] = orig_areas
    return eng


async def _rewrite(conn):
    await conn.exec_driver_sql("UPDATE t SET v = 'new'")


async def _rewrite_then_fail(conn):
    await conn.exec_driver_sql("UPDATE t SET v = 'half'")
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_callable_step_runs_and_backup_is_taken_first(tmp_path):
    eng = await _runner_db(tmp_path, [(120, "rewrite", [_rewrite])], fresh=False)
    async with eng.connect() as c:
        assert (await c.exec_driver_sql("SELECT v FROM t")).scalar() == "new"
    await eng.dispose()
    [backup] = list((tmp_path / "backups").glob("m-pre-v120-*.db"))
    import sqlite3
    with sqlite3.connect(str(backup)) as b:
        assert b.execute("SELECT v FROM t").fetchone()[0] == "old"


@pytest.mark.asyncio
async def test_failing_callable_step_rolls_back(tmp_path):
    with pytest.raises(RuntimeError):
        await _runner_db(tmp_path, [(121, "bad", ["ALTER TABLE t ADD COLUMN w TEXT", _rewrite_then_fail])], fresh=False)
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with eng.connect() as c:
        assert (await c.exec_driver_sql("SELECT v FROM t")).scalar() == "old"
        cols = [r[1] for r in (await c.exec_driver_sql("PRAGMA table_info(t)")).all()]
        versions = [r[0] for r in (await c.exec_driver_sql("SELECT version FROM schema_migrations")).all()]
    await eng.dispose()
    assert cols == ["id", "v"] and 121 not in versions


@pytest.mark.asyncio
async def test_no_backup_on_fresh_db_or_below_threshold(tmp_path):
    eng = await _runner_db(tmp_path, [(119, "small", [_rewrite]), (120, "big", [_rewrite])], fresh=True)
    await eng.dispose()
    assert not (tmp_path / "backups").exists()
    (tmp_path / "m.db").unlink()
    eng = await _runner_db(tmp_path, [(119, "small", [_rewrite])], fresh=False)
    await eng.dispose()
    assert not (tmp_path / "backups").exists()


@pytest.mark.asyncio
async def test_only_newest_backups_are_kept(tmp_path):
    import os
    from backend.db.migrations import runner

    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(4):
        old = backups / f"m-pre-v120-2020010100000{i}.db"
        old.write_bytes(b"")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    eng = await _runner_db(tmp_path, [(120, "rewrite", [_rewrite])], fresh=False)
    await eng.dispose()
    kept = sorted(p.name for p in backups.iterdir())
    assert len(kept) == runner.BACKUPS_KEPT
    assert "m-pre-v120-20200101000000.db" not in kept and "m-pre-v120-20200101000001.db" not in kept
