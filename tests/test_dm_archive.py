"""DM archive: full walks, top-ups, FloodWait, crash resume, priority requests,
status counts and the message-kind contract — against a fake Telethon client
that pages history the way Telethon's iter_messages does."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from telethon.errors import FloodWaitError
from telethon.tl import types as T

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, HistoryCoverage, Message
from backend.telegram import ids as tg_ids

BASE = datetime(2024, 1, 1, tzinfo=timezone.utc)


# ── fake Telegram ────────────────────────────────────────────────────────────

class Crash(BaseException):
    """Process death mid-walk: not an Exception, so nothing records it."""


class FakeDialog:
    def __init__(self, chat_id, entity, name, date, folder_id=None):
        self.id = chat_id
        self.entity = entity
        self.name = name
        self.date = date
        self.folder_id = folder_id
        self.unread_count = 0
        self.unread_mentions_count = 0


class FakeTelegram:
    """Per-chat histories (ascending by id) with Telethon's iter_messages
    semantics: newest→oldest below ``offset_id`` and above ``min_id``, or
    oldest→newest above ``max(offset_id, min_id)`` with ``reverse=True``."""

    def __init__(self):
        self.history: dict[int, list] = {}
        self.dialogs: list[FakeDialog] = []
        self.calls: list[tuple] = []
        self.flood_after: dict[int, int] = {}
        self.crash_after: dict[int, int] = {}
        self.error_for: dict[int, Exception] = {}
        self.on_yield = None  # async callback(chat_id, n_yielded)

    # -- setup helpers --
    def add_user(self, uid, name, n, *, bot=False, deleted=False, is_self=False, folder_id=None, kinds=True):
        user = T.User(id=uid, first_name=name, bot=bot, deleted=deleted, is_self=is_self)
        self.history[uid] = []
        self.add_messages(uid, n, kinds=kinds)
        self.dialogs.append(FakeDialog(uid, user, name, self.last_date(uid), folder_id))
        return user

    def add_group(self, gid, title, n):
        chat = T.Chat(id=gid, title=title, photo=T.ChatPhotoEmpty(), participants_count=5, date=BASE, version=1)
        cid = -gid
        self.history[cid] = []
        self.add_messages(cid, n)
        self.dialogs.append(FakeDialog(cid, chat, title, self.last_date(cid)))
        return cid

    def last_date(self, chat_id):
        h = self.history[chat_id]
        return h[-1].date if h else BASE

    def add_messages(self, chat_id, n, kinds=False):
        h = self.history[chat_id]
        start = h[-1].id + 1 if h else 1
        peer = T.PeerUser(chat_id) if chat_id > 0 else T.PeerChat(-chat_id)
        for i in range(start, start + n):
            date = BASE + timedelta(minutes=i)
            if kinds and i % 50 == 7:
                msg = T.MessageService(id=i, peer_id=peer, date=date, out=bool(i % 2),
                                       action=T.MessageActionPhoneCall(call_id=i, duration=65,
                                                                       reason=T.PhoneCallDiscardReasonHangup()))
            elif kinds and i % 50 == 8:
                poll = T.MessageMediaPoll(
                    poll=T.Poll(id=i, question=T.TextWithEntities("Lunch?", []), hash=0, answers=[
                        T.PollAnswer(T.TextWithEntities("Yes", []), b"0"),
                        T.PollAnswer(T.TextWithEntities("No", []), b"1"),
                    ]),
                    results=T.PollResults(results=[T.PollAnswerVoters(b"0", voters=2)], total_voters=2),
                )
                msg = T.Message(id=i, peer_id=peer, date=date, message="", media=poll, out=False)
            elif kinds and i % 50 == 9:
                msg = T.Message(id=i, peer_id=peer, date=date, message="fwd text", out=False,
                                fwd_from=T.MessageFwdHeader(date=BASE, from_name="Hidden Bob"))
            else:
                msg = T.Message(id=i, peer_id=peer, date=date, message=f"m{i}", out=bool(i % 3 == 0))
            h.append(msg)
        return list(range(start, start + n))

    # -- client API --
    async def iter_dialogs(self, limit=None, folder=None):
        for d in self.dialogs:
            if folder is None or (d.folder_id or 0) == folder:
                yield d

    async def iter_messages(self, chat_id, limit=None, *, offset_id=0, min_id=0, reverse=False):
        """``chat_id`` is the Telegram peer the archive resolved."""
        self.calls.append((chat_id, offset_id, min_id, reverse))
        if chat_id in self.error_for:
            raise self.error_for[chat_id]
        if chat_id not in self.history:
            raise ValueError(f"Could not find the input entity for {chat_id}")
        h = self.history[chat_id]
        if reverse:
            lo = max(offset_id, min_id)
            msgs = [m for m in h if m.id > lo]
        else:
            msgs = [m for m in reversed(h) if (not offset_id or m.id < offset_id) and m.id > min_id]
        n = 0
        for m in msgs:
            if limit is not None and n >= limit:
                return
            if self.flood_after.get(chat_id) == n:
                del self.flood_after[chat_id]
                raise FloodWaitError(request=None, capture=0)
            if self.crash_after.get(chat_id) == n:
                del self.crash_after[chat_id]
                raise Crash()
            n += 1
            yield m
            if self.on_yield is not None:
                await self.on_yield(chat_id, n)


@pytest.fixture
def fast_limiter(monkeypatch):
    from backend.telegram import coordinator as coord_mod
    monkeypatch.setattr(coord_mod.coordinator, "limiter", coord_mod.RateLimiter(rate=10000, burst=100))


@pytest.fixture
def broadcasts(monkeypatch):
    from backend.ws.manager import ws_manager
    sent = []

    async def record(data):
        sent.append(data)
    monkeypatch.setattr(ws_manager, "broadcast", record)
    return sent


@pytest.fixture
def tg(monkeypatch, fast_limiter):
    from backend.telegram import sync as tg_sync
    from backend.telegram.runtime import runtime
    fake = FakeTelegram()
    monkeypatch.setattr("backend.telegram.client.connected_client", AsyncMock(return_value=fake))
    monkeypatch.setattr(tg_sync, "last_full_dialog_sync", None)
    return fake


@pytest_asyncio.fixture
async def arch():
    from backend.telegram.archive import DMArchive
    a = DMArchive()
    yield a
    a.stop()
    await asyncio.sleep(0)


async def wait_until(cond, timeout=15.0, describe=None):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if await cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError(f"condition not reached: {describe() if describe else ''}")


async def conv_id(peer) -> int:
    async with AsyncSessionLocal() as db:
        conv = await tg_ids.conversation_for(db, peer)
    return conv.id if conv is not None else None


async def stored_ids(peer):
    """Telegram ids (= seq) stored for a peer's conversation."""
    cid = await conv_id(peer)
    if cid is None:
        return []
    async with AsyncSessionLocal() as db:
        return sorted(r[0] for r in (await db.execute(
            select(Message.seq).where(Message.conversation_id == cid))).all())


async def coverage(peer):
    cid = await conv_id(peer)
    if cid is None:
        return None
    async with AsyncSessionLocal() as db:
        return (await crud.get_coverage(db, [cid])).get(cid)


async def ensure(peer, entity):
    async with AsyncSessionLocal() as db:
        return (await tg_ids.upsert_dialog(db, peer, entity)).id


def all_ids(fake, chat_id):
    return [m.id for m in fake.history[chat_id]]


async def run_until_idle(arch, expect_complete):
    arch.auto_start()

    async def done():
        st = await arch.refresh_status()
        return arch.state == "idle" and st["dms"]["complete"] == expect_complete and not arch._queue
    await wait_until(done, describe=lambda: (arch.get_status(), arch._queue))


# ── full walk + kinds ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_full_walk_stores_every_message_and_skips_groups_and_bots(db, tg, arch, broadcasts):
    tg.add_user(101, "Alice", 437)
    tg.add_user(102, "Bob", 100)  # exactly one page
    tg.add_user(103, "Empty", 0)
    tg.add_user(104, "Archived Carol", 33, folder_id=1)
    tg.add_user(105, "Helper Bot", 20, bot=True)
    tg.add_user(106, "Deleted Account", 12, deleted=True)
    tg.add_user(999, "Me", 5, is_self=True)
    gid = tg.add_group(500, "Group", 250)

    await run_until_idle(arch, expect_complete=6)

    for uid in (101, 102, 103, 104, 106, 999):
        assert await stored_ids(uid) == all_ids(tg, uid), uid
        cov = await coverage(uid)
        assert cov.history_complete and cov.error is None
        assert cov.newest_contiguous_seq == (all_ids(tg, uid)[-1] if tg.history[uid] else 0)
    assert await stored_ids(105) == []  # bot skipped
    assert await stored_ids(gid) == []  # group not walked without a request
    assert await coverage(gid) is None

    st = arch.get_status()
    assert st["dms"] == {"total": 6, "complete": 6, "pending": 0, "failed": 0, "bots_skipped": 1}
    assert st["state"] == "idle" and st["groups_queued"] == 0 and st["started_at"]
    assert any(b["type"] == "archive_progress" for b in broadcasts)

    async with AsyncSessionLocal() as s:
        chats = {tg_ids.peer_id_of(c): c for c in (await s.execute(select(Conversation))).scalars()}
    assert chats[104].is_archived and not chats[101].is_archived
    assert chats[105].is_bot and chats[106].is_deleted_account and chats[999].is_self
    assert chats[101].history_complete and not chats[gid].history_complete
    assert chats[101].kind == "direct" and chats[gid].kind == "group"

    # Service, poll and forward rows carry the contract fields
    async with AsyncSessionLocal() as s:
        rows = {m.seq: m for m in (await s.execute(
            select(Message).where(Message.conversation_id == chats[101].id))).scalars()}
    call, poll, fwd, plain = rows[7], rows[8], rows[9], rows[10]
    assert call.kind == "service" and call.text in ("Outgoing call (1:05)", "Incoming call (1:05)")
    assert json.loads(call.meta_json)["service"] == {"action": "phone_call", "duration": 65, "video": False}
    assert poll.kind == "message" and poll.text.startswith("Poll: Lunch?")
    assert json.loads(poll.meta_json)["poll"]["options"] == [{"text": "Yes", "voters": 2}, {"text": "No", "voters": None}]
    assert fwd.fwd_from_name == "Hidden Bob" and json.loads(fwd.meta_json)["forward"]["from_name"] == "Hidden Bob"
    assert plain.kind == "message" and plain.meta_json is None and plain.fwd_from_name is None


# ── FloodWait ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_floodwait_mid_walk_waits_and_resumes_from_cursor(db, tg, arch, broadcasts):
    tg.add_user(201, "Dana", 350)
    tg.flood_after[201] = 150  # after one saved page + 50 unsaved

    await run_until_idle(arch, expect_complete=1)

    assert await stored_ids(201) == all_ids(tg, 201)
    walks = [c for c in tg.calls if c[0] == 201 and not c[3]]
    assert walks[0][1] == 0 and walks[1][1] == 251  # resumed below the saved page
    assert any(b.get("state") == "waiting_flood" and b.get("flood_wait_until") for b in broadcasts)


# ── crash resume ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_crash_mid_chat_resumes_from_walk_cursor_without_gaps(db, tg, fast_limiter):
    from backend.telegram.archive import DMArchive
    eve = tg.add_user(301, "Eve", 420)
    cid = await ensure(301, eve)
    tg.crash_after[301] = 230  # two pages saved, 30 messages in flight

    first = DMArchive()
    with pytest.raises(Crash):
        await first._process(tg, cid, preemptible=False)
    cov = await coverage(301)
    assert not cov.history_complete
    assert cov.walk_cursor_seq == 221 and cov.newest_contiguous_seq == 420
    assert await stored_ids(301) == list(range(221, 421))

    second = DMArchive()  # fresh process: state comes from the DB only
    await second._process(tg, cid, preemptible=False)
    assert tg.calls[-2] == (301, 221, 0, False)  # walk resumed at the cursor
    assert await stored_ids(301) == all_ids(tg, 301)
    cov = await coverage(301)
    assert cov.history_complete and cov.oldest_seq == 1 and cov.newest_contiguous_seq == 420


# ── top-up ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_topup_after_missed_live_updates_leaves_no_holes(db, tg, arch):
    tg.add_user(401, "Finn", 120)
    await run_until_idle(arch, expect_complete=1)
    assert await stored_ids(401) == all_ids(tg, 401)

    # 250 new messages; the live handler only caught two of them
    new = tg.add_messages(401, 250)
    from backend.telegram.convert import rows_for
    cid = await conv_id(401)
    async with AsyncSessionLocal() as s:
        await crud.save_messages(s, await rows_for(s, cid, [m for m in tg.history[401] if m.id in (130, 300)], forum=False))
    assert len(await stored_ids(401)) == 122

    await arch._process(tg, cid, preemptible=False)
    assert await stored_ids(401) == all_ids(tg, 401)
    assert (await coverage(401)).newest_contiguous_seq == new[-1]
    topups = [c for c in tg.calls if c[0] == 401 and c[3]]
    assert topups[-1] == (401, 0, 120, True)


@pytest.mark.asyncio
async def test_messages_arriving_during_walk_are_topped_up(db, tg, arch):
    tg.add_user(402, "Gus", 250)

    async def arrive(chat_id, n):
        if chat_id == 402 and n == 120 and len(tg.history[402]) == 250:
            tg.add_messages(402, 7)  # arrive mid-walk, above the walk's start
    tg.on_yield = arrive
    await run_until_idle(arch, expect_complete=1)
    assert await stored_ids(402) == all_ids(tg, 402) == list(range(1, 258))
    assert (await coverage(402)).newest_contiguous_seq == 257


@pytest.mark.asyncio
async def test_periodic_topup_only_for_dms_with_new_activity(db, tg, arch):
    tg.add_user(403, "Hal", 10)
    tg.add_user(404, "Ivy", 10)
    await run_until_idle(arch, expect_complete=2)
    tg.calls.clear()
    async with AsyncSessionLocal() as s:
        c403 = await conv_id(403)
        cov = (await crud.get_coverage(s, [c403]))
        cov[c403].last_topup_at = datetime(2000, 1, 1)  # activity moved since
        await s.commit()
    tg.add_messages(403, 3)
    arch.last_topup_at = None  # pass due now
    arch._poke()

    async def topped():
        return any(c[0] == 403 and c[3] for c in tg.calls) and arch.state == "idle"
    await wait_until(topped)
    assert not any(c[0] == 404 for c in tg.calls)
    assert await stored_ids(403) == all_ids(tg, 403)


# ── priority / groups ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_request_chat_jumps_the_queue_and_walks_a_group(db, tg, arch):
    tg.add_user(501, "Jo", 300)
    gid = tg.add_group(600, "Big group", 230)
    requested = []

    async def interrupt(chat_id, n):
        if chat_id == 501 and n == 150 and not requested:
            requested.append(True)
            await arch.request_conversation(await conv_id(gid))
    tg.on_yield = interrupt

    await run_until_idle(arch, expect_complete=1)
    order = [c[0] for c in tg.calls if not c[3]]
    assert order[:3] == [501, gid, 501]  # DM walk yielded to the group, then resumed
    resumed = tg.calls[[c[0] for c in tg.calls].index(gid) + 2]  # after group walk + top-up
    assert resumed == (501, 101, 0, False)  # two pages (300..101) were saved before yielding
    assert await stored_ids(gid) == all_ids(tg, gid)
    assert await stored_ids(501) == all_ids(tg, 501)
    assert (await coverage(gid)).history_complete


@pytest.mark.asyncio
async def test_request_and_wait_for_chat(db, tg, arch):
    gid = tg.add_group(700, "Group", 40)
    cid = await ensure(gid, tg.dialogs[-1].entity)
    await arch.request_conversation(cid)
    assert arch.get_status()["groups_queued"] == 1
    arch.auto_start()
    cov = await arch.wait_for_conversation(cid, timeout=10)
    assert cov is not None and cov.history_complete and cov.newest_contiguous_seq == 40
    assert await stored_ids(gid) == all_ids(tg, gid)


@pytest.mark.asyncio
async def test_pause_stops_backlog_but_serves_requests(db, tg, arch):
    tg.add_user(801, "Kim", 30)
    gid = tg.add_group(900, "G", 10)
    arch.pause()
    arch.auto_start()

    async def paused():
        return arch.state == "paused"
    await wait_until(paused)
    assert await stored_ids(801) == []
    cid = await ensure(gid, tg.dialogs[-1].entity)
    await arch.request_conversation(cid)
    cov = await arch.wait_for_conversation(cid, timeout=10)
    assert cov.history_complete and await stored_ids(801) == []
    arch.start()
    await run_until_idle(arch, expect_complete=1)
    assert await stored_ids(801) == all_ids(tg, 801)


# ── errors ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_per_chat_error_is_recorded_and_others_continue(db, tg, arch):
    tg.add_user(1001, "Lee", 15)
    tg.add_user(1002, "Gone", 15)
    tg.error_for[1002] = ValueError("Could not find the input entity")
    arch.auto_start()

    async def settled():
        st = await arch.refresh_status()
        return arch.state == "idle" and st["dms"]["complete"] == 1 and st["dms"]["failed"] == 1
    await wait_until(settled)
    cov = await coverage(1002)
    assert cov.attempts == 1 and "input entity" in cov.error and not cov.history_complete
    assert arch.get_status()["dms"]["pending"] == 0
    assert await stored_ids(1001) == all_ids(tg, 1001)


# ── API contract ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_contract(api, tg, arch, monkeypatch):
    from backend.telegram import archive as archive_mod
    monkeypatch.setattr(archive_mod, "archive", arch)
    for name in ("get_status", "refresh_status", "request_conversation", "wait_for_conversation", "start", "pause"):
        monkeypatch.setattr(archive_mod, name, getattr(arch, name))

    tg.add_user(1101, "Mia", 60)
    await run_until_idle(arch, expect_complete=1)

    r = await api.get("/api/archive/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"state", "flood_wait_until", "dms", "current_chat", "started_at",
                         "last_topup_at", "groups_queued"}
    assert body["dms"]["complete"] == 1 and body["started_at"].endswith("Z")

    r = await api.post("/api/archive/pause")
    assert r.json()["state"] == "paused"
    r = await api.post("/api/archive/start")
    assert r.json()["state"] in ("running", "idle")

    cid = await conv_id(1101)
    r = await api.post(f"/api/conversations/{cid}/archive-history")
    assert r.status_code == 200
    assert r.json() == {"queued": True, "coverage": {
        "history_complete": True, "message_count": 60, "oldest_seq": 1, "newest_contiguous_seq": 60}}
    assert (await api.post("/api/conversations/424242/archive-history")).status_code == 404

    r = await api.post("/api/conversations/by-ids", json={"ids": [cid]})
    chat = r.json()[0]
    assert chat["history_complete"] is True and chat["is_bot"] is False and chat["is_deleted_account"] is False

    async with AsyncSessionLocal() as s:
        await crud.touch_synced(s, cid)  # fresh: no auto-sync against the fake
    r = await api.get(f"/api/conversations/{cid}/messages/window", params={"limit": 60})
    msgs = {m["seq"]: m for m in r.json()["messages"]}
    assert msgs[7]["kind"] == "service" and msgs[7]["meta"]["service"]["action"] == "phone_call"
    assert msgs[8]["meta"]["poll"]["question"] == "Lunch?"
    assert msgs[9]["fwd_from_name"] == "Hidden Bob"
    assert msgs[10]["kind"] == "message" and msgs[10]["meta"] is None


@pytest.mark.asyncio
async def test_coverage_absent_means_not_complete(db):
    cid = await ensure(1301, T.User(id=1301, first_name="Ola"))
    async with AsyncSessionLocal() as s:
        assert await crud.get_coverage(s, [cid]) == {}
        conv = await crud.get_conversation(s, cid)
        assert conv.history_complete is False
        s.add(HistoryCoverage(conversation_id=cid, history_complete=True))
        await s.commit()
        conv = (await s.execute(select(Conversation).where(Conversation.id == cid)
                                .execution_options(populate_existing=True))).scalar_one()
        assert conv.history_complete is True


@pytest.mark.asyncio
async def test_live_service_message_triggers_chat_sync(db, monkeypatch):
    from backend.telegram import sync as tg_sync
    from backend.telegram.coordinator import SyncResult, coordinator

    class Client:
        def __init__(self):
            self.handlers = {}

        def add_event_handler(self, cb, ev):
            self.handlers[cb.__name__] = cb
    client = Client()
    tg_sync.register_event_handlers(client)
    cid = await ensure(1401, T.User(id=1401, first_name="Pia"))
    sync = AsyncMock(return_value=SyncResult("changed", cid, added=1))
    monkeypatch.setattr(coordinator, "sync_conversation", sync)
    svc = T.MessageService(id=9, peer_id=T.PeerUser(1401), date=BASE,
                           action=T.MessageActionContactSignUp())
    await client.handlers["on_raw_update"](T.UpdateNewMessage(message=svc, pts=1, pts_count=1))
    plain = T.Message(id=10, peer_id=T.PeerUser(1401), date=BASE, message="hi")
    await client.handlers["on_raw_update"](T.UpdateNewMessage(message=plain, pts=2, pts_count=1))
    from backend.jobs import get_running
    task = get_running("service_sync:1401")
    assert task is not None
    await task
    sync.assert_awaited_once_with(cid, 20, max_wait=60)
