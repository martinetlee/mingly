"""Sync engine, audit round 2: one client accessor, remote jump targets,
first-sync emptiness, coordinator keys/charging, text+preview repair, vault
lock, packaged shutdown, partial multi-file sends, truncated media."""

import asyncio
import socket
import threading
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from telethon.tl import types

from backend.db import crud
from backend.db.models import Chat, Message
from backend.telegram.convert import message_to_row
from tests.test_sync_engine import FakeHistoryClient, seed_chat, seed_history, tg_msg, use_client

UTC = timezone.utc


# ── 1. runtime-owned client access ───────────────────────────────────────────

class ConnClient:
    def __init__(self, connected=True, fail=False):
        self.connected = connected
        self.fail = fail
        self.connects = 0

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connects += 1
        await asyncio.sleep(0.01)
        if self.fail:
            raise ConnectionError("offline")
        self.connected = True


@pytest.mark.asyncio
async def test_runtime_client_requires_or_returns_none():
    from backend.telegram import runtime as rt_mod
    from backend.telegram.client import TelegramUnavailable
    rt = rt_mod.TelegramRuntime()
    with patch.object(rt_mod.tg_client, "get_client", return_value=None):
        with pytest.raises(TelegramUnavailable) as exc:
            await rt.client()
        assert exc.value.status_code == 503
        assert await rt.client(require=False) is None

    dropped = ConnClient(connected=False)
    with patch.object(rt_mod.tg_client, "get_client", return_value=dropped):
        got = await asyncio.gather(rt.client(), rt.client(), rt.client())
    assert all(g is dropped for g in got) and dropped.connects == 1  # one reconnect, shared

    offline = ConnClient(connected=False, fail=True)
    with patch.object(rt_mod.tg_client, "get_client", return_value=offline):
        assert await rt.client(require=False) is None
        with pytest.raises(TelegramUnavailable):
            await rt.client()


@pytest.mark.asyncio
async def test_login_client_creates_exactly_one_client_under_concurrency():
    from backend.telegram import runtime as rt_mod
    rt = rt_mod.TelegramRuntime()
    state = {"client": None, "created": 0}

    async def create():
        state["created"] += 1
        await asyncio.sleep(0.02)
        state["client"] = ConnClient()
        return False

    with patch.object(rt_mod.tg_client, "get_client", side_effect=lambda: state["client"]), \
         patch.object(rt_mod.tg_client, "has_credentials", return_value=True), \
         patch.object(rt_mod.tg_client, "_create_client", create):
        clients = await asyncio.gather(*[rt.login_client() for _ in range(4)])
    assert state["created"] == 1 and len({id(c) for c in clients}) == 1


def test_only_the_runtime_creates_clients():
    import backend.telegram.client as client_mod
    assert not hasattr(client_mod, "init_client")
    import inspect
    from backend.api import auth, chats, compose
    for mod in (auth, chats, compose):
        assert "_create_client" not in inspect.getsource(mod)


# ── 2. jump to a message that isn't stored locally ───────────────────────────

@pytest.mark.asyncio
async def test_anchor_not_local_is_fetched_from_telegram(api, db):
    await seed_history(db, n=3)
    chat = await crud.get_chat_by_telegram_id(db, 77)
    chat.last_synced_at = datetime.utcnow()
    await db.commit()
    remote = [tg_msg(100 + i, f"old{i}", date=datetime(2025, 6, 1, 0, i, tzinfo=UTC)) for i in range(5)]

    class AroundClient(FakeHistoryClient):
        async def get_messages(self, chat_id, ids):
            return [next((m for m in remote if m.id == i), None) for i in ids]

        async def iter_messages(self, chat_id, limit=50, offset_id=0, min_id=0, reverse=False, **kw):
            pool = sorted(remote, key=lambda m: m.id, reverse=not reverse)
            n = 0
            for m in pool:
                if offset_id and m.id >= offset_id:
                    continue
                if min_id and m.id <= min_id:
                    continue
                if n >= limit:
                    break
                n += 1
                yield m

    with use_client(AroundClient([])):
        r = await api.get("/api/chats/77/messages/window", params={"anchor": 102, "limit": 4})
        body = r.json()
        assert body["anchor_found"] is True and body["anchor_reason"] is None
        assert [m["tg_message_id"] for m in body["messages"]] == [100, 101, 102, 103]
        assert body["has_newer"]  # local recent history is newer than the fetched window

        r = await api.get("/api/chats/77/messages/window", params={"anchor": 999})
        assert r.json()["anchor_found"] is False and r.json()["anchor_reason"] == "not_found"

    with use_client(None):
        r = await api.get("/api/chats/77/messages/window", params={"anchor": 555})
    assert r.json()["anchor_reason"] == "unavailable"


@pytest.mark.asyncio
async def test_anchor_view_starts_first_sync_of_never_synced_chat(api, db):
    await seed_chat(db)  # last_synced_at NULL
    from backend.api import chats as chats_api
    with patch.object(chats_api, "run_single") as start, use_client(None):
        await api.get("/api/chats/77/messages/window", params={"anchor": 5})
    assert start.called and start.call_args.args[0] == "autosync:77"


# ── 3. first-sync emptiness needs a completed deep fetch ─────────────────────

@pytest.mark.asyncio
async def test_first_sync_empty_only_after_deep_fetch_completes(db):
    await seed_chat(db)
    from backend.api import chats as chats_api
    from backend.telegram.coordinator import SyncResult

    def run(results):
        it = iter(results)
        return AsyncMock(side_effect=lambda *a, **k: next(it))

    sent = []
    shallow = SyncResult("unchanged", 77, fetched=100)  # full page, nothing storable
    deep_err = SyncResult("error", 77, error="boom")
    deep_ok = SyncResult("unchanged", 77, fetched=40, exhausted=True)
    with patch.object(chats_api.coordinator, "sync_chat", run([shallow, deep_err, shallow, deep_ok])) as sc, \
         patch.object(chats_api.ws_manager, "broadcast", AsyncMock(side_effect=lambda d: sent.append(d))), \
         patch.object(chats_api.asyncio, "sleep", AsyncMock()):
        await chats_api._auto_sync(77, first_sync=True)
    assert [c.args[1] for c in sc.call_args_list] == [100, 500, 100, 500]
    assert [d["type"] for d in sent] == ["chat_sync_empty"]

    sent.clear()
    with patch.object(chats_api.coordinator, "sync_chat", run([shallow, deep_err] * 4)), \
         patch.object(chats_api.ws_manager, "broadcast", AsyncMock(side_effect=lambda d: sent.append(d))), \
         patch.object(chats_api.asyncio, "sleep", AsyncMock()):
        await chats_api._auto_sync(77, first_sync=True)
    assert [d["type"] for d in sent] == ["chat_sync_failed"]


# ── 4. coordinator key and proportional charging ─────────────────────────────

@pytest.mark.asyncio
async def test_deeper_sync_is_not_answered_by_shallow_inflight(db):
    from backend.telegram.coordinator import SyncCoordinator
    coord = SyncCoordinator()
    await seed_chat(db)
    client = FakeHistoryClient([tg_msg(i, f"m{i}") for i in range(1, 4)])
    with use_client(client):
        await asyncio.gather(coord.sync_chat(77, 50), coord.sync_chat(77, 500), coord.sync_chat(77, 500))
    assert sorted(c[1] for c in client.calls) == [50, 500]


@pytest.mark.asyncio
async def test_history_is_charged_per_page(db):
    from backend.telegram.coordinator import SyncCoordinator
    coord = SyncCoordinator()
    await seed_chat(db)
    client = FakeHistoryClient([tg_msg(i, f"m{i}") for i in range(1, 251)], delay=0)
    charged = []
    real = coord.limiter.acquire

    async def counting(n=1, max_wait=None):
        charged.append(n)
        return await real(n, max_wait=max_wait)

    with use_client(client), patch.object(coord.limiter, "acquire", counting):
        res = await coord.sync_chat(77, 1000, min_date=datetime(2000, 1, 1))
    assert res.fetched == 250 and sum(charged) == 3  # pages 1, 2, 3


# ── 5. text repair also fixes the chat preview ───────────────────────────────

@pytest.mark.asyncio
async def test_repair_rewrites_garbled_rows_and_preview(db):
    from sqlalchemy import text as sa_text
    from backend.telegram import sync
    await seed_chat(db)
    garbled = Message(tg_message_id=1, chat_id=77, text="hello __world__", text_html="hello <em>__wor</em>ld__",
                      date=datetime(2026, 1, 1, 12, 0))
    sent_from_app = Message(tg_message_id=2, chat_id=77, text="**bold** now", text_html=None,
                            date=datetime(2026, 1, 1, 12, 5), is_outgoing=True)
    plain = Message(tg_message_id=3, chat_id=77, text="snake_case is fine", date=datetime(2026, 1, 1, 11, 0))
    db.add_all([garbled, sent_from_app, plain])
    chat = await crud.get_chat_by_telegram_id(db, 77)
    chat.last_message_text = "**bold** now"
    chat.last_message_date = datetime(2026, 1, 1, 12, 5)
    await db.execute(sa_text("INSERT OR IGNORE INTO sync_text_repair (chat_id) VALUES (77)"))
    await db.commit()

    fresh = {
        1: tg_msg(1, "hello world", entities=[types.MessageEntityItalic(6, 5)], date=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
        2: tg_msg(2, "bold now", entities=[types.MessageEntityBold(0, 4)], out=True, date=datetime(2026, 1, 1, 12, 5, tzinfo=UTC)),
    }

    class RepairClient:
        requested = None

        def is_connected(self):
            return True

        async def get_messages(self, chat_id, ids):
            RepairClient.requested = sorted(ids)
            return [fresh.get(i) for i in ids]

    with use_client(RepairClient()):
        assert await sync.repair_garbled_text() == 2
    assert RepairClient.requested == [1, 2]  # "snake_case" isn't a marker hit
    rows = {m.tg_message_id: m for m in (await db.execute(
        select(Message).where(Message.chat_id == 77).execution_options(populate_existing=True))).scalars()}
    assert rows[1].text == "hello world" and rows[1].text_html == "hello <em>world</em>"
    assert rows[2].text == "bold now" and rows[2].text_html == "<strong>bold</strong> now"
    chat = (await db.execute(select(Chat).where(Chat.telegram_id == 77).execution_options(populate_existing=True))).scalar_one()
    assert chat.last_message_text == "bold now"
    assert (await db.execute(sa_text("SELECT COUNT(*) FROM sync_text_repair"))).scalar() == 0


# ── 7. packaged shutdown runs lifespan shutdown ──────────────────────────────

def test_should_exit_runs_shutdown_hooks():
    import uvicorn
    from fastapi import FastAPI

    ran = threading.Event()
    app = FastAPI()

    @app.on_event("shutdown")
    async def _bye():
        ran.set()

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        threading.Event().wait(0.05)
    server.should_exit = True  # what main.py does when the window closes
    t.join(timeout=10)
    assert not t.is_alive() and ran.is_set()


# ── 8. vault lock ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_endpoint_stops_runtime_and_locks_vault(db_engine):
    from backend.api.router import create_app
    from backend.api import auth
    from backend.api.dependencies import generate_session_token
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        assert (await c.post("/api/auth/lock")).status_code == 401
        token = generate_session_token()
        with patch.object(auth.runtime, "stop", AsyncMock()) as stop, \
             patch.object(auth, "lock_vault_fn") as lock, \
             patch.object(auth, "revoke_session", AsyncMock()) as revoke:
            r = await c.post("/api/auth/lock", headers={"X-Session-Token": token})
    assert r.status_code == 200 and stop.called and lock.called and revoke.called


@pytest.mark.asyncio
async def test_vault_lock_from_any_path_stops_runtime():
    from backend.telegram import runtime as rt_mod
    with patch.object(rt_mod.runtime, "stop", AsyncMock()) as stop:
        rt_mod._on_vault_lock()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    assert stop.called


# ── 9. multi-file partial failure ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_files_reports_per_file_results_and_never_resends(api, db):
    await seed_chat(db)

    class FileClient:
        def __init__(self):
            self.sends = []

        async def is_user_authorized(self):
            return True

        async def send_file(self, chat_id, file, caption=None, reply_to=None, force_document=False):
            self.sends.append((file, caption, force_document))
            if force_document:
                raise ConnectionError("upload dropped")
            files = file if isinstance(file, list) else [file]
            msgs = [tg_msg(700 + len(self.sends) * 10 + i, caption[i] if isinstance(caption, list) else caption, out=True)
                    for i in range(len(files))]
            return msgs if isinstance(file, list) else msgs[0]

    client = FileClient()
    files = [("files", ("a.jpg", b"1", "image/jpeg")), ("files", ("b.png", b"2", "image/png")),
             ("files", ("c.pdf", b"3", "application/pdf"))]
    data = {"chat_id": "77", "text": "cap", "client_nonce": "nf-1"}
    with use_client(client):
        r1 = await api.post("/api/compose/send-files", data=data, files=files)
        r2 = await api.post("/api/compose/send-files", data=data, files=files)
    assert r1.status_code == 207 and r2.status_code == 207 and r1.json() == r2.json()
    body = r1.json()
    assert body["partial"] is True and body["success"] is False
    assert [(f["filename"], f["status"]) for f in body["files"]] == [("a.jpg", "sent"), ("b.png", "sent"), ("c.pdf", "failed")]
    assert body["files"][2]["error"] == "upload dropped"
    assert len(client.sends) == 2  # album + documents; the retry sent nothing
    assert client.sends[0][1] == ["cap", ""]

    with use_client(client):
        r3 = await api.post("/api/compose/send-files", data={"chat_id": "77", "client_nonce": "nf-2"},
                            files=[("files", ("d.pdf", b"4", "application/pdf"))])
    assert r3.status_code == 400 and r3.json()["code"] == "send_failed"


# ── 10. truncated media ──────────────────────────────────────────────────────

def test_file_is_complete(tmp_path):
    from backend.telegram.media import file_is_complete
    jpg = tmp_path / "a.jpg"
    jpg.write_bytes(b"\xff\xd8data\xff\xd9")
    assert file_is_complete(jpg, "photo", None)
    jpg.write_bytes(b"\xff\xd8da")
    assert not file_is_complete(jpg, "photo", None)
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"12345")
    assert file_is_complete(doc, "document", 5) and not file_is_complete(doc, "document", 9)
    assert not file_is_complete(tmp_path / "missing", "video", None)


@pytest.mark.asyncio
async def test_audit_clears_truncated_files(db, tmp_path):
    from backend.telegram import media
    await seed_chat(db)
    (tmp_path / "media" / "77").mkdir(parents=True)
    (tmp_path / "media" / "77" / "1.jpg").write_bytes(b"\xff\xd8trunc")
    (tmp_path / "media" / "77" / "2.jpg").write_bytes(b"\xff\xd8ok\xff\xd9")
    (tmp_path / "media" / "77" / "3.mp4").write_bytes(b"short")
    rows = [
        {**message_to_row(tg_msg(1, ""), 77), "media_type": "photo", "media_path": "media/77/1.jpg"},
        {**message_to_row(tg_msg(2, ""), 77), "media_type": "photo", "media_path": "media/77/2.jpg"},
        {**message_to_row(tg_msg(3, ""), 77), "media_type": "video", "media_size": 1000, "media_path": "media/77/3.mp4"},
    ]
    await crud.save_messages(db, rows)
    with patch.object(media, "DATA_DIR", tmp_path):
        assert await media.audit_media_files() == 2
    got = {m.tg_message_id: m.media_path for m in (await db.execute(
        select(Message).where(Message.chat_id == 77).execution_options(populate_existing=True))).scalars()}
    assert got == {1: None, 2: "media/77/2.jpg", 3: None}
    assert not (tmp_path / "media" / "77" / "1.jpg").exists()
