"""MCP agent workflow: DM listing, keyset paging, write gating, notes, reviews.

Runs every tool over the real MCP HTTP endpoint against a temp DB, including
a full "review everyone I've DMed" agent simulation that must read exactly
the stored messages of every non-bot DM.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.api import mcp as mcp_module
from backend.api import mcp_tools
from backend.config import API_PORT
from backend.db import database
from backend.db.models import AgentReview, Chat, ChatHistoryCoverage, Contact, Message, PersonNote

BASE_URL = f"http://127.0.0.1:{API_PORT}"
KEY = "test-mcp-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
T0 = datetime(2025, 1, 1)

ALICE, BOB, CAROL, BOT, DAVE, ERIN, SELF, GROUP = 1001, 1002, 1003, 1004, 1005, 1006, 1007, -100500


@pytest.fixture(autouse=True)
def _mcp_available():
    mcp_module.clear_mcp_sessions()
    with patch("backend.api.mcp.get_mcp_enabled", return_value=True), \
         patch("backend.api.mcp.get_secret", return_value=KEY):
        yield
    mcp_module.clear_mcp_sessions()


@pytest.fixture(autouse=True)
def _telegram_online(monkeypatch):
    async def _connected():
        return True

    monkeypatch.setattr(mcp_tools, "_telegram_connected", _connected)


@pytest.fixture
def writes_on():
    with patch("backend.api.mcp.get_mcp_write_enabled", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _quiet_ws(monkeypatch):
    sent = []

    async def _capture(data):
        sent.append(data)

    from backend.ws.manager import ws_manager
    monkeypatch.setattr(ws_manager, "broadcast", _capture)
    return sent


class FakeArchive:
    """Stands in for backend.telegram.archive: a "walk" stores the chat's
    missing older messages and marks its history complete."""

    def __init__(self):
        self.pending = {}  # chat_id -> rows the walk will store
        self.requested = []

    def get_status(self):
        return {"state": "idle", "flood_wait_until": None, "dms": {"total": 0}}

    async def refresh_status(self):
        return self.get_status()

    async def request_chat(self, chat_id, priority=True):
        self.requested.append(chat_id)

    async def wait_for_chat(self, chat_id, timeout):
        async with database.AsyncSessionLocal() as s:
            for row in self.pending.pop(chat_id, []):
                s.add(Message(**row))
            cov = await s.get(ChatHistoryCoverage, chat_id)
            if cov is None:
                cov = ChatHistoryCoverage(chat_id=chat_id)
                s.add(cov)
            cov.history_complete = True
            await s.commit()
            return cov


@pytest.fixture
def fake_archive(monkeypatch):
    fake = FakeArchive()
    monkeypatch.setattr(mcp_tools, "_archive", lambda: fake)
    return fake


@pytest.fixture
def app(db_engine):
    with patch("backend.api.auth.get_client", return_value=None), \
         patch("backend.api.auth.has_credentials", return_value=False):
        from backend.api.router import create_app
        return create_app()


class Agent:
    """Minimal MCP client over the HTTP endpoint."""

    def __init__(self, client):
        self.client = client
        self.sid = None
        self._id = 0

    async def start(self):
        resp = await self.client.post("/mcp", headers=AUTH, json={
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test-agent", "version": "0"}},
        })
        self.sid = resp.headers["mcp-session-id"]
        return resp.json()["result"]

    async def rpc(self, method, params=None):
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        resp = await self.client.post("/mcp", headers={**AUTH, "Mcp-Session-Id": self.sid}, json=body)
        return resp.json()

    async def call_raw(self, name, **args):
        return await self.rpc("tools/call", {"name": name, "arguments": args})

    async def call(self, name, **args):
        data = await self.call_raw(name, **args)
        assert "result" in data, data
        text = data["result"]["content"][0]["text"]
        assert data["result"]["isError"] is False, text
        return json.loads(text)

    async def call_error(self, name, **args) -> str:
        data = await self.call_raw(name, **args)
        assert data["result"]["isError"] is True, data
        return data["result"]["content"][0]["text"]


@pytest_asyncio.fixture
async def agent(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client:
        a = Agent(client)
        await a.start()
        yield a


def _msg(chat_id, tg_id, *, sender=None, text=None, kind="message", minutes=None, outgoing=False):
    return dict(
        tg_message_id=tg_id, chat_id=chat_id, sender_id=sender if sender is not None else chat_id,
        sender_name=f"user{sender or chat_id}", text=text if text is not None else f"msg {tg_id} in {chat_id}",
        date=T0 + timedelta(minutes=minutes if minutes is not None else tg_id), is_outgoing=outgoing, kind=kind,
    )


async def _seed(db, fake_archive=None):
    """Seven private chats + one group:
    ALICE  complete, 230 messages (incl. service rows, outgoing)
    BOB    complete, archived
    CAROL  complete, hidden
    BOT    complete bot (excluded unless include_bots)
    DAVE   incomplete: only ids 50-60 stored; the walk adds 1-49
    ERIN   complete, no messages at all
    SELF   Saved Messages (never listed)
    GROUP  group where ALICE posted
    """
    chats = [
        Chat(telegram_id=ALICE, name="Alice", type="private", username="alice"),
        Chat(telegram_id=BOB, name="Bob", type="private", is_archived=True),
        Chat(telegram_id=CAROL, name="Carol", type="private", is_hidden=True),
        Chat(telegram_id=BOT, name="Helper Bot", type="private", is_bot=True),
        Chat(telegram_id=DAVE, name="Dave", type="private"),
        Chat(telegram_id=ERIN, name="Erin", type="private"),
        Chat(telegram_id=SELF, name="Saved Messages", type="private", is_self=True),
        Chat(telegram_id=GROUP, name="Builders", type="group"),
    ]
    db.add_all(chats)
    rows = []
    for i in range(1, 231):
        kind = "service" if i % 50 == 0 else "message"
        rows.append(_msg(ALICE, i, kind=kind, outgoing=(i % 3 == 0), text=f"hello {i}",
                         sender=(9999 if i % 3 == 0 else ALICE)))
    rows += [_msg(BOB, i) for i in range(1, 4)]
    rows += [_msg(CAROL, i) for i in range(1, 6)]
    rows += [_msg(BOT, i) for i in range(1, 4)]
    rows += [_msg(DAVE, i) for i in range(50, 61)]
    rows += [_msg(GROUP, i, sender=ALICE, text="gm builders") for i in range(1, 4)]
    db.add_all(Message(**r) for r in rows)
    for chat_id in (ALICE, BOB, CAROL, BOT, ERIN, GROUP):
        db.add(ChatHistoryCoverage(chat_id=chat_id, history_complete=True))
    db.add(ChatHistoryCoverage(chat_id=DAVE, history_complete=False, walk_cursor_tg_id=50))
    await db.commit()
    if fake_archive is not None:
        fake_archive.pending[DAVE] = [_msg(DAVE, i) for i in range(1, 50)]


async def _all_pages(agent, tool, list_key, **args):
    items, cursor, pages = [], None, 0
    while True:
        page = await agent.call(tool, **args, **({"cursor": cursor} if cursor else {}))
        items += page[list_key]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            return items, pages


async def _stored_ids(chat_id):
    async with database.AsyncSessionLocal() as s:
        return sorted((await s.execute(
            select(Message.tg_message_id).where(Message.chat_id == chat_id)
        )).scalars().all())


# ── Completeness proof ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_reviews_every_dm_and_reads_every_message(agent, db, fake_archive, writes_on):
    await _seed(db, fake_archive)

    status = await agent.call("get_archive_status")
    assert status["dms"]["total_people"] == 5  # ALICE BOB CAROL DAVE ERIN (bot, self excluded)
    assert status["dms"]["bots_excluded"] == 1
    assert status["dms"]["history_incomplete"] == 1

    incomplete, _ = await _all_pages(agent, "list_dms", "dms", filter="incomplete")
    assert [d["chat_id"] for d in incomplete] == [DAVE]

    read = {}
    rounds = 0
    while True:
        rounds += 1
        assert rounds < 10, "review loop did not converge"
        queue, _ = await _all_pages(agent, "list_dms", "dms", filter="unreviewed", limit=2)
        if not queue:
            break
        for dm in queue:
            chat_id = dm["chat_id"]
            if not dm["coverage"]["history_complete"]:
                fetched = await agent.call("fetch_history", chat_id=chat_id, wait_seconds=5)
                assert fetched["history_complete"] is True
                # mark_reviewed is refused until then; read again from the start
            msgs, _ = await _all_pages(agent, "get_messages", "messages", chat_id=chat_id, order="asc", limit=37)
            ids = [m["tg_message_id"] for m in msgs]
            assert ids == sorted(ids) and len(ids) == len(set(ids))
            read[chat_id] = ids
            citations = [{"chat_id": chat_id, "tg_message_id": ids[0]}] if ids else []
            await agent.call("add_person_note", telegram_id=chat_id, kind="context",
                             title=f"About {dm['person']['name']}", body="Summary.", citations=citations)
            await agent.call("mark_reviewed", chat_id=chat_id, through_tg_message_id=ids[-1] if ids else 0)

    expected = {c: await _stored_ids(c) for c in (ALICE, BOB, CAROL, DAVE, ERIN)}
    assert read == expected
    assert len(read[ALICE]) == 230 and len(read[DAVE]) == 60 and read[ERIN] == []
    assert BOT not in read and SELF not in read and GROUP not in read

    status = await agent.call("get_archive_status")
    assert status["dms"]["unreviewed"] == 0
    assert status["dms"]["reviewed_up_to_date"] == 5

    # Every message kind came through, service rows included
    msgs, _ = await _all_pages(agent, "get_messages", "messages", chat_id=ALICE, order="asc", limit=100)
    assert {m["kind"] for m in msgs} == {"message", "service"}

    # A new incoming message puts exactly that DM back in the queue …
    db.add(Message(**_msg(BOB, 4)))
    await db.commit()
    queue, _ = await _all_pages(agent, "list_dms", "dms", filter="unreviewed")
    assert [d["chat_id"] for d in queue] == [BOB]
    assert queue[0]["review"]["reviewed_through_tg_id"] == 3
    # … and resuming after the reviewed point reads only the new one.
    new = await agent.call("get_messages", chat_id=BOB, order="asc", after_tg_message_id=3)
    assert [m["tg_message_id"] for m in new["messages"]] == [4]
    await agent.call("mark_reviewed", chat_id=BOB, through_tg_message_id=4)

    # An older message appearing after the review (backfill) also resurfaces the DM.
    db.add(Message(**_msg(CAROL, 0, minutes=-5)))
    await db.commit()
    queue, _ = await _all_pages(agent, "list_dms", "dms", filter="unreviewed")
    assert [d["chat_id"] for d in queue] == [CAROL]

    # Agent notes landed with author/label and citations
    notes = (await db.execute(select(PersonNote).order_by(PersonNote.id))).scalars().all()
    assert len(notes) == 5
    assert {n.author for n in notes} == {"agent"}
    assert {n.agent_label for n in notes} == {"test-agent"}


@pytest.mark.asyncio
async def test_list_dms_flags_and_filters(agent, db):
    await _seed(db)
    dms, _ = await _all_pages(agent, "list_dms", "dms")
    by_id = {d["chat_id"]: d for d in dms}
    assert set(by_id) == {ALICE, BOB, CAROL, DAVE, ERIN}
    assert by_id[BOB]["is_archived"] is True
    assert by_id[CAROL]["is_hidden"] is True
    assert by_id[ALICE]["coverage"]["message_count"] == 230
    assert by_id[ALICE]["coverage"]["newest_tg_id"] == 230
    assert by_id[ALICE]["person"]["username"] == "alice"
    assert by_id[ALICE]["review"]["up_to_date"] is False

    with_bots, _ = await _all_pages(agent, "list_dms", "dms", include_bots=True)
    assert {d["chat_id"] for d in with_bots} == set(by_id) | {BOT}
    assert next(d for d in with_bots if d["chat_id"] == BOT)["is_bot"] is True

    no_archived, _ = await _all_pages(agent, "list_dms", "dms", include_archived=False)
    assert BOB not in {d["chat_id"] for d in no_archived}


@pytest.mark.asyncio
async def test_mark_reviewed_refused_on_incomplete_history(agent, db, writes_on):
    await _seed(db)
    err = await agent.call_error("mark_reviewed", chat_id=DAVE, through_tg_message_id=60)
    assert "fetch_history" in err
    assert (await db.get(AgentReview, DAVE)) is None


@pytest.mark.asyncio
async def test_mark_reviewed_rejects_unstored_message(agent, db, writes_on):
    await _seed(db)
    err = await agent.call_error("mark_reviewed", chat_id=BOB, through_tg_message_id=999)
    assert "not stored" in err
    err = await agent.call_error("mark_reviewed", chat_id=BOB, through_tg_message_id=0)
    assert "empty chat" in err


# ── Keyset paging ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_dms_paging_stable_under_concurrent_inserts(agent, db):
    await _seed(db)
    seen, cursor, n = [], None, 0
    while True:
        page = await agent.call("list_dms", limit=2, **({"cursor": cursor} if cursor else {}))
        seen += [d["chat_id"] for d in page["dms"]]
        # New DMs and fresh activity on already-listed DMs between pages
        n += 1
        db.add(Chat(telegram_id=5000 + n, name=f"New {n}", type="private"))
        db.add(Message(**_msg(ALICE, 1000 + n)))
        await db.commit()
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen))
    assert {ALICE, BOB, CAROL, DAVE, ERIN} <= set(seen)
    assert any(c > 5000 for c in seen)  # DMs added while paging still show up


@pytest.mark.asyncio
async def test_get_messages_asc_paging_sees_new_messages_once(agent, db):
    await _seed(db)
    seen, cursor, extra = [], None, 300
    while True:
        page = await agent.call("get_messages", chat_id=ALICE, order="asc", limit=40,
                                **({"cursor": cursor} if cursor else {}))
        seen += [m["tg_message_id"] for m in page["messages"]]
        extra += 1
        if extra <= 303:
            db.add(Message(**_msg(ALICE, extra)))
            await db.commit()
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == await _stored_ids(ALICE)


@pytest.mark.asyncio
async def test_get_messages_desc_default_and_cursor_scope(agent, db):
    await _seed(db)
    page = await agent.call("get_messages", chat_id=ALICE, limit=5)
    assert [m["tg_message_id"] for m in page["messages"]] == [230, 229, 228, 227, 226]
    nxt = await agent.call("get_messages", chat_id=ALICE, limit=5, cursor=page["next_cursor"])
    assert nxt["messages"][0]["tg_message_id"] == 225
    # A cursor is bound to its chat and order
    err = await agent.call_error("get_messages", chat_id=ALICE, order="asc", cursor=page["next_cursor"])
    assert "Invalid cursor" in err
    err = await agent.call_error("get_messages", chat_id=BOB, cursor=page["next_cursor"])
    assert "Invalid cursor" in err
    err = await agent.call_error("get_messages", chat_id=ALICE, cursor="garbage!!")
    assert "Invalid cursor" in err


@pytest.mark.asyncio
async def test_get_messages_incomplete_history_points_to_fetch(agent, db):
    await _seed(db)
    page = await agent.call("get_messages", chat_id=DAVE, order="asc")
    assert page["history_complete"] is False
    assert "fetch_history" in page["note"]


@pytest.mark.asyncio
async def test_search_messages_pages_past_100(agent, db):
    await _seed(db)
    hits, pages = await _all_pages(agent, "search_messages", "messages", query="hello", limit=100)
    assert pages == 3
    keys = [(h["chat_id"], h["tg_message_id"]) for h in hits]
    assert len(keys) == len(set(keys)) == 230
    first = await agent.call("search_messages", query="hello", limit=10)
    assert first["total"] == 230
    in_chat, _ = await _all_pages(agent, "search_messages", "messages", query="gm", chat_id=GROUP)
    assert len(in_chat) == 3


@pytest.mark.asyncio
async def test_list_chats_type_filter_and_stable_sort(agent, db):
    await _seed(db)
    chats, _ = await _all_pages(agent, "list_chats", "chats", sort_by="stable", limit=3)
    ids = [c["telegram_id"] for c in chats]
    assert len(ids) == len(set(ids)) == 8  # all chats, hidden/archived/self included
    groups, _ = await _all_pages(agent, "list_chats", "chats", type="group")
    assert [c["telegram_id"] for c in groups] == [GROUP]
    assert groups[0]["coverage"]["message_count"] == 3
    recent, _ = await _all_pages(agent, "list_chats", "chats", limit=3)
    assert sorted(c["telegram_id"] for c in recent) == sorted(ids)
    unarchived, _ = await _all_pages(agent, "list_chats", "chats", include_archived=False, sort_by="alpha")
    assert BOB not in {c["telegram_id"] for c in unarchived}


# ── People ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_person_aggregates(agent, db, writes_on):
    await _seed(db)
    await agent.call("add_position", telegram_id=ALICE, organization="Acme", title="CTO")
    await agent.call("add_person_note", telegram_id=ALICE, kind="due_diligence", title="Checked",
                     body="ok", citations=[{"chat_id": GROUP, "tg_message_id": 1}])
    person = await agent.call("get_person", telegram_id=ALICE)
    assert person["name"] == "Alice"
    assert person["positions"][0]["organization"] == "Acme"
    assert person["organizations"][0]["name"] == "Acme"
    assert person["notes"][0]["citations"] == [{"chat_id": GROUP, "tg_message_id": 1}]
    assert person["dm"]["coverage"]["message_count"] == 230
    assert person["groups"] == [{
        "chat_id": GROUP, "name": "Builders", "type": "group",
        "messages_from_person": 3, "last_message_at": person["groups"][0]["last_message_at"],
    }]
    err = await agent.call_error("get_person", telegram_id=424242)
    assert "not found" in err


@pytest.mark.asyncio
async def test_fetch_history_requests_walk(agent, db, fake_archive):
    await _seed(db, fake_archive)
    out = await agent.call("fetch_history", chat_id=DAVE, wait_seconds=1)
    assert fake_archive.requested == [DAVE]
    assert out["history_complete"] is True
    assert out["coverage"]["message_count"] == 60


@pytest.mark.asyncio
async def test_fetch_history_reports_archive_failure(agent, db, fake_archive):
    await _seed(db)

    async def boom(chat_id, priority=True):
        raise RuntimeError("Telegram is not connected")

    fake_archive.request_chat = boom
    err = await agent.call_error("fetch_history", chat_id=DAVE)
    assert "Telegram is not connected" in err


# ── Write gating ─────────────────────────────────────────────────────────────

WRITE_CALLS = [
    ("add_person_note", {"telegram_id": ALICE, "kind": "context", "title": "t", "body": "b"}),
    ("update_person_note", {"note_id": 1, "title": "x"}),
    ("set_person_fields", {"telegram_id": ALICE, "is_important": True}),
    ("add_position", {"telegram_id": ALICE, "organization": "Acme"}),
    ("tag_chat", {"chat_id": ALICE, "tag_name": "vip"}),
    ("mark_reviewed", {"chat_id": BOB, "through_tg_message_id": 3}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", WRITE_CALLS)
async def test_write_tools_refused_when_disabled(agent, db, name, args):
    await _seed(db)
    with patch("backend.api.mcp.get_mcp_write_enabled", return_value=False):
        err = await agent.call_error(name, **args)
    assert "turned off" in err
    assert (await db.execute(select(PersonNote))).first() is None
    assert (await db.execute(select(AgentReview))).first() is None
    assert (await db.execute(select(Contact))).first() is None


@pytest.mark.asyncio
async def test_write_pref_controls_gate_end_to_end(agent, db, api):
    """The real pref (Settings › MCP toggle) — default off — gates writes."""
    await _seed(db)
    assert (await api.get("/api/settings/prefs/mcp")).json()["write_enabled"] is False
    await agent.call_error("tag_chat", chat_id=ALICE, tag_name="vip")
    resp = await api.post("/api/settings/prefs/mcp/write", json={"enabled": True})
    assert resp.json() == {"write_enabled": True}
    assert (await api.get("/api/settings/prefs/mcp")).json()["write_enabled"] is True
    out = await agent.call("tag_chat", chat_id=ALICE, tag_name="vip")
    assert out["tag"]["name"] == "vip"
    again = await agent.call("tag_chat", chat_id=ALICE, tag_name="VIP ")
    assert again["tag"]["id"] == out["tag"]["id"]


@pytest.mark.asyncio
async def test_citations_validated(agent, db, writes_on, _quiet_ws):
    await _seed(db)
    err = await agent.call_error("add_person_note", telegram_id=ALICE, kind="risk", title="t", body="b",
                                 citations=[{"chat_id": ALICE, "tg_message_id": 99999}])
    assert "not found locally" in err and f"{ALICE}/99999" in err
    assert (await db.execute(select(PersonNote))).first() is None

    note = await agent.call("add_person_note", telegram_id=ALICE, kind="risk", title="t", body="b",
                            citations=[{"chat_id": ALICE, "tg_message_id": 5}, {"chat_id": ALICE, "tg_message_id": 5}])
    assert note["citations"] == [{"chat_id": ALICE, "tg_message_id": 5}]
    assert note["author"] == "agent" and note["agent_label"] == "test-agent"
    assert {"type": "person_notes_changed", "telegram_id": ALICE} in _quiet_ws

    err = await agent.call_error("update_person_note", note_id=note["id"],
                                 citations=[{"chat_id": BOB, "tg_message_id": 77}])
    assert "not found locally" in err
    upd = await agent.call("update_person_note", note_id=note["id"], body="new body", citations=[])
    assert upd["body"] == "new body" and upd["citations"] == []


@pytest.mark.asyncio
async def test_agent_cannot_update_user_note(agent, db, writes_on):
    await _seed(db)
    db.add(PersonNote(telegram_id=ALICE, kind="context", title="mine", body="", author="user"))
    await db.commit()
    note_id = (await db.execute(select(PersonNote.id))).scalar()
    err = await agent.call_error("update_person_note", note_id=note_id, title="hijack")
    assert "agent-written" in err


@pytest.mark.asyncio
async def test_add_note_unknown_person_refused(agent, db, writes_on):
    await _seed(db)
    err = await agent.call_error("add_person_note", telegram_id=31337, kind="context", title="t", body="b")
    assert "not found" in err


@pytest.mark.asyncio
async def test_set_person_fields_creates_named_contact(agent, db, writes_on):
    await _seed(db)
    out = await agent.call("set_person_fields", telegram_id=CAROL, is_important=True,
                           relationship_status="nurturing", linkedin_url="linkedin.com/in/carol")
    assert out["name"] == "Carol"
    assert out["is_important"] is True
    assert out["relationship_status"] == "nurturing" and out["relationship_status_manual"] is True
    assert out["linkedin_url"] == "https://linkedin.com/in/carol"
    err = await agent.call_error("set_person_fields", telegram_id=DAVE, linkedin_url="https://evil.example.com")
    assert "linkedin" in err.lower()
    assert (await db.execute(select(Contact).where(Contact.telegram_id == DAVE))).first() is None
    auto = await agent.call("set_person_fields", telegram_id=CAROL, relationship_status="auto")
    assert auto["relationship_status_manual"] is False


@pytest.mark.asyncio
async def test_add_position_duplicate_is_tool_error(agent, db, writes_on):
    await _seed(db)
    await agent.call("add_position", telegram_id=ALICE, organization="Acme")
    err = await agent.call_error("add_position", telegram_id=ALICE, organization=" acme ")
    assert "Already has a position" in err


# ── Argument validation / prompts ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,fragment", [
    ("list_dms", {"include_bots": "yes"}, "boolean"),
    ("list_dms", {"filter": "everything"}, "one of"),
    ("get_messages", {"chat_id": ALICE, "order": "sideways"}, "one of"),
    ("get_messages", {"chat_id": True}, "integer"),
    ("add_person_note", {"telegram_id": ALICE, "kind": "context", "title": "t", "body": "b",
                         "citations": [{"chat_id": "x", "tg_message_id": 1}]}, "citations[0].chat_id"),
    ("add_person_note", {"telegram_id": ALICE, "kind": "context", "title": "t", "body": "b",
                         "citations": "nope"}, "array"),
    ("add_person_note", {"telegram_id": ALICE, "kind": "gossip", "title": "t", "body": "b"}, "one of"),
    ("add_person_note", {"telegram_id": ALICE, "kind": "context", "title": "", "body": "b"}, "empty"),
    ("fetch_history", {"chat_id": ALICE, "wait_seconds": 600}, "<= 60"),
    ("list_chats", {"offset": 5}, "Unknown argument"),
])
async def test_validate_args(agent, name, args, fragment):
    data = await agent.call_raw(name, **args)
    assert data["error"]["code"] == -32602
    assert fragment in data["error"]["message"]


@pytest.mark.asyncio
async def test_prompts_and_instructions(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL) as client:
        a = Agent(client)
        init = await a.start()
        assert "prompts" in init["capabilities"]
        assert "list_dms(filter='unreviewed')" in init["instructions"]
        assert "secret chats" in init["instructions"]
        prompts = (await a.rpc("prompts/list"))["result"]["prompts"]
        assert [p["name"] for p in prompts] == ["review_dms"]
        got = (await a.rpc("prompts/get", {"name": "review_dms", "arguments": {"focus": "investors"}}))["result"]
        text = got["messages"][0]["content"]["text"]
        assert "mark_reviewed" in text and "investors" in text
        assert (await a.rpc("prompts/get", {"name": "nope"}))["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_fetch_history_timeout_reports_progress_or_error(agent, db, fake_archive):
    await _seed(db)

    async def still_walking(chat_id, timeout):
        async with database.AsyncSessionLocal() as s:
            return await s.get(ChatHistoryCoverage, chat_id)  # current row, not complete

    fake_archive.wait_for_chat = still_walking
    out = await agent.call("fetch_history", chat_id=DAVE, wait_seconds=1)
    assert out["history_complete"] is False and "Still archiving" in out["note"]

    cov = await db.get(ChatHistoryCoverage, DAVE)
    cov.error = "FloodWait 300s"
    await db.commit()
    out = await agent.call("fetch_history", chat_id=DAVE, wait_seconds=1)
    assert "FloodWait 300s" in out["note"]


# ── Against the real backend.telegram.archive (no Telegram client) ──────────

@pytest.fixture
def real_archive(monkeypatch):
    """The real DMArchive state machine with its worker disabled: request_chat
    only queues, and the test plays the worker by writing coverage + resolving."""
    from backend.telegram import archive as archive_mod

    fresh = archive_mod.DMArchive()
    monkeypatch.setattr(fresh, "_ensure_worker", lambda: None)
    for name in ("get_status", "refresh_status", "request_chat", "wait_for_chat"):
        monkeypatch.setattr(archive_mod, name, getattr(fresh, name))
    return fresh


@pytest.mark.asyncio
async def test_real_archive_status_and_fetch_history(agent, db, real_archive):
    import asyncio

    await _seed(db)
    status = await agent.call("get_archive_status")
    # refresh_status recounted from the DB (get_status alone would still say 0)
    assert status["archive"]["dms"]["total"] >= 5
    assert status["archive"]["dms"]["complete"] >= 4
    assert status["archive"]["state"] == "stopped"

    real_archive.state = "running"  # worker up (stopped answers immediately; see below)
    # Times out while queued: current (incomplete) row comes back
    out = await agent.call("fetch_history", chat_id=DAVE, wait_seconds=0.2)
    assert real_archive.is_queued(DAVE)
    assert out["history_complete"] is False and "Still archiving" in out["note"]

    async def play_worker():
        await asyncio.sleep(0.2)
        async with database.AsyncSessionLocal() as s:
            s.add_all(Message(**_msg(DAVE, i)) for i in range(1, 50))
            (await s.get(ChatHistoryCoverage, DAVE)).history_complete = True
            await s.commit()
        real_archive._queue.remove(DAVE)
        real_archive._resolve(DAVE)

    worker = asyncio.ensure_future(play_worker())
    out = await agent.call("fetch_history", chat_id=DAVE, wait_seconds=10)
    await worker
    assert out["history_complete"] is True
    assert out["coverage"]["message_count"] == 60


@pytest.mark.asyncio
async def test_fetch_history_offline_returns_immediately(agent, db, fake_archive, monkeypatch):
    await _seed(db, fake_archive)

    async def _offline():
        return False

    monkeypatch.setattr(mcp_tools, "_telegram_connected", _offline)

    async def never(chat_id, timeout):
        raise AssertionError("must not wait while Telegram is offline")

    fake_archive.wait_for_chat = never
    status = await agent.call("get_archive_status")
    assert status["telegram_connected"] is False
    assert "isn't connected" in status["note"]
    out = await agent.call("fetch_history", chat_id=DAVE, wait_seconds=60)
    assert out["telegram_connected"] is False and out["history_complete"] is False
    assert "unlock" in out["note"] and "Don't retry" in out["note"]
    assert out["coverage"]["message_count"] == 11
    assert fake_archive.requested == []


@pytest.mark.asyncio
async def test_fetch_history_archive_stopped_returns_immediately(agent, db, real_archive):
    await _seed(db)
    out = await agent.call("fetch_history", chat_id=DAVE, wait_seconds=60)
    assert out["archive_state"] == "stopped" and out["telegram_connected"] is True
    assert "isn't running" in out["note"]
    assert not real_archive.is_queued(DAVE)
