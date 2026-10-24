"""MCP agent workflow: DM listing, keyset paging, write gating, notes,
reviews, and what an agent learns about everything Mingly holds (history
limits per channel, email participants, Mingly ids only, accounts, timeline,
calendar, catch-up, briefings, boards, organizations, merge suggestions).

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

from backend import config
from backend.api import mcp as mcp_module
from backend.api.mcp_tools import archive as mcp_archive, common as mcp_common
from backend.api.mcp_tools import conversations as mcp_conversations
from backend.config import API_PORT
from backend.connectors.gmail.connector import GmailConnector
from backend.connectors.manager import manager
from backend.connectors.roles import ForumTopics
from backend.connectors.signal.connector import SignalConnector
from backend.connectors.sms.connector import SmsConnector
from backend.db import crud, database
from backend.db.models import (
    AgentReview, BriefingEntry, BriefingRun, CalendarAttendee, CalendarEvent, HistoryCoverage, Identity, Message,
    MessageParticipant, Person, PersonMergeSuggestion, PersonNote, PersonPosition,
)
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

    monkeypatch.setattr(mcp_common, "telegram_connected", _connected)


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
    monkeypatch.setattr(mcp_common, "get_archive", lambda: fake)
    return fake


@pytest.fixture
def app(db_engine):
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
        "conversation_id": S.ALICE, "title": "Alice", "kind": "direct", "channel": "telegram", "history_note": None,
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
    assert person["person_notes"][0]["citations"] == [{"conversation_id": S.GROUP, "message_id": group_msg}]
    dms = {d["conversation_id"]: d for d in person["dms"]}
    assert set(dms) == {S.ALICE, dm_mail.id}
    assert dms[S.ALICE]["coverage"]["message_count"] == 230
    assert dms[dm_mail.id]["channel"] == "gmail"
    groups = {g["conversation_id"]: g for g in person["groups"]}
    assert set(groups) == {S.GROUP} and person["groups_total"] == 1
    assert groups[S.GROUP] == {
        "conversation_id": S.GROUP, "title": "Builders", "kind": "group", "channel": "telegram",
        "messages_from_person": 3, "last_message_at": groups[S.GROUP]["last_message_at"],
    }
    # Email threads beyond her one-to-one ones are listed apart from groups
    [mail_thread] = person["email_threads"]
    assert (mail_thread["conversation_id"], mail_thread["channel"], mail_thread["messages_with_person"]) == (thread.id, "gmail", 1)
    assert person["email_threads_total"] == 1
    err = await agent.call_error("get_person", person_id=424242)
    assert "not found" in err

    found = await agent.call("search_people", query="alic")
    assert [p["person_id"] for p in found["people"]] == [alice] and found["total"] == 1


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
    """Other channels aren't archived on demand: their stored history is the
    complete history, answered at once without touching the archive."""
    await _seed(db)
    thread = await factories.conversation(db, "thread-1", title="Offsite", kind="email_thread", channel="gmail")
    res = await agent.call("fetch_history", conversation_id=thread.id)
    assert res["history_complete"] is True and "isn't fetched on demand" in res["note"]
    assert fake_archive.requested == []


# ── Write gating ─────────────────────────────────────────────────────────────

WRITE_CALLS = [
    ("add_person_note", lambda S: {"person_id": S.people["ALICE"], "kind": "context", "title": "t", "body": "b"}),
    ("update_person_note", lambda S: {"note_id": 1, "title": "x"}),
    ("set_person_fields", lambda S: {"person_id": S.people["ALICE"], "is_important": True}),
    ("add_position", lambda S: {"person_id": S.people["ALICE"], "organization": "Acme"}),
    ("tag_conversation", lambda S: {"conversation_id": S.ALICE, "tag_name": "vip"}),
    ("untag_conversation", lambda S: {"conversation_id": S.ALICE, "tag_name": "vip"}),
    ("update_position", lambda S: {"person_id": S.people["ALICE"], "position_id": 1, "title": "x"}),
    ("move_conversation_stage", lambda S: {"workspace_id": 1, "board_id": 1, "conversation_id": S.ALICE}),
    ("suggest_merge", lambda S: {"person_a_id": S.people["ALICE"], "person_b_id": S.people["BOB"], "reason": "x"}),
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
    assert (await db.execute(select(PersonMergeSuggestion))).first() is None
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
    assert out["person_id"] == carol and out["name"] == "Carol"
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

    monkeypatch.setattr(mcp_common, "telegram_connected", _offline)

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


# ── Channels the archive doesn't walk ────────────────────────────────────────

@pytest.mark.asyncio
async def test_dms_on_unarchived_channels_count_complete_and_can_be_reviewed(db):
    """An SMS/Signal DM has no on-demand archive: its stored history is all
    there is, so the review loop (incomplete → fetch_history → mark_reviewed)
    must be able to finish it."""
    from unittest.mock import AsyncMock

    tg = await factories.conversation(db, "77", title="Alice")
    db.add(HistoryCoverage(conversation_id=tg.id, history_complete=True))
    await db.commit()
    sms = await factories.conversation(db, "chat-guid", title="Bob", channel="sms")
    last = await factories.message(db, sms, 1_700_000_000_000, "see you", date=T0)

    archive = SimpleNamespace(refresh_status=AsyncMock(return_value={}), get_status=lambda: {"state": "idle"})
    with patch.object(mcp_common, "get_archive", return_value=archive):
        status = await mcp_archive.tool_get_archive_status(db, {}, {})
        assert status["dms"]["history_complete"] == 2 and status["dms"]["history_incomplete"] == 0
        assert (await mcp_conversations.tool_list_dms(db, {"filter": "incomplete"}, {}))["dms"] == []

        fetched = await mcp_archive.tool_fetch_history(db, {"conversation_id": sms.id}, {})
        assert fetched["history_complete"] is True and fetched["coverage"]["history_complete"] is True

        reviewed = await mcp_archive.tool_mark_reviewed(
            db, {"conversation_id": sms.id, "through_message_id": last.id}, {})
        assert reviewed["review"]["up_to_date"] is True
        unreviewed = (await mcp_conversations.tool_list_dms(db, {"filter": "unreviewed"}, {}))["dms"]
        assert [d["conversation_id"] for d in unreviewed] == [tg.id]
        assert (await mcp_archive.tool_get_archive_status(db, {}, {}))["dms"]["reviewed_up_to_date"] == 1


# ── Everything Mingly holds: history notes, participants, ids, new tools ─────

# Keys / values that must never reach an agent
_FORBIDDEN_KEYS = {"avatar_url", "external_id", "from_id", "user_ids", "sender_peer", "inviter_id",
                   "rfc822_message_id", "references", "in_reply_to", "label_ids"}


def _forbidden(obj, path="$") -> list:
    found = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in _FORBIDDEN_KEYS:
                found.append(f"{path}.{k}")
            found += _forbidden(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            found += _forbidden(v, f"{path}[{i}]")
    return found


class _Connectors:
    """Register connectors for a test (the manager is process-global)."""

    def __init__(self, *connectors):
        self.patch = patch.object(manager, "_connectors", {c.account_key: c for c in connectors})

    def __enter__(self):
        self.patch.start()

    def __exit__(self, *exc):
        self.patch.stop()


async def _other_channels(db, S):
    """A Signal group, an SMS DM and a one-to-one Gmail thread with Alice
    (her gmail identity on her person), each with a message."""
    alice = S.people["ALICE"]
    sig_bob = await factories.identity(db, "aci-bob", channel="signal", display_name="Bob S")
    sig = await factories.conversation(db, "group-1", title="Climbing crew", kind="group", channel="signal")
    sms_ident = await factories.identity(db, "+15551234567", channel="sms", display_name="Dana", phone="+15551234567")
    sms = await factories.conversation(db, "chat-1", title="Dana", channel="sms", peer=sms_ident)
    mail = await factories.identity(db, "alice@example.com", channel="gmail", display_name="Alice L")
    mail.person_id = alice
    await db.commit()
    thread = await factories.conversation(db, "thread-1", title="Quarterly plan", kind="email_thread",
                                          channel="gmail", peer=mail)
    await crud.save_messages(db, [
        _msg(sig.id, 1, sender=sig_bob, text="climbing saturday?"),
        _msg(sms.id, 1, sender=sms_ident, text="running late"),
        {**_msg(thread.id, 1, sender=mail, text="plan attached"), "subject": "Quarterly plan",
         "meta_json": json.dumps({"rfc822_message_id": "<a@x>", "references": "<z@x>", "in_reply_to": "<z@x>",
                                  "reply_to": "alice@example.com", "label_ids": ["INBOX"]})},
    ])
    mid = await _message_id(thread.id, 1)
    me_mail = await factories.identity(db, "me@example.com", channel="gmail", display_name="Me", is_self=True)
    cc = await factories.identity(db, "carol@example.com", channel="gmail", display_name="Carol")
    db.add_all([
        MessageParticipant(message_id=mid, identity_id=mail.id, role="from"),
        MessageParticipant(message_id=mid, identity_id=me_mail.id, role="to"),
        MessageParticipant(message_id=mid, identity_id=cc.id, role="cc"),
    ])
    await db.commit()
    return sig, sms, thread


def _connectors_for(sig, sms, thread):
    gmail = GmailConnector(thread.account_id)
    sms_c = SmsConnector()
    sms_c.account_id = sms.account_id
    return _Connectors(SignalConnector(sig.account_id), sms_c, gmail)


# ── History: what each channel holds ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_channel_says_how_far_its_history_goes(agent, db):
    S = await _seed(db)
    sig, sms, thread = await _other_channels(db, S)
    with _connectors_for(sig, sms, thread):
        convs = {c["conversation_id"]: c for c in (await agent.call("list_conversations", sort_by="stable", limit=100))["conversations"]}
        assert "linked" in convs[sig.id]["history_note"]
        assert "days of Messages history" in convs[sms.id]["history_note"]
        assert "days before this account was connected" in convs[thread.id]["history_note"]
        assert convs[S.ALICE]["history_note"] is None  # Telegram: archived on demand
        for cid in (sig.id, sms.id, thread.id):
            assert convs[cid]["coverage"]["fetchable"] is False and convs[cid]["coverage"]["history_complete"] is True
        assert convs[S.ALICE]["coverage"]["fetchable"] is True

        fetched = await agent.call("fetch_history", conversation_id=sig.id)
        assert fetched["history_complete"] is True and "linked" in fetched["history_note"]
        assert "linked" in fetched["note"] and "complete history" not in fetched["note"]
        page = await agent.call("get_messages", conversation_id=thread.id, order="asc")
        assert "days before" in page["conversation"]["history_note"]

    # Nothing tells an agent another channel's history is complete
    init = await agent.rpc("tools/list")
    texts = [t["description"] for t in init["result"]["tools"]]
    status = await agent.call("get_archive_status")
    for text in texts + [status["note"]]:
        assert "store all the history" not in text and "complete history" not in text


# ── People, notes, review queue ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_person_keeps_the_users_notes_and_lists_person_notes(agent, db, writes_on):
    S = await _seed(db)
    await _other_channels(db, S)
    alice = S.people["ALICE"]
    person_row = await db.get(Person, alice)
    person_row.notes = "met at KBW, intro via Bob"
    await db.commit()
    await agent.call("add_person_note", person_id=alice, kind="context", title="Investor", body="angel")

    person = await agent.call("get_person", person_id=alice)
    assert person["notes"] == "met at KBW, intro via Bob"
    assert [n["title"] for n in person["person_notes"]] == ["Investor"] and person["person_notes_count"] == 1
    addresses = {i["channel"]: i["address"] for i in person["identities"]}
    assert addresses == {"telegram": None, "gmail": "alice@example.com"}
    assert _forbidden(person) == []

    found = await agent.call("search_people", query="alice@example")
    assert [p["person_id"] for p in found["people"]] == [alice]
    assert _forbidden(found) == []
    dm = next(d for d in (await agent.call("list_dms"))["dms"] if d["conversation_id"] == S.ALICE)
    assert dm["person"]["has_notes"] is True and dm["person"]["person_notes_count"] == 1


@pytest.mark.asyncio
async def test_one_to_one_email_threads_are_dms_to_review(agent, db, writes_on):
    S = await _seed(db)
    sig, sms, thread = await _other_channels(db, S)
    dms = {d["conversation_id"]: d for d in (await agent.call("list_dms", limit=100))["dms"]}
    # The review counts cover the same DMs as the queue
    assert (await agent.call("get_archive_status"))["dms"]["total_people"] == len(dms)
    assert thread.id in dms and sms.id in dms and sig.id not in dms
    assert dms[thread.id]["kind"] == "email_thread" and dms[thread.id]["person"]["person_id"] == S.people["ALICE"]
    unreviewed = {d["conversation_id"] for d in (await agent.call("list_dms", filter="unreviewed", limit=100))["dms"]}
    assert thread.id in unreviewed
    await agent.call("mark_reviewed", conversation_id=thread.id, through_message_id=await _message_id(thread.id, 1))
    unreviewed = {d["conversation_id"] for d in (await agent.call("list_dms", filter="unreviewed", limit=100))["dms"]}
    assert thread.id not in unreviewed


# ── Messages: participants, translated meta, search context ──────────────────

@pytest.mark.asyncio
async def test_messages_carry_participants_and_mingly_ids_only(agent, db):
    S = await _seed(db)
    sig, sms, thread = await _other_channels(db, S)
    alice, bob = S.idents["ALICE"], S.idents["BOB"]
    meta_rows = [
        {**_msg(S.GROUP, 10, sender=alice, text="fwd"), "meta_json": json.dumps(
            {"forward": {"from_id": 1002, "from_name": "Bob", "date": None, "channel_post": None, "post_author": None}})},
        {**_msg(S.GROUP, 11, text="Alice added Bob", kind="service"), "meta_json": json.dumps(
            {"service": {"action": "add_user", "user_ids": [1001, 4242424], "inviter_id": 1002}})},
        {**_msg(S.GROUP, 12, text="as the group"), "meta_json": json.dumps({"sender_peer": -100500})},
        {**_msg(S.GROUP, 13, sender=alice, text="contact"), "meta_json": json.dumps(
            {"contact": {"first_name": "Bob", "phone": "1", "user_id": 1002}})},
        {**_msg(S.GROUP, 14, text="upgraded", kind="service"), "meta_json": json.dumps(
            {"service": {"action": "migrate_to", "channel_id": -100999}})},
    ]
    await crud.save_messages(db, meta_rows)
    page = await agent.call("get_messages", conversation_id=S.GROUP, order="asc", limit=100)
    by_seq = {m["seq"]: m for m in page["messages"]}
    assert by_seq[10]["meta"]["forward"]["from_person_id"] == bob.person_id
    assert by_seq[11]["meta"]["service"]["person_ids"] == [alice.person_id]  # the unknown user is dropped
    assert by_seq[11]["meta"]["service"]["inviter_person_id"] == bob.person_id
    assert by_seq[12]["meta"] == {"sent_as_conversation_id": S.GROUP}
    assert by_seq[13]["meta"]["contact"]["contact_person_id"] == bob.person_id
    assert by_seq[14]["meta"]["service"] == {"action": "migrate_to"}  # the new group isn't stored
    assert _forbidden(page) == []

    mail = (await agent.call("get_messages", conversation_id=thread.id))["messages"][0]
    assert mail["subject"] == "Quarterly plan"
    assert [(p["role"], p["address"]) for p in mail["participants"]] == [
        ("from", "alice@example.com"), ("to", "me@example.com"), ("cc", "carol@example.com"),
    ]
    assert mail["meta"] == {"reply_to": "alice@example.com"}
    assert _forbidden(mail) == []

    hits = (await agent.call("search_messages", query="climbing"))["messages"]
    assert [(h["conversation_kind"], h["channel"], h["conversation_title"]) for h in hits] == [
        ("group", "signal", "Climbing crew")]
    assert (await agent.call("search_messages", query="plan", kind="email_thread"))["total"] == 1
    assert (await agent.call("search_messages", query="hello", sender_person_id=bob.person_id))["total"] == 0
    assert (await agent.call("search_messages", query="hello 1",
                             since=(T0 + timedelta(minutes=100)).isoformat()))["total"] > 0
    assert (await agent.call("search_messages", query="hello", until="2020-01-01"))["total"] == 0
    err = await agent.call_error("search_messages", query="x", since="last week")
    assert "ISO date" in err


@pytest.mark.asyncio
async def test_no_tool_output_leaks_urls_or_channel_ids(agent, db):
    S = await _seed(db)
    await _other_channels(db, S)
    outputs = [
        await agent.call("list_dms", limit=100),
        await agent.call("list_conversations", limit=100),
        await agent.call("get_person", person_id=S.people["ALICE"]),
        await agent.call("search_people"),
        await agent.call("get_messages", conversation_id=S.ALICE, limit=100),
        await agent.call("search_messages", query="hello"),
        await agent.call("get_person_timeline", person_id=S.people["ALICE"]),
    ]
    assert [p for out in outputs for p in _forbidden(out)] == []


# ── Setup & derived views ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_accounts_shows_channels_notes_and_user_orgs(agent, db):
    S = await _seed(db)
    sig, sms, thread = await _other_channels(db, S)
    (config.DATA_DIR / "signal" / "data").mkdir(parents=True, exist_ok=True)
    from backend.user_orgs import set_user_orgs
    await set_user_orgs(db, ["Acme"])
    with _connectors_for(sig, sms, thread):
        out = await agent.call("list_accounts")
    by_channel = {a["channel"]: a for a in out["accounts"]}
    assert "linked" in by_channel["signal"]["history_note"]
    assert by_channel["signal"]["account_id"] == sig.account_id and by_channel["signal"]["status"]["state"]
    assert [o["name"] for o in out["user_organizations"]] == ["Acme"]


@pytest.mark.asyncio
async def test_person_timeline_spans_channels_and_meetings(agent, db):
    S = await _seed(db)
    sig, sms, thread = await _other_channels(db, S)
    alice = S.people["ALICE"]
    ev = CalendarEvent(account_id=thread.account_id, external_id="ev1", title="Coffee", start_at=T0 + timedelta(hours=3),
                       end_at=T0 + timedelta(hours=4))
    db.add(ev)
    await db.commit()
    mail_ident = (await db.execute(select(Identity).where(Identity.external_id == "alice@example.com"))).scalar_one()
    db.add(CalendarAttendee(event_id=ev.id, identity_id=mail_ident.id, response_status="accepted"))
    await db.commit()

    items, cursor = [], None
    while True:
        page = await agent.call("get_person_timeline", person_id=alice, limit=100, **({"cursor": cursor} if cursor else {}))
        items += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    kinds = {(i["kind"], (i.get("conversation") or {}).get("channel")) for i in items}
    assert ("meeting", None) in kinds
    assert ("message", "telegram") in kinds and ("message", "gmail") in kinds
    group_posts = [i for i in items if i["kind"] == "message" and i["conversation"]["conversation_id"] == S.GROUP]
    assert len(group_posts) == 3
    assert len([i for i in items if i["kind"] == "message"]) == 230 + 3 + 1

    events = (await agent.call("list_calendar_events", start=T0.isoformat(), end=(T0 + timedelta(days=1)).isoformat(),
                               person_id=alice))["events"]
    assert [e["title"] for e in events] == ["Coffee"] and events[0]["attendees"][0]["person_id"] == alice
    none = await agent.call("list_calendar_events", start=T0.isoformat(), end=(T0 + timedelta(days=1)).isoformat(),
                            person_id=S.people["BOB"])
    assert none["events"] == []
    assert "after start" in await agent.call_error("list_calendar_events", start=T0.isoformat(), end=T0.isoformat())


@pytest.mark.asyncio
async def test_catch_up_and_briefings(agent, db):
    S = await _seed(db)
    conv = await crud.get_conversation(db, S.ALICE)
    conv.is_tracked = True
    run = BriefingRun(generated_at=T0, days=1, status="done", finished_at=T0)
    db.add(run)
    await db.commit()
    group_msg = await _message_id(S.GROUP, 1)
    db.add(BriefingEntry(run_id=run.id, conversation_id=S.GROUP, conversation_title="Builders", category="tracked",
                         summary="Builders said gm", sources_json=json.dumps([{"conversation_id": S.GROUP, "message_id": group_msg}])))
    await db.commit()

    catch_up = await agent.call("get_catch_up")
    waiting = [e["conversation"]["conversation_id"] for e in catch_up["needs_reply"]]
    assert S.ALICE in waiting
    assert _forbidden(catch_up) == []

    assert [b["briefing_id"] for b in (await agent.call("list_briefings"))["briefings"]] == [run.id]
    latest = (await agent.call("get_briefing"))["briefing"]
    assert latest["briefing_id"] == run.id
    assert latest["entries"][0]["sources"] == [{"conversation_id": S.GROUP, "message_id": group_msg}]
    assert "not found" in await agent.call_error("get_briefing", briefing_id=999)


@pytest.mark.asyncio
async def test_workspace_board_read_and_move(agent, db, writes_on, _quiet_ws):
    S = await _seed(db)
    tag, _ = await crud.find_or_create_tag(db, "Deals")
    await crud.add_tag_to_conversation(db, S.ALICE, tag.id)
    ws = await crud.create_workspace(db, name="Pipeline", emoji=None, color="indigo", tag_ids=[tag.id], view_mode="kanban")
    board_id = (await crud.get_workspace_boards(db, ws.id))[0][0].id
    for name in ("Contacted", "Negotiating"):
        await crud.add_board_column(db, ws.id, board_id, name, None)

    listed = (await agent.call("list_workspaces"))["workspaces"]
    assert [(w["workspace_id"], w["name"], w["member_count"]) for w in listed] == [(ws.id, "Pipeline", 1)]
    board = await agent.call("get_workspace_board", workspace_id=ws.id)
    assert [s["stage"] for s in board["stages"]] == ["Contacted", "Negotiating"]

    moved = await agent.call("move_conversation_stage", workspace_id=ws.id, board_id=board_id,
                             conversation_id=S.ALICE, stage="  negotiating ")
    assert moved["stage"] == "Negotiating"
    board = await agent.call("get_workspace_board", workspace_id=ws.id, board_id=board_id)
    stages = {s["stage"]: [c["conversation_id"] for c in s["conversations"]] for s in board["stages"]}
    assert stages == {"Contacted": [], "Negotiating": [S.ALICE]}
    assert {"type": "conversations_synced", "conversation_ids": [S.ALICE]} in _quiet_ws
    err = await agent.call_error("move_conversation_stage", workspace_id=ws.id, board_id=board_id,
                                 conversation_id=S.ALICE, stage="Won")
    assert "No stage named" in err and "Negotiating" in err
    # A stage tag can't be removed as a plain tag
    assert "move_conversation_stage" in await agent.call_error("untag_conversation", conversation_id=S.ALICE,
                                                              tag_name="Negotiating")
    await agent.call("move_conversation_stage", workspace_id=ws.id, board_id=board_id, conversation_id=S.ALICE)
    board = await agent.call("get_workspace_board", workspace_id=ws.id, board_id=board_id)
    assert all(s["conversations"] == [] for s in board["stages"])


@pytest.mark.asyncio
async def test_untag_positions_organizations_and_merge_suggestions(agent, db, writes_on):
    S = await _seed(db)
    alice, bob = S.people["ALICE"], S.people["BOB"]
    await agent.call("tag_conversation", conversation_id=S.ALICE, tag_name="vip")
    out = await agent.call("untag_conversation", conversation_id=S.ALICE, tag_name="VIP")
    assert out["removed"] is True
    assert (await agent.call("untag_conversation", conversation_id=S.ALICE, tag_name="vip"))["removed"] is False
    assert "No tag named" in await agent.call_error("untag_conversation", conversation_id=S.ALICE, tag_name="nope")

    pos = await agent.call("add_position", person_id=alice, organization="Acme", title="CTO")
    org_id = pos["organization_id"]
    ended = await agent.call("update_position", person_id=alice, position_id=pos["id"], is_active=False,
                             ended_at="2026-06-01")
    assert ended["is_active"] is False and ended["ended_at"].startswith("2026-06-01")
    assert "is not one of" in await agent.call_error("update_position", person_id=bob, position_id=pos["id"], title="x")
    org = await agent.call("get_organization", organization_id=org_id)
    assert org["members"] == [] and [m["person_id"] for m in org["former_members"]] == [alice]
    assert "not found" in await agent.call_error("get_organization", organization_id=99999)

    made = await agent.call("suggest_merge", person_a_id=alice, person_b_id=bob, reason="same phone")
    assert made["created"] is True
    again = await agent.call("suggest_merge", person_a_id=bob, person_b_id=alice, reason="again")
    assert again["created"] is False and again["suggestion_id"] == made["suggestion_id"]
    listed = (await agent.call("list_merge_suggestions", person_id=bob))["suggestions"]
    assert [(s["person_a"]["person_id"], s["person_b"]["person_id"]) for s in listed] == [(alice, bob)]
    assert "agent: same phone" in listed[0]["reason"]
    assert "same person" in await agent.call_error("suggest_merge", person_a_id=alice, person_b_id=alice, reason="x")
    assert (await db.execute(select(PersonMergeSuggestion))).scalars().all()[0].status == "pending"
    assert (await db.execute(select(PersonPosition))).scalars().all()[0].is_active is False


class _FakeForum(ForumTopics):
    channel = "telegram"
    account_id = None
    multi_account = False
    account_key = "telegram"

    def __init__(self, fail=False):
        self.fail = fail

    async def list_topics(self, conversation):
        if self.fail:
            raise ConnectionError("offline")
        return [{"id": 1, "title": "General", "date": None}, {"id": 7, "title": "Launch", "date": None,
                                                                "is_closed": True}]

    async def create_topic(self, conversation, title):
        return None

    async def set_topic_closed(self, conversation, topic_id, closed):
        return None


@pytest.mark.asyncio
async def test_forum_topics(agent, db):
    S = await _seed(db)
    forum = await factories.conversation(db, -100777, title="DAO", kind="group", is_forum=True)
    await crud.save_messages(db, [{**_msg(forum.id, 1, text="hi"), "topic_id": 7}])
    with _Connectors(_FakeForum()):
        out = await agent.call("list_topics", conversation_id=forum.id)
    assert out["source"] == "channel" and [(t["topic_id"], t["title"]) for t in out["topics"]] == [(1, "General"), (7, "Launch")]
    with _Connectors(_FakeForum(fail=True)):
        out = await agent.call("list_topics", conversation_id=forum.id)
    assert out["source"] == "stored_messages" and [t["topic_id"] for t in out["topics"]] == [7]
    assert "not a forum" in await agent.call_error("list_topics", conversation_id=S.GROUP)
    conv = (await agent.call("get_conversation", conversation_id=forum.id))
    assert conv["is_forum"] is True
