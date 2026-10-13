"""SMS / iMessage connector over a synthetic Messages database.

Every test builds its own ``chat.db`` in a temp dir with the subset of the
real schema the connector reads (chat, handle, message, the join tables,
attachment). Nothing here opens the user's real Messages database: each
connector is given the fixture's path, and HOME points into the temp dir
whenever a default path is involved.
"""

import asyncio
import json
import os
import sqlite3
import threading
import time
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.connectors.base import AuthFlowError, Capability, OutgoingMessage, UnsupportedOperation
from backend.connectors.manager import ConnectorManager
from backend.connectors.sms import chatdb, register
from backend.connectors.sms import connector as mod
from backend.connectors.sms.chatdb import APPLE_EPOCH, ChatDbUnavailable, to_apple_ns
from backend.connectors.sms.connector import SmsConnector
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Account, Conversation, Identity, Message, MessageAttachment
from backend.ingest import fanout
from backend.media import MediaNotFound
from tests import factories as f
from tests.test_sms_typedstream import blob
from backend.people import identities as person_identities

ANN = "+15551234567"  # iMessage, in the direct chat and the group
BOB = "Bob@Example.com"  # iMessage by email, in the group
CAROL = "+447700900123"  # SMS
ME = "+15550009999"
DIRECT = "iMessage;-;+15551234567"
SMS_CHAT = "SMS;-;+447700900123"
GROUP = "iMessage;+;chat424242"

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL, service TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, style INTEGER,
    chat_identifier TEXT, service_name TEXT, display_name TEXT, last_addressed_handle TEXT);
CREATE TABLE message (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, text TEXT,
    attributedBody BLOB, handle_id INTEGER DEFAULT 0, service TEXT, date INTEGER, date_edited INTEGER DEFAULT 0,
    is_from_me INTEGER DEFAULT 0, cache_has_attachments INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0,
    other_handle INTEGER DEFAULT 0, group_title TEXT, group_action_type INTEGER DEFAULT 0,
    associated_message_guid TEXT, associated_message_type INTEGER DEFAULT 0, associated_message_emoji TEXT,
    thread_originator_guid TEXT);
CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER, message_date INTEGER DEFAULT 0,
    PRIMARY KEY (chat_id, message_id));
CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER, UNIQUE (chat_id, handle_id));
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT UNIQUE NOT NULL, filename TEXT,
    mime_type TEXT, transfer_name TEXT, total_bytes INTEGER DEFAULT 0);
CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER, UNIQUE (message_id, attachment_id));
"""


class FakeMessages:
    """Writes a synthetic chat.db the way Messages lays it out."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with self._db() as c:
            c.executescript(SCHEMA)
        self.handles = {h: self._insert("handle", id=h, service=s)
                        for h, s in ((ANN, "iMessage"), (BOB, "iMessage"), (CAROL, "SMS"))}
        self.chats = {
            DIRECT: self.chat(DIRECT, 45, ANN, "iMessage", [ANN]),
            SMS_CHAT: self.chat(SMS_CHAT, 45, CAROL, "SMS", [CAROL]),
            GROUP: self.chat(GROUP, 43, "chat424242", "iMessage", [ANN, BOB], display_name="Trip 2026"),
        }

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))

    def _insert(self, table: str, **values) -> int:
        with self._db() as c:
            cols = ", ".join(values)
            return c.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * len(values))})",
                             list(values.values())).lastrowid

    def chat(self, guid, style, identifier, service, members, display_name=None) -> int:
        chat_id = self._insert("chat", guid=guid, style=style, chat_identifier=identifier, service_name=service,
                               display_name=display_name, last_addressed_handle=ME)
        for h in members:
            self._insert("chat_handle_join", chat_id=chat_id, handle_id=self.handles[h])
        return chat_id

    def message(self, guid, chat, *, sender=None, when=None, seconds=False, attachments=(), **cols) -> int:
        when = when or datetime.utcnow()
        date = int((when - APPLE_EPOCH).total_seconds()) if seconds else to_apple_ns(when)
        if sender is not None:
            cols["handle_id"] = self.handles[sender]
        if "other_handle" in cols:
            cols["other_handle"] = self.handles[cols["other_handle"]]
        rowid = self._insert("message", guid=guid, date=date, cache_has_attachments=int(bool(attachments)), **cols)
        self._insert("chat_message_join", chat_id=self.chats[chat], message_id=rowid)
        for i, (filename, mime, name, size) in enumerate(attachments):
            att = self._insert("attachment", guid=f"{guid}-att{i}", filename=filename, mime_type=mime,
                               transfer_name=name, total_bytes=size)
            self._insert("message_attachment_join", message_id=rowid, attachment_id=att)
        return rowid


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A fake home with the Messages layout; default paths resolve into it."""
    h = tmp_path / "home"
    (h / "Library" / "Messages" / "Attachments").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


@pytest.fixture
def messages(home) -> FakeMessages:
    return FakeMessages(home / "Library" / "Messages" / "chat.db")


@pytest.fixture
def photo(home) -> Path:
    p = home / "Library" / "Messages" / "Attachments" / "ab" / "11" / "AT-1" / "IMG_0001.jpeg"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"\xff\xd8\xff fake jpeg")
    return p


@pytest.fixture
def events():
    """Stub fan-out side effects; record live messages and edits."""
    with ExitStack() as stack:
        broadcast = stack.enter_context(patch("backend.ws.manager.ws_manager.broadcast", AsyncMock()))
        received = stack.enter_context(patch.object(fanout, "message_received", AsyncMock()))
        edited = stack.enter_context(patch.object(fanout, "message_edited", AsyncMock()))
        yield MagicMock(broadcast=broadcast, received=received, edited=edited)


def _connector(messages: FakeMessages) -> SmsConnector:
    return SmsConnector(db_path=messages.path)


def _drop(coro, **kw):
    coro.close()


async def _connected(db, messages: FakeMessages) -> SmsConnector:
    """Connect without the background (re)start sign-in spawns: tests drive
    ``sync_once`` themselves or call ``start``."""
    conn = _connector(messages)
    with patch.object(mod, "spawn", _drop):
        step = await conn.begin_auth({})
    assert step.kind == "done", step.message
    return conn


def _seed(messages: FakeMessages, photo: Path) -> None:
    now = datetime.utcnow()
    messages.message("old-1", DIRECT, sender=ANN, text="from long ago", when=now - timedelta(days=200))
    messages.message("m1", DIRECT, sender=ANN, text="hello", when=now - timedelta(days=2))
    messages.message("m2", DIRECT, sender=ANN, is_from_me=1, attributedBody=blob("hi there 👋"),
                     when=now - timedelta(days=2, minutes=-1))
    messages.message("m3", SMS_CHAT, sender=CAROL, text="Your code is 1234", seconds=True,
                     when=datetime(*(now - timedelta(days=1)).timetuple()[:6]), service="SMS")
    rel = "~/" + str(photo.relative_to(Path(os.environ["HOME"])))
    messages.message("m4", GROUP, sender=BOB, text="￼Who's in?", when=now - timedelta(hours=20),
                     attachments=[(rel, "image/jpeg", "IMG_0001.jpeg", 11)])
    messages.message("t1", GROUP, sender=ANN, text="Loved “Who's in?”", associated_message_type=2000,
                     associated_message_guid="p:0/m4", when=now - timedelta(hours=19))
    messages.message("m5", GROUP, is_from_me=1, text="me!", thread_originator_guid="m4",
                     when=now - timedelta(hours=18))
    messages.message("e1", GROUP, sender=ANN, item_type=2, group_title="Trip 2026", when=now - timedelta(hours=17))
    messages.message("t2", GROUP, is_from_me=1, associated_message_type=2001, associated_message_guid="p:0/m4",
                     when=now - timedelta(hours=16))
    messages.message("t3", GROUP, sender=ANN, associated_message_type=3000, associated_message_guid="p:0/m4",
                     when=now - timedelta(hours=15))
    messages.message("e2", GROUP, sender=ANN, item_type=1, other_handle=CAROL, when=now - timedelta(hours=14))
    messages.message("blank", GROUP, sender=ANN, when=now - timedelta(hours=13))  # nothing to show


async def _conv(external_id: str) -> Conversation:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Conversation).where(Conversation.external_id == external_id))).scalar_one()


async def _msgs(external_id: str) -> list[Message]:
    conv = await _conv(external_id)
    async with AsyncSessionLocal() as db:
        return list((await db.execute(
            select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
        )).scalars().all())


async def _state(account_id: int) -> dict:
    async with AsyncSessionLocal() as db:
        acc = await crud.get_account(db, account_id)
    return json.loads(acc.sync_state_json or "{}")


# ── detection / sign-in ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_database_is_an_error_step(db, home):
    conn = SmsConnector()  # default path, inside the fake home
    assert conn.db_path == home / "Library" / "Messages" / "chat.db"
    step = await conn.begin_auth({})
    assert step.kind == "error" and "No Messages database" in step.message
    assert not conn.is_configured()
    assert (await crud.get_accounts(db, "sms")) == []


@pytest.mark.asyncio
async def test_unreadable_database_asks_for_full_disk_access(db, messages):
    os.chmod(messages.path, 0)
    try:
        step = await _connector(messages).begin_auth({})
    finally:
        os.chmod(messages.path, 0o600)
    assert step.kind == "error" and step.title == "Full Disk Access needed"
    assert "Privacy & Security → Full Disk Access" in step.message and "reopen Mingly" in step.message
    assert (await crud.get_accounts(db, "sms")) == []


@pytest.mark.asyncio
async def test_denied_messages_folder_is_reported_as_denied(db, home):
    folder = home / "Library" / "Messages"
    os.chmod(folder, 0)  # what macOS privacy protection looks like without access
    try:
        with pytest.raises(ChatDbUnavailable) as exc:
            chatdb.connect(folder / "chat.db")
    finally:
        os.chmod(folder, 0o700)
    assert exc.value.reason == "denied"


def test_sqlite_authorization_failure_is_denied(tmp_path, monkeypatch, messages):
    real = sqlite3.connect

    def deny(*a, **kw):
        c = real(*a, **kw)
        c.set_authorizer(lambda *args: sqlite3.SQLITE_DENY)
        return c
    monkeypatch.setattr(chatdb.sqlite3, "connect", deny)
    with pytest.raises(ChatDbUnavailable) as exc:
        chatdb.connect(messages.path)
    assert exc.value.reason == "denied"


def test_not_a_messages_database(tmp_path):
    other = tmp_path / "other.db"
    sqlite3.connect(str(other)).execute("CREATE TABLE t (x)").connection.commit()
    with pytest.raises(ChatDbUnavailable) as exc:
        chatdb.connect(other)
    assert exc.value.reason == "invalid"
    junk = tmp_path / "junk.db"
    junk.write_text("not sqlite")
    with pytest.raises(ChatDbUnavailable) as exc:
        chatdb.connect(junk)
    assert exc.value.reason == "invalid"


@pytest.mark.asyncio
async def test_connect_creates_the_account_and_logout_disconnects(db, messages, events):
    conn = _connector(messages)
    assert not conn.is_configured() and await conn.start() is False
    with patch.object(mod, "spawn", _drop):
        step = await conn.begin_auth({})
    assert step.kind == "done" and step.fields == []
    assert "read-only" in step.message and "replying from Mingly isn't supported" in step.message
    [acc] = await crud.get_accounts(db, "sms")
    assert (acc.channel, acc.external_id, acc.label) == ("sms", "local", "Messages on this Mac")
    assert conn.account_id == acc.id and conn.is_configured()
    me = await crud.get_identity(db, acc.self_identity_id)
    assert (me.channel, me.external_id, me.phone) == ("sms", "me", ME)
    assert conn.account_key == "sms:default"
    assert conn.describe()["capabilities"] == [Capability.HISTORY_BACKFILL.value]
    with pytest.raises(AuthFlowError):
        await conn.continue_auth(step.flow_id, {})

    # A fresh process learns the stored account from the manager (boot path).
    m = ConnectorManager()
    fresh = _connector(messages)
    m.register(fresh)
    assert not fresh.is_configured()
    await m.load_accounts()
    assert fresh.is_configured() and fresh.account_id == acc.id

    await conn.logout()
    assert not conn.is_configured() and conn.account_id is None
    await db.refresh(acc)
    assert acc.external_id is None
    await m.load_accounts()
    assert not fresh.is_configured()
    # Connecting again reuses the account.
    with patch.object(mod, "spawn", _drop):
        await conn.begin_auth({})
    assert [a.id for a in await crud.get_accounts(db, "sms")] == [acc.id]


@pytest.mark.asyncio
async def test_read_only(db, messages):
    conn = await _connected(db, messages)
    with pytest.raises(UnsupportedOperation):
        await conn.send(None, OutgoingMessage(text="hi"))


def test_register_adds_a_single_account_connector():
    m = ConnectorManager()
    register(m)
    assert m.get("sms").account_key == "sms:default"
    assert {"channel": "sms", "display_name": "SMS / iMessage", "multi_account": False, "setup": None} in m.channels()


def test_the_app_manager_has_sms():
    from backend.connectors.manager import manager
    assert isinstance(manager.get("sms"), SmsConnector)


# ── import ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_initial_import(db, messages, photo, events):
    _seed(messages, photo)
    conn = await _connected(db, messages)
    await conn.sync_once()

    direct = await _conv(DIRECT)
    assert (direct.kind, direct.title) == ("direct", "+1 555-123-4567")
    peer = await crud.get_identity(db, direct.peer_identity_id)
    assert (peer.channel, peer.external_id, peer.phone) == ("sms", ANN, ANN)
    m1, m2 = await _msgs(DIRECT)  # old-1 is outside the 90-day window
    assert (m1.external_id, m1.text, m1.is_outgoing) == ("m1", "hello", False)
    assert m1.sender_identity_id == peer.id and m1.seq == 2
    assert (m2.text, m2.is_outgoing) == ("hi there 👋", True)  # attributedBody only
    assert m2.meta_json is None

    [sms] = await _msgs(SMS_CHAT)
    assert sms.text == "Your code is 1234"
    expected = datetime(*(datetime.utcnow() - timedelta(days=1)).timetuple()[:6])
    assert abs((sms.date - expected).total_seconds()) < 5  # stored in seconds, not nanoseconds
    assert json.loads(sms.meta_json) == {"service": "SMS"}

    group = await _conv(GROUP)
    assert (group.kind, group.title, group.members_count, group.peer_identity_id) == ("group", "Trip 2026", 3, None)
    rows = await _msgs(GROUP)
    assert [m.external_id for m in rows] == ["m4", "m5", "e1", "e2"]  # tapbacks and the blank row aren't messages
    m4, m5, e1, e2 = rows
    assert m4.text == "Who's in?"
    bob = await crud.get_identity(db, m4.sender_identity_id)
    assert bob.external_id == "bob@example.com" and bob.phone is None
    [att] = (await db.execute(select(MessageAttachment).where(MessageAttachment.message_id == m4.id))).scalars().all()
    assert (att.kind, att.filename, att.mime_type, att.size) == ("photo", "IMG_0001.jpeg", "image/jpeg", 11)
    assert att.external_ref.startswith("~/Library/Messages/Attachments/")
    # Loved by Ann, liked by me, then Ann took hers back.
    assert json.loads(m4.reactions_json) == [{"emoji": "👍", "count": 1}]
    assert m4.my_reaction == "👍"
    assert json.loads(m4.meta_json)["sms_reactions"] == {"me": "👍"}
    assert (m5.reply_to_external_id, m5.is_outgoing) == ("m4", True)
    assert (e1.kind, e1.text) == ("service", "+1 555-123-4567 named the conversation “Trip 2026”")
    assert (e2.kind, e2.text) == ("service", "+1 555-123-4567 added +44 7700 900123")

    state = await _state(conn.account_id)
    assert state["backfilled"] is True and state["last_rowid"] == 12 and "backfill_rowid" not in state
    events.received.assert_not_awaited()  # history isn't announced as new messages

    # Running again changes nothing (idempotent, reactions kept).
    await conn.sync_once()
    assert len(await _msgs(GROUP)) == 4
    assert json.loads((await _msgs(GROUP))[0].reactions_json) == [{"emoji": "👍", "count": 1}]


@pytest.mark.asyncio
async def test_reimport_keeps_reactions(db, messages, photo, events):
    _seed(messages, photo)
    conn = await _connected(db, messages)
    await conn.sync_once()
    async with AsyncSessionLocal() as s:
        acc = await s.get(Account, conn.account_id)
        acc.sync_state_json = None  # e.g. a reconnect that starts over
        await s.commit()
    await conn.sync_once()
    m4 = (await _msgs(GROUP))[0]
    assert json.loads(m4.reactions_json) == [{"emoji": "👍", "count": 1}] and m4.my_reaction == "👍"


@pytest.mark.asyncio
async def test_backfill_window_pref(db, messages, photo, events):
    from backend.prefs import save_prefs
    _seed(messages, photo)
    save_prefs({"sms_backfill_days": 1})
    conn = await _connected(db, messages)
    await conn.sync_once()
    # m1 / m2 are two days old; the group is within the day.
    assert (await crud.get_conversation_by_external(db, conn.account_id, DIRECT)) is None
    assert [m.external_id for m in await _msgs(GROUP)][:1] == ["m4"]
    # The window doesn't hold back the poll: last_rowid moved past everything.
    assert (await _state(conn.account_id))["last_rowid"] == 12


# ── live polling ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_poll_ingests_new_rows_as_realtime(db, messages, photo, events):
    _seed(messages, photo)
    conn = await _connected(db, messages)
    await conn.sync_once()

    messages.message("n1", DIRECT, sender=ANN, text="are you there?")
    messages.message("n0", DIRECT, sender=ANN, text="sent while the app was closed",
                     when=datetime.utcnow() - timedelta(hours=5))
    messages.message("t4", DIRECT, is_from_me=1, associated_message_type=2003, associated_message_guid="p:0/m1")
    await conn.sync_once()

    texts = [m.text for m in await _msgs(DIRECT)]
    assert texts == ["hello", "hi there 👋", "are you there?", "sent while the app was closed"]
    assert [c.args[1].external_id for c in events.received.await_args_list] == ["n1"]  # old one stays quiet
    [edit] = events.edited.await_args_list
    assert edit.args[1].external_id == "m1" and edit.args[1].my_reaction == "😂"
    assert (await _state(conn.account_id))["last_rowid"] == 15


@pytest.mark.asyncio
async def test_runtime_loop_imports_then_polls(db, messages, photo, events, monkeypatch):
    _seed(messages, photo)
    monkeypatch.setattr(SmsConnector, "poll_interval", 0.05)
    conn = await _connected(db, messages)
    assert await conn.start() is True
    try:
        async def until(cond):
            for _ in range(200):
                if await cond():
                    return
                await asyncio.sleep(0.02)
            raise AssertionError("timed out")

        async def has(conv, ext):
            c = await crud.get_conversation_by_external(db, conn.account_id, conv)
            return c is not None and (await crud.get_message_by_external(db, c.id, ext)) is not None

        async def ready():
            return conn.status().state == "ready"

        await until(ready)
        assert await has(GROUP, "e2")
        messages.message("live", SMS_CHAT, sender=CAROL, text="new code 9999")
        await conn.sync_now()
        await until(lambda: has(SMS_CHAT, "live"))
    finally:
        await conn.stop()
    assert conn.status().state == "stopped"


@pytest.mark.asyncio
async def test_runtime_reports_lost_access(db, messages, events, monkeypatch):
    conn = await _connected(db, messages)
    monkeypatch.setattr(SmsConnector, "poll_interval", 0.05)
    os.chmod(messages.path, 0)
    try:
        await conn.start()
        for _ in range(100):
            if conn.status().state == "unauthorized":
                break
            await asyncio.sleep(0.02)
    finally:
        await conn.stop()
        os.chmod(messages.path, 0o600)
    # (stop() resets the state; the broadcast shows what the UI saw)
    states = [c.args[0] for c in events.broadcast.await_args_list if c.args[0].get("type") == "connector_status"]
    assert any(s["state"] == "unauthorized" and "Full Disk Access" in s["error"] for s in states)


def test_locked_database_is_retried(messages, monkeypatch):
    monkeypatch.setattr(chatdb, "BUSY_TIMEOUT", 0.01)
    messages.message("m1", DIRECT, sender=ANN, text="hello")
    writer = sqlite3.connect(str(messages.path), isolation_level=None, check_same_thread=False)
    writer.execute("BEGIN EXCLUSIVE")
    threading.Timer(0.3, lambda: writer.execute("COMMIT")).start()
    started = time.monotonic()
    batch = chatdb.read_after(messages.path, 0, 10)
    assert [m.guid for m in batch.messages] == ["m1"] and time.monotonic() - started >= 0.2
    writer.close()


def test_database_that_stays_locked_is_busy(messages, monkeypatch):
    monkeypatch.setattr(chatdb, "BUSY_TIMEOUT", 0.01)
    monkeypatch.setattr(chatdb, "BUSY_RETRIES", 1)
    writer = sqlite3.connect(str(messages.path), isolation_level=None)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(ChatDbUnavailable) as exc:
            chatdb.read_after(messages.path, 0, 10)
        assert exc.value.reason == "busy"
    finally:
        writer.execute("COMMIT")
        writer.close()


def test_older_schema_without_newer_columns(tmp_path):
    path = tmp_path / "chat.db"
    with sqlite3.connect(str(path)) as c:
        c.executescript("""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, guid TEXT, chat_identifier TEXT, display_name TEXT,
                style INTEGER, service_name TEXT);
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, handle_id INTEGER,
                date INTEGER, is_from_me INTEGER);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
            CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, filename TEXT);
            CREATE TABLE message_attachment_join (message_id INTEGER, attachment_id INTEGER);
            INSERT INTO handle VALUES (1, '+15551234567');
            INSERT INTO chat VALUES (1, 'SMS;-;+15551234567', '+15551234567', NULL, 45, 'SMS');
            INSERT INTO chat_handle_join VALUES (1, 1);
            INSERT INTO message VALUES (1, 'g1', 'old school', 1, 400000000, 0);
            INSERT INTO chat_message_join VALUES (1, 1);
        """)
    [m] = chatdb.read_after(path, 0, 10).messages
    assert (m.text, m.handle, m.associated_type, m.thread_originator_guid) == ("old school", "+15551234567", 0, None)
    assert m.date == datetime(2013, 9, 4, 15, 6, 40)  # seconds since 2001
    assert chatdb.probe(path) is None  # no last_addressed_handle column


# ── attachments ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_attachment_copies_into_media(db, messages, photo, events):
    from backend import config
    _seed(messages, photo)
    conn = await _connected(db, messages)
    await conn.sync_once()
    m4 = (await _msgs(GROUP))[0]
    att = (await db.execute(select(MessageAttachment).where(MessageAttachment.message_id == m4.id))).scalar_one()
    conv = await _conv(GROUP)
    rel = await conn.fetch_attachment(att, m4, conv)
    assert rel == f"media/{conv.id}/{att.id}.jpeg"
    assert (Path(config.DATA_DIR) / rel).read_bytes() == photo.read_bytes()
    await db.refresh(att)
    assert att.local_path == rel


@pytest.mark.asyncio
async def test_fetch_attachment_rejects_paths_outside_messages(db, messages, home, tmp_path):
    conn = _connector(messages)
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    link = home / "Library" / "Messages" / "Attachments" / "evil.txt"
    link.symlink_to(secret)
    conv, msg = MagicMock(id=1), MagicMock()
    for ref in (str(secret), "~/Library/Messages/Attachments/../../../../secret.txt",
                "~/Library/Messages/chat.db", str(link), "relative/file.jpg", None, ""):
        att = MagicMock(id=5, external_ref=ref, filename="x.txt", mime_type="text/plain")
        with pytest.raises(MediaNotFound):
            await conn.fetch_attachment(att, msg, conv)
    gone = MagicMock(id=6, external_ref="~/Library/Messages/Attachments/zz/gone.jpg", filename=None, mime_type=None)
    with pytest.raises(MediaNotFound, match="no longer"):
        await conn.fetch_attachment(gone, msg, conv)


# ── cross-channel linking ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_phone_handle_links_to_the_telegram_person(db, messages, photo, events):
    tg = await f.identity(db, "5", display_name="Ann", phone="15551234567")  # Telegram: no '+'
    _seed(messages, photo)
    conn = await _connected(db, messages)
    await conn.sync_once()
    sms = (await db.execute(
        select(Identity).where(Identity.channel == "sms", Identity.external_id == ANN)
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert sms.person_id == tg.person_id
    carol = await crud.get_identity_by_external(db, "sms", CAROL)
    assert carol.person_id != tg.person_id


@pytest.mark.asyncio
async def test_email_handle_links_to_the_gmail_person(db, messages, photo, events):
    mail = await person_identities.upsert_identity(db, channel="gmail", external_id="bob@example.com", display_name="Bob")
    _seed(messages, photo)
    conn = await _connected(db, messages)
    await conn.sync_once()
    sms = (await db.execute(
        select(Identity).where(Identity.channel == "sms", Identity.external_id == "bob@example.com")
        .execution_options(populate_existing=True)
    )).scalar_one()
    assert sms.person_id == mail.person_id


def test_handle_normalization(monkeypatch):
    monkeypatch.setenv("LANG", "zh_TW.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    n = mod.normalize_handle
    assert n("+1 (555) 123-4567") == ("+15551234567", "+15551234567")
    assert n("Ann@Example.COM") == ("ann@example.com", None)
    assert n("0912 345 678") == ("+886912345678", "+886912345678")  # national format, locale region
    assert n("72975") == ("72975", None)  # short code
    monkeypatch.setenv("LANG", "C")
    assert n("0912 345 678") == ("0912 345 678", None)  # no region: not guessed


def test_reaction_parsing():
    raw = chatdb.RawMessage(rowid=1, guid="r", chat_id=1, text=None, handle=ANN, date=None, is_from_me=False,
                            service="iMessage", associated_type=2006, associated_guid="bp:target",
                            associated_emoji="🔥")
    r = mod.reaction_of(raw)
    assert (r.target_guid, r.reactor, r.emoji, r.remove) == ("target", ANN, "🔥", False)
    raw.associated_type, raw.associated_guid = 3004, "p:1/target"
    r = mod.reaction_of(raw)
    assert (r.target_guid, r.emoji, r.remove) == ("target", "‼️", True)
    raw.associated_type = 1000  # sticker placed on a message: a normal row
    assert mod.reaction_of(raw) is None


FAKE_SIPS = r'''
import os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_SIPS_LOG"], "a") as fh:
    fh.write(" ".join(args) + "\n")
if os.environ.get("FAKE_SIPS_FAIL"):
    print("Error: unsupported image", file=sys.stderr)
    sys.exit(13)
assert args[:3] == ["-s", "format", "jpeg"] and args[4] == "--out"
src, out = args[3], args[5]
with open(out, "wb") as fh:
    fh.write(b"\xff\xd8\xff converted from " + open(src, "rb").read())
'''


@pytest.fixture
def heic(home) -> Path:
    p = home / "Library" / "Messages" / "Attachments" / "cd" / "02" / "AT-2" / "IMG_0002.HEIC"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"ftypheic fake")
    return p


@pytest.fixture
def sips_dir(tmp_path, monkeypatch) -> Path:
    """A PATH holding only a fake ``sips`` (the real one is never run)."""
    import stat
    import sys
    d = tmp_path / "sipsbin"
    d.mkdir()
    exe = d / "sips"
    exe.write_text(f"#!{sys.executable}\n{FAKE_SIPS}")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.setenv("FAKE_SIPS_LOG", str(tmp_path / "sips.log"))
    return d


async def _heic_attachment(db, messages, heic):
    rel = "~/" + str(heic.relative_to(Path(os.environ["HOME"])))
    messages.message("h1", DIRECT, sender=ANN, attachments=[(rel, "image/heic", "IMG_0002.HEIC", 13)])
    conn = await _connected(db, messages)
    await conn.sync_once()
    [msg] = await _msgs(DIRECT)
    att = (await db.execute(select(MessageAttachment).where(MessageAttachment.message_id == msg.id))).scalar_one()
    assert (att.kind, att.mime_type) == ("photo", "image/heic")
    return conn, att, msg, await _conv(DIRECT)


@pytest.mark.asyncio
async def test_heic_is_converted_to_jpeg(db, messages, heic, sips_dir, events, tmp_path):
    from backend import config
    conn, att, msg, conv = await _heic_attachment(db, messages, heic)
    rel = await conn.fetch_attachment(att, msg, conv)
    assert rel == f"media/{conv.id}/{att.id}.jpg"
    out = Path(config.DATA_DIR) / rel
    assert out.read_bytes() == b"\xff\xd8\xff converted from ftypheic fake"
    assert [p.name for p in out.parent.iterdir()] == [out.name]  # no temp file left
    assert str(heic) in (tmp_path / "sips.log").read_text()
    await db.refresh(att)
    assert (att.local_path, att.mime_type) == (rel, "image/jpeg")


@pytest.mark.asyncio
async def test_heic_falls_back_to_the_original_when_sips_fails(db, messages, heic, sips_dir, events, monkeypatch):
    from backend import config
    monkeypatch.setenv("FAKE_SIPS_FAIL", "1")
    conn, att, msg, conv = await _heic_attachment(db, messages, heic)
    rel = await conn.fetch_attachment(att, msg, conv)
    assert rel == f"media/{conv.id}/{att.id}.heic"
    out = Path(config.DATA_DIR) / rel
    assert out.read_bytes() == heic.read_bytes()
    assert [p.name for p in out.parent.iterdir()] == [out.name]
    await db.refresh(att)
    assert (att.local_path, att.mime_type) == (rel, "image/heic")


@pytest.mark.asyncio
async def test_heic_falls_back_to_the_original_without_sips(db, messages, heic, events, tmp_path, monkeypatch):
    from backend import config
    empty = tmp_path / "nobin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    conn, att, msg, conv = await _heic_attachment(db, messages, heic)
    rel = await conn.fetch_attachment(att, msg, conv)
    assert rel.endswith(".heic") and (Path(config.DATA_DIR) / rel).read_bytes() == heic.read_bytes()
    await db.refresh(att)
    assert att.mime_type == "image/heic"


@pytest.mark.asyncio
async def test_concurrent_starts_run_one_poll_loop_that_stop_ends(db, messages, events, monkeypatch):
    from backend.tasks import _background_tasks

    def live():
        return [t for t in _background_tasks if not t.done() and t.get_name() == "sms_sync"]

    conn = await _connected(db, messages)

    async def yielding(data):  # a real WebSocket send yields to the loop
        await asyncio.sleep(0)

    monkeypatch.setattr("backend.ws.manager.ws_manager.broadcast", yielding)
    assert await asyncio.gather(conn.start(), conn.start()) == [True, True]
    assert len(live()) == 1
    await conn.stop()
    await asyncio.sleep(0)
    assert live() == [] and conn.status().state == "stopped"


@pytest.mark.asyncio
async def test_conversations_offer_no_history_backfill(db, messages, events):
    from backend.connectors.base import Capability

    conn = await _connected(db, messages)
    conv = await f.conversation(db, "chat-1", title="Ann", channel="sms")
    assert Capability.HISTORY_BACKFILL in conn.capabilities  # the account imports chat.db history
    assert Capability.HISTORY_BACKFILL not in conn.conversation_capabilities(conv)
