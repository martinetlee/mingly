"""Channel-neutral plumbing: the ingest fan-out every connector shares, the
connector contract + manager, the Telegram sign-in step machine, the
/api/accounts routes, and the migration runner's callable steps + backups."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError

from backend.connectors.base import AuthStep, Capability, Connector, ConnectorStatus
from backend.connectors.manager import ConnectorManager
from backend.db.database import AsyncSessionLocal
from backend.db.models import CatchUpItem
from backend.ingest import fanout
from tests import factories as f


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
    triage = AsyncMock()
    return capture, scam, notify, profile, triage, [
        patch.object(fanout.ws_manager, "broadcast", capture),
        patch.object(fanout, "inspect_messages", scam),
        patch.object(fanout, "send_native_notification", notify),
        patch.object(fanout, "stamp_triage", triage),
        patch("backend.scam.queue.note_sender_profile", profile),
        patch("backend.prefs.get_notif_pref", return_value=pref),
    ]


def _enter(stack, patches):
    for p in patches:
        stack.enter_context(p)


# ── ingest fan-out ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_incoming_message_fans_out_to_every_feature(db):
    from contextlib import ExitStack
    conv = await f.conversation(db, is_tracked=True)
    ann = await f.identity(db, "77", display_name="Alice")
    db.add(CatchUpItem(conversation_id=conv.id, status="handled", handled_at=datetime.utcnow()))
    await db.commit()
    saved = await f.message(db, conv, 1, "pay me", sender=ann)

    capture, scam, notify, profile, triage, ps = _fanout_patches()
    with ExitStack() as stack:
        _enter(stack, ps)
        await fanout.message_received(conv, saved, sender_profile={"bio": "b", "is_contact": False})

    async with AsyncSessionLocal() as s:
        item = (await s.execute(select(CatchUpItem))).scalar_one()
    assert item.status == "pending" and item.handled_at is None
    triage.assert_awaited_once_with(conv.id, saved.date)
    [event] = capture.events
    assert event["type"] == "new_message" and event["conversation_id"] == conv.id
    assert event["message_id"] == saved.id and event["is_tracked"] is True
    assert event["message"]["text"] == "pay me" and event["message"]["sender"]["person_id"] == ann.person_id
    profile.assert_called_once_with(ann.id, bio="b", is_contact=False)
    scam.assert_awaited_once_with(conv.id, [saved.id], "realtime")
    notify.assert_called_once_with("Alice", "Alice: pay me", conversation_id=conv.id)


@pytest.mark.asyncio
async def test_outgoing_message_only_broadcasts(db):
    from contextlib import ExitStack
    conv = await f.conversation(db)
    db.add(CatchUpItem(conversation_id=conv.id, status="handled"))
    await db.commit()
    saved = await f.message(db, conv, 2, "on my way", out=True)

    capture, scam, notify, profile, triage, ps = _fanout_patches()
    with ExitStack() as stack:
        _enter(stack, ps)
        await fanout.message_received(conv, saved, sender_profile={"bio": None, "is_contact": True})

    async with AsyncSessionLocal() as s:
        assert (await s.execute(select(CatchUpItem.status))).scalar_one() == "handled"
    assert [e["type"] for e in capture.events] == ["new_message"]
    assert not scam.await_count and not notify.called and not profile.called and not triage.called


@pytest.mark.asyncio
async def test_notification_respects_tracked_preference(db):
    from contextlib import ExitStack
    conv = await f.conversation(db, is_tracked=False)
    saved = await f.message(db, conv, 3)
    capture, scam, notify, profile, triage, ps = _fanout_patches(pref="tracked")
    with ExitStack() as stack:
        _enter(stack, ps)
        await fanout.message_received(conv, saved)
    assert not notify.called
    assert not profile.called  # no resolved sender → no Shield profile hint
    scam.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_rescans_only_when_text_changed(db):
    from contextlib import ExitStack
    conv = await f.conversation(db)
    saved = await f.message(db, conv, 4)
    capture, scam, notify, profile, triage, ps = _fanout_patches()
    with ExitStack() as stack:
        _enter(stack, ps)
        await fanout.message_edited(conv.id, saved, text_changed=False)
        await fanout.message_edited(conv.id, saved, text_changed=True)
    assert [e["type"] for e in capture.events] == ["message_edited", "message_edited"]
    assert capture.events[0]["message_id"] == saved.id
    scam.assert_awaited_once_with(conv.id, [saved.id], "edit")


# ── connector manager ────────────────────────────────────────────────────────

class _Fake(Connector):
    channel = "fake"
    display_name = "Fake"
    capabilities = frozenset({Capability.SEND})

    def __init__(self, channel="fake", configured=True, start_result=True):
        super().__init__(account_id=None)
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

    @property
    def account_key(self):
        return f"{self.channel}:default"


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
        "account_key": "a:default", "account_id": None, "channel": "a", "display_name": "Fake",
        "capabilities": ["send"], "configured": True, "status": {"state": "ready", "error": None},
    }


class _Mail(Connector):
    """A multi-account channel whose sign-in creates the account row."""
    channel = "mail"
    display_name = "Mail"
    capabilities = frozenset({Capability.SEND, Capability.SUBJECT})
    multi_account = True
    started: list = []

    def is_configured(self):
        return self.account_id is not None

    async def start(self):
        _Mail.started.append(self.account_id)
        return True

    async def stop(self):
        pass

    def status(self):
        return ConnectorStatus("ready" if self.account_id else "stopped")

    async def begin_auth(self, params):
        return AuthStep("flow-1", "fields")

    async def continue_auth(self, flow_id, data):
        from backend.db.models import Account
        async with AsyncSessionLocal() as s:
            acc = Account(channel="mail", external_id=data["email"], label=data["email"])
            s.add(acc)
            await s.commit()
            self.account_id = acc.id
        return AuthStep(flow_id, "done")

    async def logout(self):
        pass


@pytest.mark.asyncio
async def test_multi_account_sign_in_registers_and_starts_the_new_account(db):
    import asyncio
    m = ConnectorManager()
    m.register_factory("mail", "Mail", lambda account_id: _Mail(account_id))
    _Mail.started = []
    assert {"channel": "mail", "display_name": "Mail", "multi_account": True} in m.channels()

    step = await m.begin_auth("mail", {})
    assert step.kind == "fields" and m.all() == []  # provisional until the flow creates the account
    done = await m.continue_auth(step.flow_id, {"email": "a@x.com"})
    await asyncio.sleep(0.01)
    [conn] = m.all()
    assert done.kind == "done" and conn.account_key == f"mail:{conn.account_id}" and _Mail.started == [conn.account_id]
    from backend.connectors.base import AuthFlowError
    with pytest.raises(AuthFlowError):
        await m.continue_auth(step.flow_id, {})

    # A second account of the same channel is a separate connector.
    step = await m.begin_auth("mail", {})
    await m.continue_auth(step.flow_id, {"email": "b@x.com"})
    await asyncio.sleep(0.01)
    assert len(m.all()) == 2 and m.get("mail") is None  # ambiguous by channel name

    # A fresh manager (app restart) recreates both from the accounts table.
    m2 = ConnectorManager()
    m2.register_factory("mail", "Mail", lambda account_id: _Mail(account_id))
    await m2.load_accounts()
    assert sorted(c.account_id for c in m2.all()) == sorted(c.account_id for c in m.all())
    assert m2.for_account("mail", conn.account_id).account_id == conn.account_id

    # Removing deletes the account (and with it its conversations).
    conv = await f.conversation(db, "t1", title="Hi", kind="email_thread", channel="mail")
    assert conv.account_id in [c.account_id for c in m2.all()]
    assert await m2.remove(f"mail:{conv.account_id}") is True
    from backend.db.models import Account, Conversation
    async with AsyncSessionLocal() as s:
        assert await s.get(Account, conv.account_id) is None
        assert await s.get(Conversation, conv.id) is None


@pytest.mark.asyncio
async def test_capabilities_come_from_the_conversations_own_account(db):
    from backend.connectors import manager as mgr_mod
    from backend.serializers import conversation_out
    conn = _Mail(None)
    conv = await f.conversation(db, "t1", title="Hi", kind="email_thread", channel="mail")
    conn.account_id = conv.account_id
    other = await f.conversation(db, "t2", title="Other", kind="email_thread", channel="mail")
    with patch.object(mgr_mod.manager, "_connectors", {**mgr_mod.manager._connectors, conn.account_key: conn}):
        assert conversation_out(conv)["capabilities"] == ["send", "subject"]
        if other.account_id != conv.account_id:
            assert conversation_out(other)["capabilities"] == []


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
        assert {"channel": "telegram", "display_name": "Telegram", "multi_account": False} in r.json()["channels"]
        [tg] = [a for a in r.json()["accounts"] if a["channel"] == "telegram"]
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
    from backend.db.migrations import runner

    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with eng.connect() as c:
        await c.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        await c.exec_driver_sql("INSERT INTO t (v) VALUES ('old')")
        await c.exec_driver_sql(
            "CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE, color TEXT, "
            "is_default BOOLEAN, created_at DATETIME)"
        )
        await c.commit()
    orig = list(runner.MIGRATIONS)
    runner.MIGRATIONS[:] = migrations
    try:
        async with eng.connect() as c:
            await runner.run_migrations(c, fresh=fresh)
    finally:
        runner.MIGRATIONS[:] = orig
    return eng


async def _rewrite(conn):
    await conn.exec_driver_sql("UPDATE t SET v = 'new'")


async def _rewrite_then_fail(conn):
    await conn.exec_driver_sql("UPDATE t SET v = 'half'")
    raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_callable_step_runs_and_backup_is_taken_first(tmp_path):
    eng = await _runner_db(tmp_path, [(3, "rewrite", [_rewrite])], fresh=False)
    async with eng.connect() as c:
        assert (await c.exec_driver_sql("SELECT v FROM t")).scalar() == "new"
    await eng.dispose()
    [backup] = list((tmp_path / "backups").glob("m-pre-v3-*.db"))
    import sqlite3
    with sqlite3.connect(str(backup)) as b:
        assert b.execute("SELECT v FROM t").fetchone()[0] == "old"


@pytest.mark.asyncio
async def test_failing_callable_step_rolls_back(tmp_path):
    with pytest.raises(RuntimeError):
        await _runner_db(tmp_path, [(4, "bad", ["ALTER TABLE t ADD COLUMN w TEXT", _rewrite_then_fail])], fresh=False)
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with eng.connect() as c:
        assert (await c.exec_driver_sql("SELECT v FROM t")).scalar() == "old"
        cols = [r[1] for r in (await c.exec_driver_sql("PRAGMA table_info(t)")).all()]
        versions = [r[0] for r in (await c.exec_driver_sql("SELECT version FROM schema_migrations")).all()]
    await eng.dispose()
    assert cols == ["id", "v"] and 4 not in versions


@pytest.mark.asyncio
async def test_no_backup_on_fresh_db_or_without_pending_migrations(tmp_path):
    eng = await _runner_db(tmp_path, [(1, "a", [_rewrite]), (2, "b", [_rewrite])], fresh=True)
    await eng.dispose()
    assert not (tmp_path / "backups").exists()
    (tmp_path / "m.db").unlink()
    eng = await _runner_db(tmp_path, [], fresh=False)
    await eng.dispose()
    assert not (tmp_path / "backups").exists()


@pytest.mark.asyncio
async def test_only_newest_backups_are_kept(tmp_path):
    import os
    from backend.db.migrations import runner

    backups = tmp_path / "backups"
    backups.mkdir()
    for i in range(4):
        old = backups / f"m-pre-v5-2020010100000{i}.db"
        old.write_bytes(b"")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    eng = await _runner_db(tmp_path, [(5, "rewrite", [_rewrite])], fresh=False)
    await eng.dispose()
    kept = sorted(p.name for p in backups.iterdir())
    assert len(kept) == runner.BACKUPS_KEPT
    assert "m-pre-v5-20200101000000.db" not in kept and "m-pre-v5-20200101000001.db" not in kept


@pytest.mark.asyncio
async def test_telegram_sign_in_can_restart_at_the_credentials_step():
    from backend.telegram import connector as mod
    conn = mod.TelegramConnector()
    with patch.object(mod.tg_client, "has_credentials", return_value=True):
        assert [f.name for f in (await conn.begin_auth({})).fields] == ["phone"]
        step = await conn.begin_auth({"reset_credentials": True})
    assert [f.name for f in step.fields] == ["api_id", "api_hash"]


@pytest.mark.asyncio
async def test_accounts_list_carries_each_accounts_label(api, db):
    acc = await f.account(db)
    acc.label = "@ann"
    await db.commit()
    [tg] = [a for a in (await api.get("/api/accounts")).json()["accounts"] if a["channel"] == "telegram"]
    assert tg["label"] == "@ann"
