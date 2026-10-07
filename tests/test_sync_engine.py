"""Sync engine: message conversion, upsert semantics, keyset windows,
coordination, runtime ownership and the auth/compose/media entry points
(reviews/APP_REVIEW_2026-10-06.md §2-A, §2-A2, §2-C, §2-E, §3)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from telethon.errors import FloodWaitError
from telethon.tl import types

from backend.db import crud
from backend.db.models import Chat, Message
from backend.telegram.convert import message_to_row

UTC = timezone.utc


def use_client(client):
    """Make runtime.client() return ``client`` (None = Telegram unavailable)."""
    from backend.telegram.runtime import runtime
    return patch.object(runtime, "client", AsyncMock(return_value=client))


def tg_msg(mid, text="hi", peer=77, date=None, entities=None, out=False, **kw):
    m = types.Message(
        id=mid,
        peer_id=types.PeerUser(peer) if peer > 0 else types.PeerChannel(-peer),
        date=date or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        message=text,
        entities=entities,
        out=out,
        **kw,
    )
    m.get_sender = AsyncMock(return_value=None)
    return m


async def seed_chat(db, telegram_id=77, **kw):
    kw.setdefault("name", "Alice")
    kw.setdefault("type", "private")
    chat = Chat(telegram_id=telegram_id, **kw)
    db.add(chat)
    await db.commit()
    return chat


# ── message_to_row ───────────────────────────────────────────────────────────

def test_formatted_text_uses_raw_text_and_entities():
    row = message_to_row(tg_msg(1, "hello world", entities=[types.MessageEntityItalic(6, 5)]))
    assert row["text"] == "hello world"
    assert row["text_html"] == "hello <em>world</em>"
    row = message_to_row(tg_msg(2, "use x now", entities=[types.MessageEntityCode(4, 1)]))
    assert row["text_html"] == "use <code>x</code> now"
    assert row["delivery_state"] == "sent"


def test_outgoing_from_other_device_is_sent_not_sending():
    assert message_to_row(tg_msg(3, "x", out=True))["delivery_state"] == "sent"


def test_forum_topic_mapping():
    # plain message inside topic 10: topic membership, not a reply
    m = tg_msg(20, peer=-100, reply_to=types.MessageReplyHeader(reply_to_msg_id=10, forum_topic=True))
    row = message_to_row(m, -100, forum=True)
    assert (row["topic_id"], row["reply_to_tg_message_id"]) == (10, None)
    # real reply inside topic 10
    m = tg_msg(21, peer=-100, reply_to=types.MessageReplyHeader(reply_to_msg_id=15, reply_to_top_id=10, forum_topic=True))
    row = message_to_row(m, -100, forum=True)
    assert (row["topic_id"], row["reply_to_tg_message_id"]) == (10, 15)
    # no header in a forum → General
    row = message_to_row(tg_msg(22, peer=-100), -100, forum=True)
    assert row["topic_id"] == 1
    assert message_to_row(tg_msg(23, peer=-100), -100, forum=False)["topic_id"] is None


# ── upsert semantics ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_updates_telegram_columns_and_keeps_local_ones(db):
    await seed_chat(db)
    first = message_to_row(tg_msg(1, "meet at 5", entities=[types.MessageEntityBold(0, 4)]))
    first["reactions_json"] = '[{"emoji": "👍", "count": 1}]'
    first["is_pinned"] = True
    res = await crud.save_messages(db, [first])
    assert res == {"created": [1], "updated": []}

    row = (await db.execute(select(Message).where(Message.tg_message_id == 1))).scalar_one()
    row.scam_score = 0.9
    row.my_reaction = "👍"
    row.media_path = "media/77/1.jpg"
    row.delivery_state = "read"
    await db.commit()

    edited = message_to_row(tg_msg(1, "meet at 6", edit_date=datetime(2026, 1, 1, 13, tzinfo=UTC)))
    edited["media_path"] = "media/77/other.jpg"
    res = await crud.save_messages(db, [edited])
    assert res == {"created": [], "updated": [1]}

    row = (await db.execute(select(Message).where(Message.tg_message_id == 1).execution_options(populate_existing=True))).scalar_one()
    assert row.text == "meet at 6"
    assert row.text_html is None          # plain-text edit clears old HTML
    assert row.reactions_json is None     # reaction removal propagates
    assert row.is_pinned is False         # unpin propagates
    assert row.is_edited is True
    assert row.scam_score == 0.9 and row.my_reaction == "👍"
    assert row.media_path == "media/77/1.jpg"
    assert row.delivery_state == "read"   # monotonic

    # identical content → no change reported
    assert await crud.save_messages(db, [edited]) == {"created": [], "updated": []}


@pytest.mark.asyncio
async def test_save_message_backfills_topic_and_updates_preview(db):
    await seed_chat(db, -100, type="group", is_forum=True)
    row = message_to_row(tg_msg(5, "x", peer=-100), -100, forum=False)
    saved = await crud.save_message(db, **row)
    assert saved._change == "created" and saved.topic_id is None
    saved = await crud.save_message(db, **message_to_row(tg_msg(5, "x", peer=-100), -100, forum=True))
    assert saved._change == "updated" and saved.topic_id == 1
    chat = await crud.get_chat_by_telegram_id(db, -100)
    await db.refresh(chat)
    assert chat.last_message_text == "x"


@pytest.mark.asyncio
async def test_concurrent_upsert_chat_never_conflicts(db_engine):
    from backend.db.database import AsyncSessionLocal

    async def one(i):
        async with AsyncSessionLocal() as s:
            return await crud.upsert_chat(s, 555, f"n{i}", "private", unread_count=i)

    chats = await asyncio.gather(*[one(i) for i in range(5)])
    assert {c.telegram_id for c in chats} == {555}
    async with AsyncSessionLocal() as s:
        assert len((await s.execute(select(Chat).where(Chat.telegram_id == 555))).all()) == 1
        # optional fields keep old values when not provided
        c = await crud.upsert_chat(s, 555, "renamed", "private")
        assert c.name == "renamed" and c.unread_count is not None


# ── keyset windows ───────────────────────────────────────────────────────────

async def seed_history(db, n=10, chat_id=77):
    await seed_chat(db, chat_id)
    base = datetime(2026, 1, 1, 12, 0)
    rows = []
    for i in range(1, n + 1):
        # pairs share a timestamp (albums) → tg_message_id breaks the tie
        rows.append(message_to_row(tg_msg(i, f"m{i}", peer=chat_id, date=(base + timedelta(minutes=(i + 1) // 2)).replace(tzinfo=UTC)), chat_id))
    await crud.save_messages(db, rows)


def ids(window):
    return [r[0].tg_message_id for r in window["rows"]]


@pytest.mark.asyncio
async def test_keyset_window_pages_without_gaps_or_duplicates(db):
    await seed_history(db)
    w = await crud.get_message_window(db, 77, limit=4)
    assert ids(w) == [7, 8, 9, 10] and w["has_older"] and not w["has_newer"]
    seen = ids(w)
    while w["has_older"]:
        oldest = w["rows"][0][0]
        w = await crud.get_message_window(db, 77, limit=4, before=(oldest.date, oldest.tg_message_id))
        seen = ids(w) + seen
    assert seen == list(range(1, 11))
    newest = w["rows"][-1][0]
    w2 = await crud.get_message_window(db, 77, limit=3, after=(newest.date, newest.tg_message_id))
    assert ids(w2) == [3, 4, 5] and w2["has_older"] and w2["has_newer"]


@pytest.mark.asyncio
async def test_anchor_and_date_windows_report_both_flags(db):
    await seed_history(db)
    w = await crud.get_message_window(db, 77, limit=4, anchor_tg_id=5)
    assert ids(w) == [3, 4, 5, 6] and w["has_older"] and w["has_newer"] and w["anchor_found"]
    w = await crud.get_message_window(db, 77, limit=2, around_date=datetime(2026, 1, 1, 12, 3))
    assert ids(w) == [4, 5]
    w = await crud.get_message_window(db, 77, limit=2, anchor_tg_id=999)
    assert not w["anchor_found"]


@pytest.mark.asyncio
async def test_general_topic_includes_untagged_rows(db):
    await seed_chat(db, -100, type="group", is_forum=True)
    await crud.save_messages(db, [
        {**message_to_row(tg_msg(1, "old", peer=-100), -100), "topic_id": None},
        message_to_row(tg_msg(2, "general", peer=-100), -100, forum=True),
        message_to_row(tg_msg(3, "t10", peer=-100, reply_to=types.MessageReplyHeader(reply_to_msg_id=10, forum_topic=True)), -100, forum=True),
    ])
    assert ids(await crud.get_message_window(db, -100, topic_id=1)) == [1, 2]
    assert ids(await crud.get_message_window(db, -100, topic_id=10)) == [3]


@pytest.mark.asyncio
async def test_window_endpoint_cursors(api, db):
    await seed_history(db)
    chat = await crud.get_chat_by_telegram_id(db, 77)
    chat.last_synced_at = datetime.utcnow()  # fresh: no auto-sync
    await db.commit()
    r = await api.get("/api/chats/77/messages/window", params={"limit": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["tg_message_id"] for m in body["messages"]] == [7, 8, 9, 10]
    assert body["has_older"] and not body["has_newer"]
    r = await api.get("/api/chats/77/messages/window", params={"limit": 4, "before": body["older_cursor"]})
    assert [m["tg_message_id"] for m in r.json()["messages"]] == [3, 4, 5, 6]
    assert (await api.get("/api/chats/77/messages/window", params={"before": "garbage"})).status_code == 400
    # offset endpoints are gone
    assert (await api.get("/api/chats/77/messages", params={"limit": 3})).status_code in (404, 405)
    assert (await api.get("/api/chats/77/messages/date-offset", params={"date": "2026-01-01"})).status_code in (404, 405)
    assert (await api.get("/api/chats/77/topics/1/messages")).status_code in (404, 405)


# ── coordinator ──────────────────────────────────────────────────────────────

class FakeHistoryClient:
    def __init__(self, msgs, delay=0.05, flood=None):
        self.msgs = msgs
        self.calls = []
        self.delay = delay
        self.flood = flood

    def is_connected(self):
        return True

    async def iter_messages(self, chat_id, limit=50, offset_id=0, **kw):
        self.calls.append((chat_id, limit, offset_id))
        if self.flood:
            raise FloodWaitError(request=None, capture=self.flood)
        await asyncio.sleep(self.delay)
        for m in sorted(self.msgs, key=lambda m: -m.id):
            if offset_id and m.id >= offset_id:
                continue
            yield m

    async def get_entity(self, chat_id):
        raise ValueError("no entity")  # pinned probe: ignored


@pytest.fixture
def coord():
    from backend.telegram.coordinator import SyncCoordinator
    return SyncCoordinator()


@pytest.mark.asyncio
async def test_concurrent_syncs_share_one_fetch(db, coord):
    await seed_chat(db)
    client = FakeHistoryClient([tg_msg(i, f"m{i}") for i in range(1, 4)])
    with use_client(client):
        r1, r2 = await asyncio.gather(coord.sync_chat(77, 50), coord.sync_chat(77, 50))
    assert len(client.calls) == 1
    assert r1 is r2 and r1.status == "changed" and r1.added == 3 and r1.exhausted
    with use_client(client):
        r3 = await coord.sync_chat(77, 50)
    assert r3.status == "unchanged"


@pytest.mark.asyncio
async def test_sync_removes_messages_deleted_on_telegram(db, coord):
    await seed_chat(db)
    msgs = [tg_msg(i, f"m{i}", date=datetime(2026, 1, 1, 12, i, tzinfo=UTC)) for i in range(1, 5)]
    client = FakeHistoryClient(msgs, delay=0)
    with use_client(client):
        await coord.sync_chat(77, 50)
        client.msgs = [m for m in msgs if m.id != 2]
        res = await coord.sync_chat(77, 50)
    assert res.deleted == 1 and res.status == "changed"


@pytest.mark.asyncio
async def test_flood_wait_is_typed_and_gates_other_syncs(db, coord):
    await seed_chat(db)
    client = FakeHistoryClient([], flood=30)
    with use_client(client):
        res = await coord.sync_chat(77, 50)
        assert res.status == "error" and res.flood_wait == 30 and not res.completed
        skipped = await coord.sync_chat(78, 50, max_wait=1)
    assert skipped.status == "skipped" and skipped.flood_wait > 0


@pytest.mark.asyncio
async def test_no_client_is_unavailable_not_empty(coord):
    with use_client(None):
        res = await coord.sync_chat(77, 50)
    assert res.status == "unavailable" and not res.completed


@pytest.mark.asyncio
async def test_backfill_uses_oldest_local_id(api, db):
    await seed_history(db, n=3)
    from backend.telegram.coordinator import coordinator, SyncResult
    with patch.object(coordinator, "sync_chat", AsyncMock(return_value=SyncResult("changed", 77, fetched=5, added=5, exhausted=True))) as m:
        r = await api.post("/api/chats/77/backfill", params={"limit": 50})
    assert r.status_code == 200 and r.json() == {"added": 5, "exhausted": True, "status": "changed"}
    assert m.call_args.kwargs["offset_id"] == 1


# ── runtime ──────────────────────────────────────────────────────────────────

class FakeRuntimeClient:
    def __init__(self):
        self.handlers = []
        self.connected = True
        self._disc = asyncio.get_event_loop().create_future()

    def is_connected(self):
        return self.connected

    async def is_user_authorized(self):
        return True

    def add_event_handler(self, cb, ev):
        self.handlers.append(cb)

    def remove_event_handler(self, cb, ev=None):
        self.handlers.remove(cb)

    async def catch_up(self):
        pass

    @property
    def disconnected(self):
        return asyncio.shield(self._disc)


@pytest.mark.asyncio
async def test_runtime_registers_handlers_once_and_stops_cleanly(db_engine):
    from backend.telegram import runtime as rt_mod
    rt = rt_mod.TelegramRuntime()
    client = FakeRuntimeClient()
    noop = AsyncMock(return_value=None)
    with patch.object(rt_mod.tg_client, "get_client", return_value=client), \
         patch.object(rt_mod.tg_client, "disconnect", AsyncMock()), \
         patch.object(rt, "_startup_sequence", noop), \
         patch.object(rt, "_periodic_dialog_sync", noop):
        results = await asyncio.gather(rt.ensure_started(), rt.ensure_started(), rt.ensure_started())
        assert results == [True, True, True]
        assert len(client.handlers) == 5  # one set, not three
        assert await rt.ensure_started() is True
        assert len(client.handlers) == 5
        await rt.stop()
    assert client.handlers == [] and rt.state == "stopped" and not rt.running


# ── live handlers ────────────────────────────────────────────────────────────

class FakeEvent:
    def __init__(self, message, chat_id, entity):
        self.message = message
        self.chat_id = chat_id
        self._entity = entity

    async def get_chat(self):
        return self._entity


@pytest.mark.asyncio
async def test_new_message_for_unknown_chat_is_saved_and_edits_propagate(db_engine):
    from backend.telegram import sync
    client = FakeRuntimeClient()
    handlers = {cb.__name__: cb for cb, _ in sync.register_event_handlers(client)}
    sent = []

    async def capture(data):
        sent.append(data)

    user = types.User(id=77, first_name="Ann", last_name="Lee")
    msg = tg_msg(1, "meet at 5")
    msg.get_sender = AsyncMock(return_value=user)
    with patch.object(sync.ws_manager, "broadcast", capture), \
         patch.object(sync, "_enqueue_scam", AsyncMock()) as scam, \
         patch("backend.api.settings.get_notif_pref", return_value="none"):
        await handlers["on_new_message"](FakeEvent(msg, 77, user))
        edited = tg_msg(1, "meet at 6", edit_date=datetime(2026, 1, 1, 13, tzinfo=UTC))
        edited.get_sender = AsyncMock(return_value=user)
        await handlers["on_message_edited"](FakeEvent(edited, 77, user))

    from backend.db.database import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        chat = await crud.get_chat_by_telegram_id(s, 77)
        row = (await s.execute(select(Message).where(Message.chat_id == 77))).scalar_one()
    assert chat.name == "Ann Lee" and chat.type == "private"
    assert row.text == "meet at 6" and row.sender_name == "Ann Lee"
    types_sent = [d["type"] for d in sent]
    assert types_sent == ["new_message", "message_edited"]
    assert sent[1]["message"]["text"] == "meet at 6"
    assert [c.args[2] for c in scam.call_args_list] == ["realtime", "edit"]


# ── auth / compose / media ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_mutators_require_token_and_reset_requires_confirmation(db_engine):
    from backend.api.router import create_app
    from backend.api import auth
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        for path in ("/api/auth/logout", "/api/auth/setup", "/api/auth/send-code"):
            assert (await c.post(path, json={})).status_code == 401, path
        assert (await c.get("/api/auth/status")).status_code == 401
        assert (await c.get("/api/auth/vault-status")).status_code == 200
        assert (await c.post("/api/auth/vault/reset", json={})).status_code == 400
        with patch.object(auth, "reset_vault") as reset, \
             patch.object(auth.runtime, "stop", AsyncMock()) as stop:
            r = await c.post("/api/auth/vault/reset", json={"confirm": "RESET"})
        assert r.status_code == 200 and reset.called and stop.called


@pytest.mark.asyncio
async def test_send_with_same_nonce_sends_once_and_persists_topic(api, db):
    await seed_chat(db, -100, type="group", is_forum=True)

    class FakeSendClient:
        def __init__(self):
            self.calls = []

        async def is_user_authorized(self):
            return True

        async def send_message(self, chat_id, text, reply_to=None):
            self.calls.append(reply_to)
            await asyncio.sleep(0.02)
            return tg_msg(500 + len(self.calls), text, peer=-100, out=True)

    client = FakeSendClient()
    body = {"chat_id": -100, "text": "hello", "topic_id": 10, "client_nonce": "n-1"}
    with use_client(client):
        r1, r2 = await asyncio.gather(api.post("/api/compose/send", json=body), api.post("/api/compose/send", json=body))
        r3 = await api.post("/api/compose/send", json=body)
    assert client.calls == [10]  # topic-aware: reply_to=top_msg_id, sent once
    assert r1.json() == r2.json() == r3.json()
    assert r1.json()["message"]["topic_id"] == 10


@pytest.mark.asyncio
async def test_media_download_is_atomic_and_single_flight(db, tmp_path):
    await seed_chat(db)
    await crud.save_messages(db, [{**message_to_row(tg_msg(9, "doc"), 77), "media_type": "document", "media_filename": "a.pdf"}])
    from backend.telegram import media

    class FakeMediaClient:
        def __init__(self, fail=False):
            self.fail = fail
            self.downloads = 0

        async def get_messages(self, chat_id, ids):
            m = tg_msg(ids, "doc")
            m.media = types.MessageMediaDocument(document=types.Document(
                id=1, access_hash=1, file_reference=b"", date=None, mime_type="application/pdf",
                size=3, dc_id=1, attributes=[types.DocumentAttributeFilename("a.pdf")]))
            return m

        async def download_media(self, msg, file, thumb=None):
            self.downloads += 1
            with open(file, "wb") as f:
                f.write(b"par")
            await asyncio.sleep(0.02)
            if self.fail:
                raise ConnectionError("dropped")
            return file

    with patch.object(media, "MEDIA_DIR", tmp_path / "media"), patch.object(media, "DATA_DIR", tmp_path):
        failing = FakeMediaClient(fail=True)
        with use_client(failing):
            with pytest.raises(ConnectionError):
                await media.download_message_media(77, 9, media_type="document", media_filename="a.pdf")
        assert not (tmp_path / "media" / "77" / "9.pdf").exists()
        assert list((tmp_path / "media" / "77").iterdir()) == []  # no partial file left

        ok = FakeMediaClient()
        with use_client(ok):
            a, b = await asyncio.gather(
                media.download_message_media(77, 9, media_type="document", media_filename="a.pdf"),
                media.download_message_media(77, 9, media_type="document", media_filename="a.pdf"),
            )
        assert a == b == "media/77/9.pdf" and ok.downloads == 1
        assert (tmp_path / "media" / "77" / "9.pdf").read_bytes() == b"par"
    row = (await db.execute(select(Message).where(Message.tg_message_id == 9).execution_options(populate_existing=True))).scalar_one()
    assert row.media_path == "media/77/9.pdf"


# ── message dict shape ───────────────────────────────────────────────────────

SCAM_KEYS = {"scam_score", "scam_reason", "scam_category", "scam_manual_verdict",
             "scam_status", "scam_error", "effective_is_scam"}


@pytest.mark.asyncio
async def test_every_message_serializer_carries_scam_fields(api, db):
    """Window lists, compose responses and WS payloads all come from
    chats._serialize_msg, so they share one shape including the scam state."""
    await seed_history(db, n=2)
    row = (await db.execute(select(Message).where(Message.tg_message_id == 2))).scalar_one()
    row.scam_manual_verdict = "scam"
    await db.commit()
    chat = await crud.get_chat_by_telegram_id(db, 77)
    chat.last_synced_at = datetime.utcnow()
    await db.commit()

    window = (await api.get("/api/chats/77/messages/window")).json()["messages"]
    pinned = (await api.get("/api/chats/77/pinned")).json()
    for m in window + pinned:
        assert SCAM_KEYS <= m.keys()
    assert window[-1]["effective_is_scam"] is True and window[0]["effective_is_scam"] is False


    class FakeSendClient:
        async def is_user_authorized(self):
            return True

        async def send_message(self, chat_id, text, reply_to=None):
            return tg_msg(900, text, out=True)

    with use_client(FakeSendClient()):
        sent = (await api.post("/api/compose/send", json={"chat_id": 77, "text": "x"})).json()["message"]
    assert SCAM_KEYS <= sent.keys()
