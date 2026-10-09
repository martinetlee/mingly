"""Telegram sync engine on schema v2: message conversion, upsert semantics,
keyset windows, coordination, runtime ownership, live handlers and the
compose/media entry points (reviews/APP_REVIEW_2026-10-06.md §2-A, §2-A2,
§2-C, §2-E, §3)."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from telethon.errors import FloodWaitError
from telethon.tl import types

from backend.db import crud
from backend.db.models import Conversation, Identity, Message
from backend.telegram import ids as tg_ids
from backend.telegram.convert import message_to_row, rows_for
from tests import factories as f

UTC = timezone.utc


def use_client(client):
    """Make runtime.client() return ``client`` (None = Telegram unavailable)."""
    from backend.telegram.runtime import runtime
    return patch.object(runtime, "client", AsyncMock(return_value=client))


def tg_msg(mid, text="hi", peer=77, date=None, entities=None, out=False, sender=None, **kw):
    m = types.Message(
        id=mid,
        peer_id=types.PeerUser(peer) if peer > 0 else types.PeerChannel(-peer),
        date=date or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        message=text,
        entities=entities,
        out=out,
        **kw,
    )
    m.get_sender = AsyncMock(return_value=sender)
    if sender is not None:
        m._sender = sender
        m._sender_id = sender.id
    return m


async def tg_conv(db, peer=77, **kw):
    """A Telegram conversation for ``peer`` (direct for users, group otherwise)."""
    kw.setdefault("title", "Alice")
    kw.setdefault("kind", "direct" if peer > 0 else "group")
    return await f.conversation(db, str(peer), **kw)


async def save(db, conv, *msgs, forum=None):
    rows = await rows_for(db, conv.id, list(msgs), forum=conv.is_forum if forum is None else forum)
    return await crud.save_messages(db, rows)


async def stored(db, conv, tg_id) -> Message:
    return (await db.execute(
        select(Message).where(Message.conversation_id == conv.id, Message.external_id == str(tg_id))
        .execution_options(populate_existing=True)
    )).scalar_one()


# ── message_to_row ───────────────────────────────────────────────────────────

def test_formatted_text_uses_raw_text_and_entities():
    row = message_to_row(tg_msg(1, "hello world", entities=[types.MessageEntityItalic(6, 5)]), 1)
    assert row["text"] == "hello world"
    assert row["text_html"] == "hello <em>world</em>"
    assert (row["external_id"], row["seq"], row["conversation_id"]) == ("1", 1, 1)
    row = message_to_row(tg_msg(2, "use x now", entities=[types.MessageEntityCode(4, 1)]), 1)
    assert row["text_html"] == "use <code>x</code> now"
    assert row["delivery_state"] == "sent" and row["attachments"] == []


def test_outgoing_from_other_device_is_sent_not_sending():
    assert message_to_row(tg_msg(3, "x", out=True), 1)["delivery_state"] == "sent"


def test_forum_topic_mapping():
    # plain message inside topic 10: topic membership, not a reply
    m = tg_msg(20, peer=-100, reply_to=types.MessageReplyHeader(reply_to_msg_id=10, forum_topic=True))
    row = message_to_row(m, 1, forum=True)
    assert (row["topic_id"], row["reply_to_external_id"]) == (10, None)
    # real reply inside topic 10
    m = tg_msg(21, peer=-100, reply_to=types.MessageReplyHeader(reply_to_msg_id=15, reply_to_top_id=10, forum_topic=True))
    row = message_to_row(m, 1, forum=True)
    assert (row["topic_id"], row["reply_to_external_id"]) == (10, "15")
    # no header in a forum → General
    assert message_to_row(tg_msg(22, peer=-100), 1, forum=True)["topic_id"] == 1
    assert message_to_row(tg_msg(23, peer=-100), 1, forum=False)["topic_id"] is None


def test_document_becomes_one_attachment():
    m = tg_msg(5, "file")
    m.media = types.MessageMediaDocument(document=types.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="application/pdf",
        size=3, dc_id=1, attributes=[types.DocumentAttributeFilename("a.pdf")]))
    [att] = message_to_row(m, 1)["attachments"]
    assert att == {"kind": "document", "filename": "a.pdf", "mime_type": "application/pdf", "size": 3, "external_ref": None}


def _photo():
    return types.Photo(id=1, access_hash=1, file_reference=b"", date=None, dc_id=1,
                       sizes=[types.PhotoSize(type="x", w=800, h=600, size=1234)])


def test_link_previews_and_service_photos_are_not_attachments():
    preview = tg_msg(6, "read https://example.com")
    preview.media = types.MessageMediaWebPage(webpage=types.WebPage(
        id=1, url="https://example.com", display_url="example.com", hash=0, photo=_photo()))
    assert preview.photo is not None  # Telethon exposes the preview's image…
    assert message_to_row(preview, 1)["attachments"] == []  # …but it isn't the message's file

    changed = types.MessageService(id=7, peer_id=types.PeerChannel(100), date=datetime(2026, 1, 1, tzinfo=UTC),
                                   action=types.MessageActionChatEditPhoto(photo=_photo()))
    assert changed.photo is not None
    row = message_to_row(changed, 1)
    assert row["attachments"] == [] and row["kind"] == "service"

    real = tg_msg(8, "")
    real.media = types.MessageMediaPhoto(photo=_photo())
    assert [a["kind"] for a in message_to_row(real, 1)["attachments"]] == ["photo"]


# ── identities from senders ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_senders_become_identities_and_min_users_keep_contact_flags(db):
    conv = await tg_conv(db, -100, title="Group")
    ann = types.User(id=501, first_name="Ann", last_name="Lee", phone="15550001", contact=True)
    await save(db, conv, tg_msg(1, "hi", peer=-100, sender=ann))
    ident = await crud.get_identity_by_external(db, "telegram", "501")
    assert ident.display_name == "Ann Lee" and ident.is_contact is True and ident.phone == "15550001"
    assert (await stored(db, conv, 1)).sender_identity_id == ident.id

    # Seen later as a "min" user (no access to phone/contact): those stay.
    min_ann = types.User(id=501, first_name="Ann", last_name="Lee", min=True, contact=False)
    await save(db, conv, tg_msg(2, "again", peer=-100, sender=min_ann))
    await db.refresh(ident)
    assert ident.is_contact is True and ident.phone == "15550001"

    # A channel posting as itself has no identity.
    channel = types.Channel(id=100, title="News", photo=types.ChatPhotoEmpty(), date=None)
    await save(db, conv, tg_msg(3, "post", peer=-100, sender=channel))
    row = await stored(db, conv, 3)
    assert row.sender_identity_id is None and row.sender_name == "News"
    assert len((await db.execute(select(Identity))).scalars().all()) == 1  # only Ann; groups have no peer


# ── upsert semantics ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_updates_telegram_columns_and_keeps_local_ones(db):
    conv = await tg_conv(db)
    first = message_to_row(tg_msg(1, "meet at 5", entities=[types.MessageEntityBold(0, 4)]), conv.id)
    first["reactions_json"] = '[{"emoji": "👍", "count": 1}]'
    first["is_pinned"] = True
    first["attachments"] = [f.photo()]
    res = await crud.save_messages(db, [first])
    assert len(res["created"]) == 1 and res["updated"] == []

    row = await stored(db, conv, 1)
    row.scam_score = 0.9
    row.my_reaction = "👍"
    row.delivery_state = "read"
    await db.commit()
    await crud.set_attachment_local_path(db, row.attachments[0].id, "media/1/1.jpg")

    edited = message_to_row(tg_msg(1, "meet at 6", edit_date=datetime(2026, 1, 1, 13, tzinfo=UTC)), conv.id)
    edited["attachments"] = [f.photo()]
    res = await crud.save_messages(db, [edited])
    assert res == {"created": [], "updated": [row.id]}

    row = await stored(db, conv, 1)
    assert row.text == "meet at 6"
    assert row.text_html is None          # plain-text edit clears old HTML
    assert row.reactions_json is None     # reaction removal propagates
    assert row.is_pinned is False         # unpin propagates
    assert row.is_edited is True
    assert row.scam_score == 0.9 and row.my_reaction == "👍"
    await db.refresh(row)
    assert row.attachments[0].local_path == "media/1/1.jpg"
    assert row.delivery_state == "read"   # monotonic

    # identical content → no change reported
    assert await crud.save_messages(db, [edited]) == {"created": [], "updated": []}


@pytest.mark.asyncio
async def test_save_message_backfills_topic_and_updates_preview(db):
    conv = await tg_conv(db, -100, kind="group", is_forum=True)
    saved = await crud.save_message(db, **message_to_row(tg_msg(5, "x", peer=-100), conv.id, forum=False))
    assert saved._change == "created" and saved.topic_id is None
    saved = await crud.save_message(db, **message_to_row(tg_msg(5, "x", peer=-100), conv.id, forum=True))
    assert saved._change == "updated" and saved.topic_id == 1
    await db.refresh(conv)
    assert conv.last_message_text == "x"


@pytest.mark.asyncio
async def test_concurrent_conversation_upserts_never_conflict(db_engine):
    from backend.db.database import AsyncSessionLocal

    async with AsyncSessionLocal() as s:
        acc = await f.account(s)

    async def one(i):
        async with AsyncSessionLocal() as s:
            return await crud.upsert_conversation(
                s, account_id=acc.id, external_id="555", title=f"n{i}", kind="direct", unread_count=i,
            )

    convs = await asyncio.gather(*[one(i) for i in range(5)])
    assert len({c.id for c in convs}) == 1
    async with AsyncSessionLocal() as s:
        assert len((await s.execute(select(Conversation).where(Conversation.external_id == "555"))).all()) == 1
        # optional fields keep old values when not provided
        c = await crud.upsert_conversation(s, account_id=acc.id, external_id="555", title="renamed", kind="direct")
        assert c.title == "renamed" and c.unread_count is not None


# ── keyset windows ───────────────────────────────────────────────────────────

async def seed_history(db, n=10, peer=77):
    conv = await tg_conv(db, peer)
    base = datetime(2026, 1, 1, 12, 0)
    # pairs share a timestamp (albums) → seq breaks the tie
    msgs = [
        tg_msg(i, f"m{i}", peer=peer, date=(base + timedelta(minutes=(i + 1) // 2)).replace(tzinfo=UTC))
        for i in range(1, n + 1)
    ]
    await save(db, conv, *msgs)
    return conv


def seqs(window):
    return [r[0].seq for r in window["rows"]]


@pytest.mark.asyncio
async def test_keyset_window_pages_without_gaps_or_duplicates(db):
    conv = await seed_history(db)
    w = await crud.get_message_window(db, conv.id, limit=4)
    assert seqs(w) == [7, 8, 9, 10] and w["has_older"] and not w["has_newer"]
    seen = seqs(w)
    while w["has_older"]:
        w = await crud.get_message_window(db, conv.id, limit=4, before=w["rows"][0][0].id)
        seen = seqs(w) + seen
    assert seen == list(range(1, 11))
    w2 = await crud.get_message_window(db, conv.id, limit=3, after=w["rows"][-1][0].id)
    assert seqs(w2) == [3, 4, 5] and w2["has_older"] and w2["has_newer"]


@pytest.mark.asyncio
async def test_anchor_and_date_windows_report_both_flags(db):
    conv = await seed_history(db)
    fifth = await stored(db, conv, 5)
    w = await crud.get_message_window(db, conv.id, limit=4, anchor=fifth.id)
    assert seqs(w) == [3, 4, 5, 6] and w["has_older"] and w["has_newer"] and w["anchor_found"]
    w = await crud.get_message_window(db, conv.id, limit=2, around_date=datetime(2026, 1, 1, 12, 3))
    assert seqs(w) == [4, 5]
    w = await crud.get_message_window(db, conv.id, limit=2, anchor=999999)
    assert not w["anchor_found"]


@pytest.mark.asyncio
async def test_general_topic_includes_untagged_rows(db):
    conv = await tg_conv(db, -100, kind="group", is_forum=True)
    await crud.save_messages(db, [
        {**message_to_row(tg_msg(1, "old", peer=-100), conv.id), "topic_id": None},
        message_to_row(tg_msg(2, "general", peer=-100), conv.id, forum=True),
        message_to_row(tg_msg(3, "t10", peer=-100, reply_to=types.MessageReplyHeader(reply_to_msg_id=10, forum_topic=True)), conv.id, forum=True),
    ])
    assert seqs(await crud.get_message_window(db, conv.id, topic_id=1)) == [1, 2]
    assert seqs(await crud.get_message_window(db, conv.id, topic_id=10)) == [3]


@pytest.mark.asyncio
async def test_window_endpoint_pages_by_message_id(api, db):
    conv = await seed_history(db)
    conv.last_synced_at = datetime.utcnow()  # fresh: no auto-sync
    await db.commit()
    url = f"/api/conversations/{conv.id}/messages/window"
    r = await api.get(url, params={"limit": 4})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [m["seq"] for m in body["messages"]] == [7, 8, 9, 10]
    assert body["has_older"] and not body["has_newer"] and body["syncing"] is False
    r = await api.get(url, params={"limit": 4, "before": body["messages"][0]["id"]})
    assert [m["seq"] for m in r.json()["messages"]] == [3, 4, 5, 6]
    assert (await api.get(url, params={"before": "garbage"})).status_code == 422
    assert (await api.get("/api/conversations/999999/messages/window")).status_code == 404
    # A stored message can be opened by its external id without any fetch.
    with patch("backend.api.conversations.coordinator.fetch_around", AsyncMock()) as fetch:
        r = await api.get(url, params={"around_external_id": "5", "limit": 2})
    assert [m["seq"] for m in r.json()["messages"]] == [4, 5] and not fetch.called


@pytest.mark.asyncio
async def test_window_fetches_unstored_reply_target(api, db):
    from backend.telegram.coordinator import SyncResult
    conv = await seed_history(db, n=2)
    conv.last_synced_at = datetime.utcnow()
    await db.commit()

    async def fetch(conversation_id, anchor_external_id=None, date=None, limit=50):
        async with __import__("backend.db.database", fromlist=["x"]).AsyncSessionLocal() as s:
            c = await crud.get_conversation(s, conversation_id)
            await save(s, c, tg_msg(40, "old reply target", date=datetime(2025, 12, 1, tzinfo=UTC)))
        return SyncResult("changed", conversation_id, added=1)

    with patch("backend.api.conversations.coordinator.fetch_around", side_effect=fetch) as m:
        r = await api.get(f"/api/conversations/{conv.id}/messages/window", params={"around_external_id": "40"})
    body = r.json()
    assert m.call_args.kwargs["anchor_external_id"] == "40"
    assert body["anchor_found"] and "old reply target" in [x["text"] for x in body["messages"]]


# ── coordinator ──────────────────────────────────────────────────────────────

class FakeHistoryClient:
    def __init__(self, msgs, delay=0.05, flood=None):
        self.msgs = msgs
        self.calls = []
        self.delay = delay
        self.flood = flood

    def is_connected(self):
        return True

    async def iter_messages(self, peer, limit=50, offset_id=0, **kw):
        self.calls.append((peer, limit, offset_id))
        if self.flood:
            raise FloodWaitError(request=None, capture=self.flood)
        await asyncio.sleep(self.delay)
        for m in sorted(self.msgs, key=lambda m: -m.id):
            if offset_id and m.id >= offset_id:
                continue
            yield m

    async def get_entity(self, peer):
        raise ValueError("no entity")  # pinned probe: ignored


@pytest.fixture
def coord():
    from backend.telegram.coordinator import SyncCoordinator
    return SyncCoordinator()


@pytest.mark.asyncio
async def test_concurrent_syncs_share_one_fetch(db, coord):
    conv = await tg_conv(db)
    client = FakeHistoryClient([tg_msg(i, f"m{i}") for i in range(1, 4)])
    with use_client(client):
        r1, r2 = await asyncio.gather(coord.sync_conversation(conv.id, 50), coord.sync_conversation(conv.id, 50))
    assert client.calls == [(77, 50, 0)]  # the Telegram peer, fetched once
    assert r1 is r2 and r1.status == "changed" and r1.added == 3 and r1.exhausted
    assert len(r1.added_ids) == 3
    with use_client(client):
        r3 = await coord.sync_conversation(conv.id, 50)
    assert r3.status == "unchanged"


@pytest.mark.asyncio
async def test_sync_removes_messages_deleted_on_telegram(db, coord):
    conv = await tg_conv(db)
    msgs = [tg_msg(i, f"m{i}", date=datetime(2026, 1, 1, 12, i, tzinfo=UTC)) for i in range(1, 5)]
    client = FakeHistoryClient(msgs, delay=0)
    sent = []

    async def capture(data):
        sent.append(data)

    with use_client(client), patch("backend.telegram.coordinator.ws_manager.broadcast", capture):
        await coord.sync_conversation(conv.id, 50)
        gone = (await stored(db, conv, 2)).id
        client.msgs = [m for m in msgs if m.id != 2]
        res = await coord.sync_conversation(conv.id, 50)
    assert res.deleted == 1 and res.status == "changed"
    assert {"type": "messages_deleted", "conversation_id": conv.id, "message_ids": [gone]} in sent


@pytest.mark.asyncio
async def test_flood_wait_is_typed_and_gates_other_syncs(db, coord):
    conv = await tg_conv(db)
    other = await tg_conv(db, 78, title="Bob")
    client = FakeHistoryClient([], flood=30)
    with use_client(client):
        res = await coord.sync_conversation(conv.id, 50)
        assert res.status == "error" and res.flood_wait == 30 and not res.completed
        skipped = await coord.sync_conversation(other.id, 50, max_wait=1)
    assert skipped.status == "skipped" and skipped.flood_wait > 0


@pytest.mark.asyncio
async def test_no_client_is_unavailable_not_empty(db, coord):
    conv = await tg_conv(db)
    with use_client(None):
        res = await coord.sync_conversation(conv.id, 50)
    assert res.status == "unavailable" and not res.completed


@pytest.mark.asyncio
async def test_non_telegram_conversation_is_not_found(db, coord):
    mail = await f.conversation(db, "thread-1", title="Hi", kind="email_thread", channel="gmail")
    with use_client(FakeHistoryClient([])):
        res = await coord.sync_conversation(mail.id, 50)
    assert res.status == "not_found"


@pytest.mark.asyncio
async def test_backfill_uses_oldest_local_seq(api, db):
    conv = await seed_history(db, n=3)
    from backend.telegram.coordinator import coordinator, SyncResult
    with patch.object(coordinator, "sync_conversation", AsyncMock(
        return_value=SyncResult("changed", conv.id, fetched=5, added=5, exhausted=True),
    )) as m:
        r = await api.post(f"/api/conversations/{conv.id}/backfill", params={"limit": 50})
    assert r.status_code == 200 and r.json() == {"added": 5, "exhausted": True, "status": "changed"}
    assert m.call_args.kwargs["offset_seq"] == 1


@pytest.mark.asyncio
async def test_telegram_only_routes_reject_other_channels(api, db):
    mail = await f.conversation(db, "thread-1", title="Hi", kind="email_thread", channel="gmail")
    for method, path in (
        ("post", f"/api/conversations/{mail.id}/backfill"),
        ("post", f"/api/conversations/{mail.id}/sync"),
        ("get", f"/api/conversations/{mail.id}/topics"),
        ("post", f"/api/conversations/{mail.id}/typing"),
    ):
        r = await getattr(api, method)(path)
        assert r.status_code == 409 and r.json()["code"] == "unsupported_channel", path
    r = await api.post("/api/compose/send", json={"conversation_id": mail.id, "text": "x"})
    assert r.status_code == 409 and r.json()["channel"] == "gmail"


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


@pytest.mark.asyncio
async def test_startup_records_account_owner_as_self(db):
    from backend.telegram.runtime import TelegramRuntime

    class MeClient:
        async def get_me(self):
            return types.User(id=1, first_name="Me", username="me", phone="15550000", is_self=True)

    await TelegramRuntime()._record_account(MeClient())
    acc = await tg_ids.account(db)
    await db.refresh(acc)
    assert acc.external_id == "1" and acc.label == "@me"
    me = await crud.get_identity(db, acc.self_identity_id)
    assert me.is_self and (await crud.self_person(db)).id == me.person_id


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
    from backend.ingest import fanout
    from backend.telegram import sync
    client = FakeRuntimeClient()
    handlers = {cb.__name__: cb for cb, _ in sync.register_event_handlers(client)}
    sent = []

    async def capture(data):
        sent.append(data)

    user = types.User(id=77, first_name="Ann", last_name="Lee")
    msg = tg_msg(1, "meet at 5", sender=user)
    with patch.object(fanout.ws_manager, "broadcast", capture), \
         patch.object(fanout, "enqueue_scam", AsyncMock()) as scam, \
         patch.object(fanout, "stamp_triage", AsyncMock()), \
         patch("backend.api.settings.get_notif_pref", return_value="none"):
        await handlers["on_new_message"](FakeEvent(msg, 77, user))
        edited = tg_msg(1, "meet at 6", edit_date=datetime(2026, 1, 1, 13, tzinfo=UTC), sender=user)
        await handlers["on_message_edited"](FakeEvent(edited, 77, user))

    from backend.db.database import AsyncSessionLocal
    async with AsyncSessionLocal() as s:
        conv = await tg_ids.conversation_for(s, 77)
        row = (await s.execute(select(Message).where(Message.conversation_id == conv.id))).scalar_one()
        ann = await crud.get_identity_by_external(s, "telegram", "77")
    assert conv.title == "Ann Lee" and conv.kind == "direct" and conv.peer_identity_id == ann.id
    assert row.text == "meet at 6" and row.sender_name == "Ann Lee" and row.sender_identity_id == ann.id
    assert [d["type"] for d in sent] == ["new_message", "message_edited"]
    assert sent[0]["conversation_id"] == conv.id and sent[0]["message_id"] == row.id
    assert sent[1]["message"]["text"] == "meet at 6" and sent[1]["message_id"] == row.id
    assert [c.args for c in scam.call_args_list] == [(conv.id, [row.id], "realtime"), (conv.id, [row.id], "edit")]


@pytest.mark.asyncio
async def test_bare_delete_never_touches_channel_messages(db_engine):
    """MessageDeleted without a chat id applies to private chats and basic
    groups only; supergroup/channel message ids are per-channel."""
    from backend.db.database import AsyncSessionLocal
    from backend.telegram import sync

    async with AsyncSessionLocal() as s:
        dm = await tg_conv(s, 77)
        basic = await tg_conv(s, -1001234, title="Basic group")  # basic group: chat id 1001234
        supergroup = await tg_conv(s, -1001234567890, title="Supergroup")
        for conv in (dm, basic, supergroup):
            await save(s, conv, tg_msg(5, "same id", peer=tg_ids.peer_id_of(conv)))

    client = FakeRuntimeClient()
    handlers = {cb.__name__: cb for cb, _ in sync.register_event_handlers(client)}

    class Deleted:
        deleted_ids = [5]
        chat_id = None

    with patch.object(sync.ws_manager, "broadcast", AsyncMock()):
        await handlers["on_message_deleted"](Deleted())
    async with AsyncSessionLocal() as s:
        left = (await s.execute(select(Message.conversation_id))).scalars().all()
    assert left == [supergroup.id]


# ── auth / compose / media ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_mutators_require_token_and_reset_requires_confirmation(db_engine):
    from backend.api.router import create_app
    from backend.api import auth
    app = create_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as c:
        for path in ("/api/auth/lock", "/api/auth/resolve-1password",
                     "/api/accounts/telegram/auth", "/api/accounts/telegram/auth/x",
                     "/api/accounts/telegram/logout"):
            assert (await c.post(path, json={})).status_code == 401, path
        assert (await c.get("/api/accounts")).status_code == 401
        assert (await c.delete("/api/accounts/gmail:1")).status_code == 401
        # The Telegram-only onboarding routes are gone (sign-in is /api/accounts)
        for path in ("/api/auth/logout", "/api/auth/setup", "/api/auth/send-code",
                     "/api/auth/verify-code", "/api/auth/verify-2fa"):
            assert (await c.post(path, json={})).status_code in (404, 405), path
        for path in ("/api/auth/status", "/api/auth/has-credentials"):
            assert (await c.get(path)).status_code in (404, 405), path
        assert (await c.get("/api/auth/vault-status")).status_code == 200
        assert (await c.post("/api/auth/vault/reset", json={})).status_code == 400
        with patch.object(auth, "reset_vault") as reset, \
             patch.object(auth.manager, "stop_all", AsyncMock()) as stop:
            r = await c.post("/api/auth/vault/reset", json={"confirm": "RESET"})
        assert r.status_code == 200 and reset.called and stop.called


class FakeSendClient:
    def __init__(self, peer=77):
        self.calls = []
        self.peer = peer

    async def is_user_authorized(self):
        return True

    async def send_message(self, peer, text, reply_to=None):
        self.calls.append((peer, reply_to))
        await asyncio.sleep(0.02)
        return tg_msg(500 + len(self.calls), text, peer=self.peer, out=True)


@pytest.mark.asyncio
async def test_send_with_same_nonce_sends_once_and_persists_topic(api, db):
    conv = await tg_conv(db, -100, kind="group", is_forum=True)
    client = FakeSendClient(peer=-100)
    body = {"conversation_id": conv.id, "text": "hello", "topic_id": 10, "client_nonce": "n-1"}
    with use_client(client):
        r1, r2 = await asyncio.gather(api.post("/api/compose/send", json=body), api.post("/api/compose/send", json=body))
        r3 = await api.post("/api/compose/send", json=body)
    assert client.calls == [(-100, 10)]  # topic-aware: reply_to=top_msg_id, sent once, to the peer
    assert r1.json() == r2.json() == r3.json()
    assert r1.json()["message"]["topic_id"] == 10
    assert r1.json()["message_id"] == r1.json()["message"]["id"]


@pytest.mark.asyncio
async def test_reply_is_sent_with_telegram_id_and_stored_by_internal_id(api, db):
    conv = await seed_history(db, n=3)
    target = await stored(db, conv, 2)
    client = FakeSendClient()
    with use_client(client):
        r = await api.post("/api/compose/send", json={
            "conversation_id": conv.id, "text": "re", "reply_to_message_id": target.id,
        })
    assert client.calls == [(77, 2)]
    msg = r.json()["message"]
    assert msg["reply_to_message_id"] is None or msg["reply_to_message_id"] == target.id
    row = await stored(db, conv, 501)
    assert row.reply_to_external_id == "2"
    other = await seed_history(db, n=1, peer=88)
    foreign = await stored(db, other, 1)
    with use_client(client):
        r = await api.post("/api/compose/send", json={
            "conversation_id": conv.id, "text": "x", "reply_to_message_id": foreign.id,
        })
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_media_download_is_atomic_and_single_flight(db, tmp_path):
    conv = await tg_conv(db)
    msg = await f.message(db, conv, 9, "doc", attachments=[{"kind": "document", "filename": "a.pdf", "size": 3}])
    att_id = msg.attachments[0].id
    from backend.telegram import media

    class FakeMediaClient:
        def __init__(self, fail=False):
            self.fail = fail
            self.downloads = 0
            self.asked = []

        async def get_messages(self, peer, ids):
            self.asked.append((peer, ids))
            m = tg_msg(ids, "doc")
            m.media = types.MessageMediaDocument(document=types.Document(
                id=1, access_hash=1, file_reference=b"", date=None, mime_type="application/pdf",
                size=3, dc_id=1, attributes=[types.DocumentAttributeFilename("a.pdf")]))
            return m

        async def download_media(self, msg, file, thumb=None):
            self.downloads += 1
            with open(file, "wb") as fh:
                fh.write(b"par")
            await asyncio.sleep(0.02)
            if self.fail:
                raise ConnectionError("dropped")
            return file

    final = tmp_path / "media" / str(conv.id) / f"{att_id}.pdf"
    with patch.object(media, "MEDIA_DIR", tmp_path / "media"), patch.object(media, "DATA_DIR", tmp_path):
        failing = FakeMediaClient(fail=True)
        with use_client(failing):
            with pytest.raises(ConnectionError):
                await media.download_attachment(att_id)
        assert failing.asked == [(77, 9)]  # fetched by Telegram peer + message id
        assert not final.exists()
        assert list(final.parent.iterdir()) == []  # no partial file left

        ok = FakeMediaClient()
        with use_client(ok):
            a, b = await asyncio.gather(media.download_attachment(att_id), media.download_attachment(att_id))
        assert a == b == f"media/{conv.id}/{att_id}.pdf" and ok.downloads == 1
        assert final.read_bytes() == b"par"
    await db.refresh(msg)
    assert msg.attachments[0].local_path == f"media/{conv.id}/{att_id}.pdf"


# ── message dict shape ───────────────────────────────────────────────────────

SCAM_KEYS = {"scam_score", "scam_reason", "scam_category", "scam_manual_verdict",
             "scam_status", "scam_error", "effective_is_scam"}


@pytest.mark.asyncio
async def test_every_message_serializer_carries_scam_fields(api, db):
    """Window lists, compose responses and WS payloads all come from
    serializers.message_out, so they share one shape including the scam state."""
    conv = await seed_history(db, n=2)
    row = await stored(db, conv, 2)
    row.scam_manual_verdict = "scam"
    row.is_pinned = True
    conv.last_synced_at = datetime.utcnow()
    await db.commit()

    window = (await api.get(f"/api/conversations/{conv.id}/messages/window")).json()["messages"]
    pinned = (await api.get(f"/api/conversations/{conv.id}/pinned")).json()
    assert pinned
    for m in window + pinned:
        assert SCAM_KEYS <= m.keys()
    assert window[-1]["effective_is_scam"] is True and window[0]["effective_is_scam"] is False

    with use_client(FakeSendClient()):
        sent = (await api.post("/api/compose/send", json={"conversation_id": conv.id, "text": "x"})).json()["message"]
    assert SCAM_KEYS <= sent.keys()
