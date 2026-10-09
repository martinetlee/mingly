"""MCP agent workflow: DM listing, keyset paging, write gating, notes, reviews.

Runs every tool over the real MCP HTTP endpoint against a temp DB, including
a full "review everyone I've DMed" agent simulation that must read exactly
the stored messages of every non-bot DM.
"""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from backend.api import mcp as mcp_module
from backend.api import mcp_tools
from backend.config import API_PORT
from backend.db import crud, database
from backend.db.models import AgentReview, HistoryCoverage, Identity, Message, Person, PersonNote
from tests import factories

BASE_URL = f"http://127.0.0.1:{API_PORT}"
KEY = "test-mcp-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
T0 = datetime(2025, 1, 1)


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


def _msg(conv_id, seq, *, sender=None, text=None, kind="message", minutes=None, outgoing=False):
    """A save_messages row; ``sender`` is an Identity (None = unknown)."""
    return dict(
        conversation_id=conv_id, external_id=str(seq), seq=seq,
        sender_identity_id=sender.id if sender is not None else None,
        sender_name=(sender.display_name if sender is not None else None),
        text=text if text is not None else f"msg {seq} in {conv_id}",
        date=T0 + timedelta(minutes=minutes if minutes is not None else seq), is_outgoing=outgoing, kind=kind,
    )


class FakeArchive:
    """Stands in for backend.telegram.archive: a "walk" stores the
    conversation's missing older messages and marks its history complete."""

    def __init__(self):
        self.pending = {}  # conversation_id -> rows the walk will store
        self.requested = []

    def get_status(self):
        return {"state": "idle", "flood_wait_until": None, "dms": {"total": 0}}

    async def refresh_status(self):
        return self.get_status()

    async def request_conversation(self, conversation_id, priority=True):
        self.requested.append(conversation_id)

    async def wait_for_conversation(self, conversation_id, timeout):
        async with database.AsyncSessionLocal() as s:
            await crud.save_messages(s, self.pending.pop(conversation_id, []))
            cov = await crud.get_or_create_coverage(s, conversation_id)
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


async def _seed(db, fake_archive=None) -> SimpleNamespace:
    """Seven direct conversations + one group (conversation ids on the result):
    ALICE  complete, 230 messages (incl. service rows, outgoing)
    BOB    complete, archived
    CAROL  complete, hidden
    BOT    complete bot (excluded unless include_bots)
    DAVE   incomplete: only seqs 50-60 stored; the walk adds 1-49
    ERIN   complete, no messages at all
    SELF   notes to self (never listed)
    GROUP  group where ALICE posted
    ``people`` maps those names to person ids, ``idents`` to identities.
    """
    me = await factories.identity(db, 9999, display_name="Me", is_self=True)
    idents = {
        "ALICE": await factories.identity(db, 1001, display_name="Alice", username="alice"),
        "BOB": await factories.identity(db, 1002, display_name="Bob"),
        "CAROL": await factories.identity(db, 1003, display_name="Carol"),
        "BOT": await factories.identity(db, 1004, display_name="Helper Bot", is_bot=True),
        "DAVE": await factories.identity(db, 1005, display_name="Dave"),
        "ERIN": await factories.identity(db, 1006, display_name="Erin"),
    }
    flags = {"BOB": {"is_archived": True}, "CAROL": {"is_hidden": True}, "BOT": {"is_bot": True}}
    S = SimpleNamespace(me=me, idents=idents, people={k: v.person_id for k, v in idents.items()})
    for name, ident in idents.items():
        conv = await factories.conversation(
            db, ident.external_id, title=ident.display_name, peer=ident, **flags.get(name, {}),
        )
        setattr(S, name, conv.id)
    S.SELF = (await factories.conversation(db, 9999, title="Saved Messages", peer=me, is_self=True)).id
    S.GROUP = (await factories.conversation(db, -100500, title="Builders", kind="group")).id

    alice = idents["ALICE"]
    rows = []
    for i in range(1, 231):
        kind = "service" if i % 50 == 0 else "message"
        rows.append(_msg(S.ALICE, i, kind=kind, outgoing=(i % 3 == 0), text=f"hello {i}",
                         sender=(me if i % 3 == 0 else alice)))
    rows += [_msg(S.BOB, i, sender=idents["BOB"]) for i in range(1, 4)]
    rows += [_msg(S.CAROL, i, sender=idents["CAROL"]) for i in range(1, 6)]
    rows += [_msg(S.BOT, i, sender=idents["BOT"]) for i in range(1, 4)]
    rows += [_msg(S.DAVE, i, sender=idents["DAVE"]) for i in range(50, 61)]
    rows += [_msg(S.GROUP, i, sender=alice, text="gm builders") for i in range(1, 4)]
    await crud.save_messages(db, rows)
    for cid in (S.ALICE, S.BOB, S.CAROL, S.BOT, S.ERIN, S.GROUP):
        db.add(HistoryCoverage(conversation_id=cid, history_complete=True))
    db.add(HistoryCoverage(conversation_id=S.DAVE, history_complete=False, walk_cursor_seq=50))
    await db.commit()
    if fake_archive is not None:
        fake_archive.pending[S.DAVE] = [_msg(S.DAVE, i, sender=idents["DAVE"]) for i in range(1, 50)]
    return S


async def _all_pages(agent, tool, list_key, **args):
    items, cursor, pages = [], None, 0
    while True:
        page = await agent.call(tool, **args, **({"cursor": cursor} if cursor else {}))
        items += page[list_key]
        pages += 1
        cursor = page["next_cursor"]
        if not cursor:
            return items, pages


async def _stored_seqs(conversation_id):
    async with database.AsyncSessionLocal() as s:
        return sorted((await s.execute(
            select(Message.seq).where(Message.conversation_id == conversation_id)
        )).scalars().all())


async def _message_id(conversation_id, seq):
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(
            select(Message.id).where(Message.conversation_id == conversation_id, Message.seq == seq)
        )).scalar_one()


# ── Completeness proof ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_reviews_every_dm_and_reads_every_message(agent, db, fake_archive, writes_on):
    S = await _seed(db, fake_archive)

    status = await agent.call("get_archive_status")
    assert status["dms"]["total_people"] == 5  # ALICE BOB CAROL DAVE ERIN (bot, self excluded)
    assert status["dms"]["bots_excluded"] == 1
    assert status["dms"]["history_incomplete"] == 1

    incomplete, _ = await _all_pages(agent, "list_dms", "dms", filter="incomplete")
    assert [d["conversation_id"] for d in incomplete] == [S.DAVE]

    read = {}
    rounds = 0
    while True:
        rounds += 1
        assert rounds < 10, "review loop did not converge"
        queue, _ = await _all_pages(agent, "list_dms", "dms", filter="unreviewed", limit=2)
        if not queue:
            break
        for dm in queue:
            cid = dm["conversation_id"]
            if not dm["coverage"]["history_complete"]:
                fetched = await agent.call("fetch_history", conversation_id=cid, wait_seconds=5)
                assert fetched["history_complete"] is True
                # mark_reviewed is refused until then; read again from the start
            msgs, _ = await _all_pages(agent, "get_messages", "messages", conversation_id=cid, order="asc", limit=37)
            seqs = [m["seq"] for m in msgs]
            assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
            assert all(m["conversation_id"] == cid for m in msgs)
            read[cid] = seqs
            citations = [{"conversation_id": cid, "message_id": msgs[0]["message_id"]}] if msgs else []
            await agent.call("add_person_note", person_id=dm["person"]["person_id"], kind="context",
                             title=f"About {dm['person']['name']}", body="Summary.", citations=citations)
            through = {"through_message_id": msgs[-1]["message_id"]} if msgs else {}
            await agent.call("mark_reviewed", conversation_id=cid, **through)

    expected = {c: await _stored_seqs(c) for c in (S.ALICE, S.BOB, S.CAROL, S.DAVE, S.ERIN)}
    assert read == expected
    assert len(read[S.ALICE]) == 230 and len(read[S.DAVE]) == 60 and read[S.ERIN] == []
    assert S.BOT not in read and S.SELF not in read and S.GROUP not in read

    status = await agent.call("get_archive_status")
    assert status["dms"]["unreviewed"] == 0
    assert status["dms"]["reviewed_up_to_date"] == 5

    # Every message kind came through, service rows included
    msgs, _ = await _all_pages(agent, "get_messages", "messages", conversation_id=S.ALICE, order="asc", limit=100)
    assert {m["kind"] for m in msgs} == {"message", "service"}
    assert msgs[0]["sender"]["person_id"] == S.people["ALICE"]

    # A new incoming message puts exactly that DM back in the queue …
    await crud.save_messages(db, [_msg(S.BOB, 4, sender=S.idents["BOB"])])
    queue, _ = await _all_pages(agent, "list_dms", "dms", filter="unreviewed")
    assert [d["conversation_id"] for d in queue] == [S.BOB]
    through = queue[0]["review"]["reviewed_through_message_id"]
    assert through == await _message_id(S.BOB, 3)
    # … and resuming after the reviewed point reads only the new one.
    new = await agent.call("get_messages", conversation_id=S.BOB, order="asc", after_message_id=through)
    assert [m["seq"] for m in new["messages"]] == [4]
    await agent.call("mark_reviewed", conversation_id=S.BOB, through_message_id=new["messages"][0]["message_id"])

    # An older message appearing after the review (backfill) also resurfaces the DM.
    await crud.save_messages(db, [_msg(S.CAROL, 0, minutes=-5)])
    queue, _ = await _all_pages(agent, "list_dms", "dms", filter="unreviewed")
    assert [d["conversation_id"] for d in queue] == [S.CAROL]

    # Agent notes landed on each DM's person with author/label and citations
    notes = (await db.execute(select(PersonNote).order_by(PersonNote.id))).scalars().all()
    assert len(notes) == 5
    assert {n.person_id for n in notes} == {S.people[k] for k in ("ALICE", "BOB", "CAROL", "DAVE", "ERIN")}
    assert {n.author for n in notes} == {"agent"}
    assert {n.agent_label for n in notes} == {"test-agent"}


@pytest.mark.asyncio
async def test_list_dms_flags_and_filters(agent, db):
    S = await _seed(db)
    dms, _ = await _all_pages(agent, "list_dms", "dms")
    by_id = {d["conversation_id"]: d for d in dms}
    assert set(by_id) == {S.ALICE, S.BOB, S.CAROL, S.DAVE, S.ERIN}
    assert by_id[S.BOB]["is_archived"] is True
    assert by_id[S.CAROL]["is_hidden"] is True
    assert by_id[S.ALICE]["channel"] == "telegram" and by_id[S.ALICE]["kind"] == "direct"
    assert by_id[S.ALICE]["coverage"]["message_count"] == 230
    assert by_id[S.ALICE]["coverage"]["newest_message_id"] == await _message_id(S.ALICE, 230)
    person = by_id[S.ALICE]["person"]
    assert person["person_id"] == S.people["ALICE"] and person["name"] == "Alice"
    assert [(i["channel"], i["username"]) for i in person["identities"]] == [("telegram", "alice")]
    assert by_id[S.ALICE]["review"]["up_to_date"] is False

    with_bots, _ = await _all_pages(agent, "list_dms", "dms", include_bots=True)
    assert {d["conversation_id"] for d in with_bots} == set(by_id) | {S.BOT}
    assert next(d for d in with_bots if d["conversation_id"] == S.BOT)["is_bot"] is True

    no_archived, _ = await _all_pages(agent, "list_dms", "dms", include_archived=False)
    assert S.BOB not in {d["conversation_id"] for d in no_archived}

    other_channel, _ = await _all_pages(agent, "list_dms", "dms", channel="gmail")
    assert other_channel == []


@pytest.mark.asyncio
async def test_mark_reviewed_refused_on_incomplete_history(agent, db, writes_on):
    S = await _seed(db)
    err = await agent.call_error(
        "mark_reviewed", conversation_id=S.DAVE, through_message_id=await _message_id(S.DAVE, 60),
    )
    assert "fetch_history" in err
    assert (await db.get(AgentReview, S.DAVE)) is None


@pytest.mark.asyncio
async def test_mark_reviewed_rejects_unstored_message(agent, db, writes_on):
    S = await _seed(db)
    err = await agent.call_error("mark_reviewed", conversation_id=S.BOB, through_message_id=999999)
    assert "not stored" in err
    # A message of another conversation is not "stored here"
    err = await agent.call_error(
        "mark_reviewed", conversation_id=S.BOB, through_message_id=await _message_id(S.ALICE, 1),
    )
    assert "not stored" in err
    err = await agent.call_error("mark_reviewed", conversation_id=S.BOB)
    assert "empty conversation" in err
    # An empty conversation is reviewed without a message
    out = await agent.call("mark_reviewed", conversation_id=S.ERIN)
    assert out["review"]["up_to_date"] is True and out["review"]["reviewed_through_message_id"] is None


# ── Keyset paging ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_dms_paging_stable_under_concurrent_inserts(agent, db):
    S = await _seed(db)
    seen, added, cursor, n = [], [], None, 0
    while True:
        page = await agent.call("list_dms", limit=2, **({"cursor": cursor} if cursor else {}))
        seen += [d["conversation_id"] for d in page["dms"]]
        # New DMs and fresh activity on already-listed DMs between pages
        n += 1
        added.append((await factories.conversation(db, 5000 + n, title=f"New {n}")).id)
        await crud.save_messages(db, [_msg(S.ALICE, 1000 + n)])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen))
    assert {S.ALICE, S.BOB, S.CAROL, S.DAVE, S.ERIN} <= set(seen)
    assert set(added) & set(seen)  # DMs added while paging still show up


@pytest.mark.asyncio
async def test_get_messages_asc_paging_sees_new_messages_once(agent, db):
    S = await _seed(db)
    seen, cursor, extra = [], None, 300
    while True:
        page = await agent.call("get_messages", conversation_id=S.ALICE, order="asc", limit=40,
                                **({"cursor": cursor} if cursor else {}))
        seen += [m["seq"] for m in page["messages"]]
        extra += 1
        if extra <= 303:
            await crud.save_messages(db, [_msg(S.ALICE, extra)])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == await _stored_seqs(S.ALICE)


@pytest.mark.asyncio
async def test_get_messages_desc_default_and_cursor_scope(agent, db):
    S = await _seed(db)
    page = await agent.call("get_messages", conversation_id=S.ALICE, limit=5)
    assert [m["seq"] for m in page["messages"]] == [230, 229, 228, 227, 226]
    assert page["conversation"] == {
        "conversation_id": S.ALICE, "title": "Alice", "kind": "direct", "channel": "telegram",
    }
    nxt = await agent.call("get_messages", conversation_id=S.ALICE, limit=5, cursor=page["next_cursor"])
    assert nxt["messages"][0]["seq"] == 225
    # A cursor is bound to its conversation and order
    err = await agent.call_error("get_messages", conversation_id=S.ALICE, order="asc", cursor=page["next_cursor"])
    assert "Invalid cursor" in err
    err = await agent.call_error("get_messages", conversation_id=S.BOB, cursor=page["next_cursor"])
    assert "Invalid cursor" in err
    err = await agent.call_error("get_messages", conversation_id=S.ALICE, cursor="garbage!!")
    assert "Invalid cursor" in err
    err = await agent.call_error("get_messages", conversation_id=S.ALICE, order="asc", after_message_id=999999)
    assert "not stored" in err


@pytest.mark.asyncio
async def test_get_messages_incomplete_history_points_to_fetch(agent, db):
    S = await _seed(db)
    page = await agent.call("get_messages", conversation_id=S.DAVE, order="asc")
    assert page["history_complete"] is False
    assert "fetch_history" in page["note"]


@pytest.mark.asyncio
async def test_search_messages_pages_past_100(agent, db):
    S = await _seed(db)
    hits, pages = await _all_pages(agent, "search_messages", "messages", query="hello", limit=100)
    assert pages == 3
    keys = [(h["conversation_id"], h["message_id"]) for h in hits]
    assert len(keys) == len(set(keys)) == 230
    assert hits[0]["conversation_title"] == "Alice" and hits[0]["channel"] == "telegram"
    first = await agent.call("search_messages", query="hello", limit=10)
    assert first["total"] == 230
    in_conv, _ = await _all_pages(agent, "search_messages", "messages", query="gm", conversation_id=S.GROUP)
    assert len(in_conv) == 3
    elsewhere, _ = await _all_pages(agent, "search_messages", "messages", query="hello", channel="gmail")
    assert elsewhere == []


@pytest.mark.asyncio
async def test_list_conversations_kind_filter_and_stable_sort(agent, db):
    S = await _seed(db)
    convs, _ = await _all_pages(agent, "list_conversations", "conversations", sort_by="stable", limit=3)
    ids = [c["conversation_id"] for c in convs]
    assert len(ids) == len(set(ids)) == 8  # all conversations, hidden/archived/self included
    groups, _ = await _all_pages(agent, "list_conversations", "conversations", kind="group")
    assert [c["conversation_id"] for c in groups] == [S.GROUP]
    assert groups[0]["coverage"]["message_count"] == 3
    recent, _ = await _all_pages(agent, "list_conversations", "conversations", limit=3)
    assert sorted(c["conversation_id"] for c in recent) == sorted(ids)
    unarchived, _ = await _all_pages(
        agent, "list_conversations", "conversations", include_archived=False, sort_by="alpha",
    )
    assert S.BOB not in {c["conversation_id"] for c in unarchived}


# ── People ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_person_aggregates_identities_across_channels(agent, db, writes_on):
    S = await _seed(db)
    alice = S.people["ALICE"]
    # Alice also emails: a gmail identity linked to her person, a direct
    # email conversation and a group thread she posted in.
    mail = await factories.identity(db, "alice@example.com", channel="gmail", display_name="Alice L")
    await db.execute(update(Identity).where(Identity.id == mail.id).values(person_id=alice))
    await db.commit()
    dm_mail = await factories.conversation(db, "dm-1", title="Alice L", channel="gmail", peer=mail)
    thread = await factories.conversation(db, "thread-1", title="Offsite", kind="email_thread", channel="gmail")
    await crud.save_messages(db, [_msg(thread.id, 1, sender=mail, text="agenda attached")])

    group_msg = await _message_id(S.GROUP, 1)
    await agent.call("add_position", person_id=alice, organization="Acme", title="CTO")
    await agent.call("add_person_note", person_id=alice, kind="due_diligence", title="Checked",
                     body="ok", citations=[{"conversation_id": S.GROUP, "message_id": group_msg}])
    person = await agent.call("get_person", person_id=alice)
    assert person["person_id"] == alice and person["name"] == "Alice"
    assert sorted(i["channel"] for i in person["identities"]) == ["gmail", "telegram"]
    assert person["positions"][0]["organization"] == "Acme"
    assert person["organizations"][0]["name"] == "Acme"
    assert person["notes"][0]["citations"] == [{"conversation_id": S.GROUP, "message_id": group_msg}]
    dms = {d["conversation_id"]: d for d in person["dms"]}
    assert set(dms) == {S.ALICE, dm_mail.id}
    assert dms[S.ALICE]["coverage"]["message_count"] == 230
    assert dms[dm_mail.id]["channel"] == "gmail"
    groups = {g["conversation_id"]: g for g in person["groups"]}
    assert set(groups) == {S.GROUP, thread.id} and person["groups_total"] == 2
    assert groups[S.GROUP] == {
        "conversation_id": S.GROUP, "title": "Builders", "kind": "group", "channel": "telegram",
        "messages_from_person": 3, "last_message_at": groups[S.GROUP]["last_message_at"],
    }
    assert groups[thread.id]["channel"] == "gmail" and groups[thread.id]["messages_from_person"] == 1
    err = await agent.call_error("get_person", person_id=424242)
    assert "not found" in err

    found = await agent.call("search_people", query="alic")
    assert [p["id"] for p in found["people"]] == [alice] and found["total"] == 1


@pytest.mark.asyncio
async def test_fetch_history_requests_walk(agent, db, fake_archive):
    S = await _seed(db, fake_archive)
    out = await agent.call("fetch_history", conversation_id=S.DAVE, wait_seconds=1)
    assert fake_archive.requested == [S.DAVE]
    assert out["conversation_id"] == S.DAVE and out["history_complete"] is True
    assert out["coverage"]["message_count"] == 60


@pytest.mark.asyncio
async def test_fetch_history_reports_archive_failure(agent, db, fake_archive):
    S = await _seed(db)

    async def boom(conversation_id, priority=True):
        raise RuntimeError("Telegram is not connected")

    fake_archive.request_conversation = boom
    err = await agent.call_error("fetch_history", conversation_id=S.DAVE)
    assert "Telegram is not connected" in err


@pytest.mark.asyncio
async def test_fetch_history_is_telegram_only(agent, db, fake_archive):
    await _seed(db)
    thread = await factories.conversation(db, "thread-1", title="Offsite", kind="email_thread", channel="gmail")
    err = await agent.call_error("fetch_history", conversation_id=thread.id)
    assert "only Telegram" in err
    assert fake_archive.requested == []


# ── Write gating ─────────────────────────────────────────────────────────────

WRITE_CALLS = [
    ("add_person_note", lambda S: {"person_id": S.people["ALICE"], "kind": "context", "title": "t", "body": "b"}),
    ("update_person_note", lambda S: {"note_id": 1, "title": "x"}),
    ("set_person_fields", lambda S: {"person_id": S.people["ALICE"], "is_important": True}),
    ("add_position", lambda S: {"person_id": S.people["ALICE"], "organization": "Acme"}),
    ("tag_conversation", lambda S: {"conversation_id": S.ALICE, "tag_name": "vip"}),
    ("mark_reviewed", lambda S: {"conversation_id": S.BOB, "through_message_id": 1}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,args", WRITE_CALLS)
async def test_write_tools_refused_when_disabled(agent, db, name, args):
    S = await _seed(db)
    with patch("backend.api.mcp.get_mcp_write_enabled", return_value=False):
        err = await agent.call_error(name, **args(S))
    assert "turned off" in err
    assert (await db.execute(select(PersonNote))).first() is None
    assert (await db.execute(select(AgentReview))).first() is None
    assert (await db.execute(select(Person).where(Person.is_important == True))).first() is None  # noqa: E712


@pytest.mark.asyncio
async def test_write_pref_controls_gate_end_to_end(agent, db, api):
    """The real pref (Settings › MCP toggle) — default off — gates writes."""
    S = await _seed(db)
    assert (await api.get("/api/settings/prefs/mcp")).json()["write_enabled"] is False
    await agent.call_error("tag_conversation", conversation_id=S.ALICE, tag_name="vip")
    resp = await api.post("/api/settings/prefs/mcp/write", json={"enabled": True})
    assert resp.json() == {"write_enabled": True}
    assert (await api.get("/api/settings/prefs/mcp")).json()["write_enabled"] is True
    out = await agent.call("tag_conversation", conversation_id=S.ALICE, tag_name="vip")
    assert out["tag"]["name"] == "vip" and out["conversation_id"] == S.ALICE
    again = await agent.call("tag_conversation", conversation_id=S.ALICE, tag_name="VIP ")
    assert again["tag"]["id"] == out["tag"]["id"]


@pytest.mark.asyncio
async def test_citations_validated(agent, db, writes_on, _quiet_ws):
    S = await _seed(db)
    alice = S.people["ALICE"]
    err = await agent.call_error("add_person_note", person_id=alice, kind="risk", title="t", body="b",
                                 citations=[{"conversation_id": S.ALICE, "message_id": 999999}])
    assert "not found locally" in err and f"{S.ALICE}/999999" in err
    # A real message cited under the wrong conversation is refused too
    bob_msg = await _message_id(S.BOB, 1)
    err = await agent.call_error("add_person_note", person_id=alice, kind="risk", title="t", body="b",
                                 citations=[{"conversation_id": S.ALICE, "message_id": bob_msg}])
    assert "not found locally" in err
    assert (await db.execute(select(PersonNote))).first() is None

    m5 = await _message_id(S.ALICE, 5)
    note = await agent.call("add_person_note", person_id=alice, kind="risk", title="t", body="b",
                            citations=[{"conversation_id": S.ALICE, "message_id": m5},
                                       {"conversation_id": S.ALICE, "message_id": m5}])
    assert note["citations"] == [{"conversation_id": S.ALICE, "message_id": m5}]
    assert note["person_id"] == alice
    assert note["author"] == "agent" and note["agent_label"] == "test-agent"
    assert {"type": "person_notes_changed", "person_id": alice} in _quiet_ws

    err = await agent.call_error("update_person_note", note_id=note["id"],
                                 citations=[{"conversation_id": S.BOB, "message_id": 999998}])
    assert "not found locally" in err
    upd = await agent.call("update_person_note", note_id=note["id"], body="new body", citations=[])
    assert upd["body"] == "new body" and upd["citations"] == []


@pytest.mark.asyncio
async def test_agent_cannot_update_user_note(agent, db, writes_on):
    S = await _seed(db)
    db.add(PersonNote(person_id=S.people["ALICE"], kind="context", title="mine", body="", author="user"))
    await db.commit()
    note_id = (await db.execute(select(PersonNote.id))).scalar()
    err = await agent.call_error("update_person_note", note_id=note_id, title="hijack")
    assert "agent-written" in err


@pytest.mark.asyncio
async def test_add_note_unknown_person_refused(agent, db, writes_on):
    await _seed(db)
    err = await agent.call_error("add_person_note", person_id=31337, kind="context", title="t", body="b")
    assert "not found" in err


@pytest.mark.asyncio
async def test_set_person_fields(agent, db, writes_on, _quiet_ws):
    S = await _seed(db)
    carol = S.people["CAROL"]
    out = await agent.call("set_person_fields", person_id=carol, is_important=True,
                           relationship_status="nurturing", linkedin_url="linkedin.com/in/carol")
    assert out["id"] == carol and out["name"] == "Carol"
    assert out["is_important"] is True
    assert out["relationship_status"] == "nurturing" and out["relationship_status_manual"] is True
    assert out["linkedin_url"] == "https://linkedin.com/in/carol"
    assert {"type": "person_changed", "person_id": carol} in _quiet_ws
    dave = S.people["DAVE"]
    err = await agent.call_error("set_person_fields", person_id=dave, linkedin_url="https://evil.example.com")
    assert "linkedin" in err.lower()
    assert (await crud.get_person(db, dave)).linkedin_url is None
    auto = await agent.call("set_person_fields", person_id=carol, relationship_status="auto")
    assert auto["relationship_status_manual"] is False


@pytest.mark.asyncio
async def test_add_position_duplicate_is_tool_error(agent, db, writes_on):
    S = await _seed(db)
    await agent.call("add_position", person_id=S.people["ALICE"], organization="Acme")
    err = await agent.call_error("add_position", person_id=S.people["ALICE"], organization=" acme ")
    assert "Already has a position" in err


# ── Argument validation / prompts ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name,args,fragment", [
    ("list_dms", {"include_bots": "yes"}, "boolean"),
    ("list_dms", {"filter": "everything"}, "one of"),
    ("get_messages", {"conversation_id": 1, "order": "sideways"}, "one of"),
    ("get_messages", {"conversation_id": True}, "integer"),
    ("add_person_note", {"person_id": 1, "kind": "context", "title": "t", "body": "b",
                         "citations": [{"conversation_id": "x", "message_id": 1}]}, "citations[0].conversation_id"),
    ("add_person_note", {"person_id": 1, "kind": "context", "title": "t", "body": "b",
                         "citations": "nope"}, "array"),
    ("add_person_note", {"person_id": 1, "kind": "gossip", "title": "t", "body": "b"}, "one of"),
    ("add_person_note", {"person_id": 1, "kind": "context", "title": "", "body": "b"}, "empty"),
    ("fetch_history", {"conversation_id": 1, "wait_seconds": 600}, "<= 60"),
    ("list_conversations", {"offset": 5}, "Unknown argument"),
    ("list_conversations", {"kind": "private"}, "one of"),
    ("get_messages", {"conversation_id": 1, "chat_id": 1}, "Unknown argument: chat_id"),
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
        assert "tg_message_id" not in init["instructions"] and "chat_id" not in init["instructions"]
        prompts = (await a.rpc("prompts/list"))["result"]["prompts"]
        assert [p["name"] for p in prompts] == ["review_dms"]
        got = (await a.rpc("prompts/get", {"name": "review_dms", "arguments": {"focus": "investors"}}))["result"]
        text = got["messages"][0]["content"]["text"]
        assert "mark_reviewed" in text and "investors" in text
        assert (await a.rpc("prompts/get", {"name": "nope"}))["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_fetch_history_timeout_reports_progress_or_error(agent, db, fake_archive):
    S = await _seed(db)

    async def still_walking(conversation_id, timeout):
        async with database.AsyncSessionLocal() as s:
            return await s.get(HistoryCoverage, conversation_id)  # current row, not complete

    fake_archive.wait_for_conversation = still_walking
    out = await agent.call("fetch_history", conversation_id=S.DAVE, wait_seconds=1)
    assert out["history_complete"] is False and "Still archiving" in out["note"]

    cov = await db.get(HistoryCoverage, S.DAVE)
    cov.error = "FloodWait 300s"
    await db.commit()
    out = await agent.call("fetch_history", conversation_id=S.DAVE, wait_seconds=1)
    assert "FloodWait 300s" in out["note"]


# ── Against the real backend.telegram.archive (no Telegram client) ──────────

@pytest.fixture
def real_archive(monkeypatch):
    """The real DMArchive state machine with its worker disabled:
    request_conversation only queues, and the test plays the worker by
    writing coverage + resolving."""
    from backend.telegram import archive as archive_mod

    fresh = archive_mod.DMArchive()
    monkeypatch.setattr(fresh, "_ensure_worker", lambda: None)
    for name in ("get_status", "refresh_status", "request_conversation", "wait_for_conversation"):
        monkeypatch.setattr(archive_mod, name, getattr(fresh, name))
    return fresh


@pytest.mark.asyncio
async def test_real_archive_status_and_fetch_history(agent, db, real_archive):
    import asyncio

    S = await _seed(db)
    status = await agent.call("get_archive_status")
    # refresh_status recounted from the DB (get_status alone would still say 0)
    assert status["archive"]["dms"]["total"] >= 5
    assert status["archive"]["dms"]["complete"] >= 4
    assert status["archive"]["state"] == "stopped"

    real_archive.state = "running"  # worker up (stopped answers immediately; see below)
    # Times out while queued: current (incomplete) row comes back
    out = await agent.call("fetch_history", conversation_id=S.DAVE, wait_seconds=0.2)
    assert real_archive.is_queued(S.DAVE)
    assert out["history_complete"] is False and "Still archiving" in out["note"]

    async def play_worker():
        await asyncio.sleep(0.2)
        async with database.AsyncSessionLocal() as s:
            await crud.save_messages(s, [_msg(S.DAVE, i) for i in range(1, 50)])
            (await s.get(HistoryCoverage, S.DAVE)).history_complete = True
            await s.commit()
        real_archive._queue.remove(S.DAVE)
        real_archive._resolve(S.DAVE)

    worker = asyncio.ensure_future(play_worker())
    out = await agent.call("fetch_history", conversation_id=S.DAVE, wait_seconds=10)
    await worker
    assert out["history_complete"] is True
    assert out["coverage"]["message_count"] == 60


@pytest.mark.asyncio
async def test_fetch_history_offline_returns_immediately(agent, db, fake_archive, monkeypatch):
    S = await _seed(db, fake_archive)

    async def _offline():
        return False

    monkeypatch.setattr(mcp_tools, "_telegram_connected", _offline)

    async def never(conversation_id, timeout):
        raise AssertionError("must not wait while Telegram is offline")

    fake_archive.wait_for_conversation = never
    status = await agent.call("get_archive_status")
    assert status["telegram_connected"] is False
    assert "isn't connected" in status["note"]
    out = await agent.call("fetch_history", conversation_id=S.DAVE, wait_seconds=60)
    assert out["telegram_connected"] is False and out["history_complete"] is False
    assert "unlock" in out["note"] and "Don't retry" in out["note"]
    assert out["coverage"]["message_count"] == 11
    assert fake_archive.requested == []


@pytest.mark.asyncio
async def test_fetch_history_archive_stopped_returns_immediately(agent, db, real_archive):
    S = await _seed(db)
    out = await agent.call("fetch_history", conversation_id=S.DAVE, wait_seconds=60)
    assert out["archive_state"] == "stopped" and out["telegram_connected"] is True
    assert "isn't running" in out["note"]
    assert not real_archive.is_queued(S.DAVE)
