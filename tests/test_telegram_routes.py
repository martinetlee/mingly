"""Black-box tests of every route that does Telegram work: the Telethon calls
each one makes (request type and the arguments that matter), what it stores
and what it answers — plus 409 ``unsupported_channel`` for other channels.

The Telegram client is a recording fake behind ``connected_client``, so these
tests pin the routes' observable behaviour independently of where the
Telegram code lives.
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from telethon.tl import types

from backend.db import crud
from backend.db.models import AdminActionLog, Conversation, Message
from tests import factories as f

UTC = timezone.utc
DATE = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _user(uid, first="Ann", username=None, **kw):
    return types.User(id=uid, first_name=first, username=username, access_hash=1, **kw)


def _channel(cid, title="News", **kw):
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=DATE, access_hash=1, **kw)


def _msg(mid, text="hi", peer=77, out=True):
    return types.Message(
        id=mid, peer_id=types.PeerUser(peer) if peer > 0 else types.PeerChannel(-peer),
        date=DATE, message=text, out=out,
    )


async def _agen(items):
    for item in items:
        yield item


class FakeClient:
    """Records every call; answers from ``responses`` (request type name →
    value or callable(request))."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}
        self.next_id = 500

    def _answer(self, key, *args):
        r = self.responses.get(key)
        if isinstance(r, Exception):
            raise r
        return r(*args) if callable(r) else r

    def is_connected(self):
        return True

    async def is_user_authorized(self):
        return True

    async def __call__(self, request):
        name = type(request).__name__
        self.calls.append((name, request))
        return self._answer(name, request)

    def requests(self, name):
        return [r for n, r in self.calls if n == name]

    def methods(self, name):
        return [a for n, a in self.calls if n == name]

    async def get_input_entity(self, peer):
        return types.InputPeerUser(peer, 0) if peer > 0 else types.InputPeerChannel(-peer, 0)

    async def get_me(self):
        return _user(1, "Me")

    async def get_entity(self, x):
        self.calls.append(("get_entity", x))
        return self._answer("get_entity", x)

    async def send_message(self, peer, text, reply_to=None):
        self.calls.append(("send_message", (peer, text, reply_to)))
        self.next_id += 1
        return _msg(self.next_id, text, peer=peer)

    async def send_file(self, peer, file, caption=None, reply_to=None, force_document=False):
        self.calls.append(("send_file", (peer, file, caption, reply_to, force_document)))
        files = file if isinstance(file, list) else [file]
        out = []
        for i, _ in enumerate(files):
            self.next_id += 1
            caption_i = caption[i] if isinstance(caption, list) else caption
            out.append(_msg(self.next_id, caption_i or "[file]", peer=peer))
        return out if isinstance(file, list) else out[0]

    async def edit_message(self, peer, msg_id, text):
        self.calls.append(("edit_message", (peer, msg_id, text)))
        return _msg(msg_id, text, peer=peer)

    async def delete_messages(self, peer, msg_ids, revoke=False):
        self.calls.append(("delete_messages", (peer, list(msg_ids), revoke)))

    async def forward_messages(self, to_peer, msg_ids, from_peer):
        self.calls.append(("forward_messages", (to_peer, list(msg_ids), from_peer)))

    async def pin_message(self, peer, msg_id, notify=False):
        self.calls.append(("pin_message", (peer, msg_id, notify)))

    async def unpin_message(self, peer, msg_id):
        self.calls.append(("unpin_message", (peer, msg_id)))

    async def send_read_acknowledge(self, entity):
        self.calls.append(("send_read_acknowledge", entity))

    def iter_participants(self, peer, limit=None):
        self.calls.append(("iter_participants", (peer, limit)))
        return _agen(self._answer("iter_participants") or [])

    def iter_messages(self, peer, limit=None):
        self.calls.append(("iter_messages", (peer, limit)))
        return _agen(self._answer("iter_messages") or [])


def online(client):
    from backend.telegram import client as tg_client
    return patch.object(tg_client, "connected_client", AsyncMock(return_value=client))


def offline():
    from backend.telegram import client as tg_client
    from backend.telegram.client import TelegramUnavailable

    async def _none(require=True):
        if require:
            raise TelegramUnavailable()
        return None
    return patch.object(tg_client, "connected_client", _none)


async def _dm(db, peer=77):
    return await f.conversation(db, str(peer), title="Alice")


async def _group(db, peer=-1001, kind="group", **kw):
    return await f.conversation(db, str(peer), title="Group", kind=kind, **kw)


async def _mail(db):
    return await f.conversation(db, "thread-1", title="Hi", kind="email_thread", channel="gmail")


def _unsupported(r):
    return r.status_code == 409 and r.json()["code"] == "unsupported_channel"


# ── compose ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_replies_in_thread_and_stores_the_sent_message(api, db):
    conv = await _dm(db)
    original = await f.message(db, conv, 7, "question")
    client = FakeClient()
    with online(client):
        r = await api.post("/api/compose/send", json={
            "conversation_id": conv.id, "text": "answer", "reply_to_message_id": original.id,
        })
    assert r.status_code == 200, r.text
    assert client.methods("send_message") == [(77, "answer", 7)]
    body = r.json()
    assert body["success"] is True and body["message"]["text"] == "answer"
    assert body["message"]["reply_to_message_id"] == original.id
    assert body["message_id"] == body["message"]["id"]
    stored = (await db.execute(select(Message).where(Message.id == body["message_id"]))).scalar_one()
    assert stored.is_outgoing and stored.conversation_id == conv.id


@pytest.mark.asyncio
async def test_send_in_forum_topic_replies_to_the_topic_root(api, db):
    conv = await _group(db, is_forum=True)
    client = FakeClient()
    with online(client):
        r = await api.post("/api/compose/send", json={"conversation_id": conv.id, "text": "x", "topic_id": 42})
    assert r.status_code == 200, r.text
    assert client.methods("send_message") == [(-1001, "x", 42)]
    stored = (await db.execute(select(Message).where(Message.id == r.json()["message_id"]))).scalar_one()
    assert stored.topic_id == 42


@pytest.mark.asyncio
async def test_send_failure_is_400_and_offline_is_503(api, db):
    conv = await _dm(db)

    class Failing(FakeClient):
        async def send_message(self, *a, **k):
            raise RuntimeError("PEER_FLOOD")

    with online(Failing()):
        r = await api.post("/api/compose/send", json={"conversation_id": conv.id, "text": "x"})
    assert r.status_code == 400 and r.json()["detail"] == "PEER_FLOOD"
    with offline():
        r = await api.post("/api/compose/send", json={"conversation_id": conv.id, "text": "x"})
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_send_files_groups_media_and_documents_into_albums(api, db):
    conv = await _dm(db)
    client = FakeClient()
    files = [("files", ("a.jpg", b"1", "image/jpeg")), ("files", ("b.mp4", b"2", "video/mp4")),
             ("files", ("c.pdf", b"3", "application/pdf"))]
    with online(client):
        r = await api.post("/api/compose/send-files", data={"conversation_id": str(conv.id), "text": "cap"}, files=files)
    assert r.status_code == 200, r.text
    calls = client.methods("send_file")
    assert len(calls) == 2
    (peer, album, captions, _, as_doc), (_, doc, doc_caption, _, doc_flag) = calls
    assert peer == 77 and isinstance(album, list) and len(album) == 2 and captions == ["cap", ""] and not as_doc
    assert isinstance(doc, str) and doc_caption == "" and doc_flag is True
    body = r.json()
    assert body["success"] is True and body["partial"] is False
    assert [x["status"] for x in body["files"]] == ["sent", "sent", "sent"]
    assert [x["filename"] for x in body["files"]] == ["a.jpg", "b.mp4", "c.pdf"]
    assert all(x["message_id"] in body["message_ids"] for x in body["files"])
    assert len(body["messages"]) == 3


@pytest.mark.asyncio
async def test_send_files_partial_is_207_and_nothing_sent_is_400(api, db):
    conv = await _dm(db)

    class DocsFail(FakeClient):
        async def send_file(self, peer, file, force_document=False, **kw):
            if force_document:
                raise RuntimeError("FILE_PARTS_INVALID")
            return await super().send_file(peer, file, force_document=force_document, **kw)

    files = [("files", ("a.jpg", b"1", "image/jpeg")), ("files", ("c.pdf", b"3", "application/pdf"))]
    with online(DocsFail()):
        r = await api.post("/api/compose/send-files", data={"conversation_id": str(conv.id)}, files=files)
    assert r.status_code == 207, r.text
    assert [(x["status"], x.get("error")) for x in r.json()["files"]] == [
        ("sent", None), ("failed", "FILE_PARTS_INVALID")]

    class AllFail(FakeClient):
        async def send_file(self, *a, **k):
            raise RuntimeError("nope")

    with online(AllFail()):
        r = await api.post("/api/compose/send-files", data={"conversation_id": str(conv.id)},
                           files=[("files", ("c.pdf", b"3", "application/pdf"))])
    assert r.status_code == 400
    assert r.json()["code"] == "send_failed" and r.json()["files"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_edit_delete_forward(api, db):
    conv = await _dm(db)
    other = await _dm(db, 88)
    m1 = await f.message(db, conv, 11, "old", out=True)
    m2 = await f.message(db, conv, 12, "two", out=True)
    client = FakeClient()
    with online(client):
        r = await api.post("/api/compose/edit", json={"message_id": m1.id, "text": "new"})
        assert r.status_code == 200, r.text
        assert r.json()["message"]["id"] == m1.id and r.json()["message"]["text"] == "new"
        r = await api.post("/api/compose/forward", json={
            "from_conversation_id": conv.id, "message_ids": [m1.id, m2.id], "to_conversation_id": other.id})
        assert r.status_code == 200 and r.json() == {"success": True}
        r = await api.post("/api/compose/delete", json={
            "conversation_id": conv.id, "message_ids": [m2.id], "revoke": True})
        assert r.status_code == 200 and r.json() == {"success": True}
    assert client.methods("edit_message") == [(77, 11, "new")]
    assert client.methods("forward_messages") == [(88, [11, 12], 77)]
    assert client.methods("delete_messages") == [(77, [12], True)]
    assert await crud.get_message(db, m2.id) is None


@pytest.mark.asyncio
async def test_message_ops_are_409_for_other_channels(api, db):
    mail = await _mail(db)
    msg = await f.message(db, mail, 1, "x")
    with online(FakeClient()):
        assert _unsupported(await api.post("/api/compose/edit", json={"message_id": msg.id, "text": "y"}))
        assert _unsupported(await api.post("/api/compose/delete", json={"conversation_id": mail.id, "message_ids": [msg.id]}))
        assert _unsupported(await api.post("/api/messages/%d/pin" % msg.id, json={"pinned": True}))
        assert _unsupported(await api.get(f"/api/conversations/{mail.id}/members"))
        assert _unsupported(await api.get(f"/api/conversations/{mail.id}/topics"))
        assert _unsupported(await api.post(f"/api/conversations/{mail.id}/slow-mode", json={"seconds": 10}))
        assert _unsupported(await api.post(f"/api/conversations/{mail.id}/backfill"))
        assert _unsupported(await api.post(f"/api/admin/{mail.id}/ban", json={"identity_id": 1}))


# ── single messages ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_react_and_pin_message(api, db):
    conv = await _dm(db)
    msg = await f.message(db, conv, 21, "hi")
    client = FakeClient()
    with online(client):
        r = await api.post(f"/api/messages/{msg.id}/react", json={"emoji": "👍"})
        assert r.status_code == 200 and r.json() == {"success": True, "my_reaction": "👍"}
        r = await api.post(f"/api/messages/{msg.id}/pin", json={"pinned": True, "notify": True})
        assert r.status_code == 200 and r.json() == {"success": True, "is_pinned": True}
        r = await api.post(f"/api/messages/{msg.id}/pin", json={"pinned": False})
        assert r.status_code == 200 and r.json()["is_pinned"] is False
    [reaction] = client.requests("SendReactionRequest")
    assert reaction.peer == 77 and reaction.msg_id == 21 and reaction.reaction[0].emoticon == "👍"
    assert client.methods("pin_message") == [(77, 21, True)]
    assert client.methods("unpin_message") == [(77, 21)]


@pytest.mark.asyncio
async def test_failed_pin_is_400_with_reason(api, db):
    conv = await _dm(db)
    msg = await f.message(db, conv, 22, "hi")

    class Failing(FakeClient):
        async def pin_message(self, *a, **k):
            raise RuntimeError("CHAT_ADMIN_REQUIRED")

    with online(Failing()):
        r = await api.post(f"/api/messages/{msg.id}/pin", json={"pinned": True})
    assert r.status_code == 400 and r.json()["detail"] == "Cannot pin message: CHAT_ADMIN_REQUIRED"


# ── dialog-list state is mirrored best-effort ────────────────────────────────

@pytest.mark.asyncio
async def test_pin_archive_mute_and_read_mirror_to_telegram(api, db):
    conv = await _dm(db)
    client = FakeClient()
    with online(client):
        assert (await api.post(f"/api/conversations/{conv.id}/pin", json={"pinned": True})).json()["is_pinned"]
        assert (await api.post(f"/api/conversations/{conv.id}/archive", json={"archived": True})).json()["is_archived"]
        r = await api.post(f"/api/conversations/{conv.id}/mute", json={"muted": True, "duration": "forever"})
        assert r.json()["is_muted"] is True and r.json()["muted_until"] is None
        assert (await api.post(f"/api/conversations/{conv.id}/mark-read")).json() == {"success": True}
    [pin] = client.requests("ToggleDialogPinRequest")
    assert pin.pinned is True and pin.peer.peer.user_id == 77
    [folder] = client.requests("EditPeerFoldersRequest")
    assert folder.folder_peers[0].folder_id == 1
    [mute] = client.requests("UpdateNotifySettingsRequest")
    assert mute.settings.mute_until == 2145916800  # 2038-01-01 = indefinitely
    assert len(client.methods("send_read_acknowledge")) == 1


@pytest.mark.asyncio
async def test_mirroring_failures_and_offline_keep_the_local_change(api, db):
    conv = await _dm(db)
    client = FakeClient({"ToggleDialogPinRequest": RuntimeError("boom")})
    with online(client):
        r = await api.post(f"/api/conversations/{conv.id}/pin", json={"pinned": True})
    assert r.status_code == 200 and r.json()["is_pinned"] is True
    with offline():
        r = await api.post(f"/api/conversations/{conv.id}/archive", json={"archived": True})
        assert r.status_code == 200 and r.json()["is_archived"] is True
        assert (await api.post(f"/api/conversations/{conv.id}/mark-read")).status_code == 200


# ── members / presence / typing ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_members_are_recorded_and_ordered_by_role(api, db):
    conv = await _group(db)
    creator = _user(5, "Zed")
    creator.participant = types.ChannelParticipantCreator(user_id=5, admin_rights=types.ChatAdminRights())
    member = _user(6, "Amy", username="amy")
    client = FakeClient({"iter_participants": [member, creator]})
    with online(client):
        r = await api.get(f"/api/conversations/{conv.id}/members", params={"limit": 50})
    assert r.status_code == 200, r.text
    assert [(m["name"], m["role"], m["username"]) for m in r.json()] == [("Zed", "creator", None), ("Amy", "member", "amy")]
    assert client.methods("iter_participants") == [(-1001, 50)]
    assert all(m["identity_id"] and m["person_id"] for m in r.json())
    dm = await _dm(db)
    assert (await api.get(f"/api/conversations/{dm.id}/members")).json() == []


@pytest.mark.asyncio
async def test_members_refused_is_403(api, db):
    conv = await _group(db)

    class Refusing(FakeClient):
        def iter_participants(self, *a, **k):
            raise RuntimeError("CHAT_ADMIN_REQUIRED")

    with online(Refusing()):
        r = await api.get(f"/api/conversations/{conv.id}/members")
    assert r.status_code == 403 and r.json()["detail"] == "Cannot fetch members: CHAT_ADMIN_REQUIRED"


@pytest.mark.asyncio
async def test_presence_and_typing(api, db):
    conv = await _dm(db)
    status = types.UserStatusOnline(expires=DATE)
    client = FakeClient({"GetFullUserRequest": SimpleNamespace(users=[_user(77, status=status)])})
    with online(client):
        r = await api.get(f"/api/conversations/{conv.id}/presence")
        assert r.status_code == 200 and r.json()["status"] == "online"
        r = await api.post(f"/api/conversations/{conv.id}/typing")
        assert r.json() == {"success": True}
    [typing] = client.requests("SetTypingRequest")
    assert typing.peer == 77
    group = await _group(db)
    with online(FakeClient()):
        assert (await api.get(f"/api/conversations/{group.id}/presence")).json() == {"status": "unknown"}
    with online(FakeClient({"SetTypingRequest": RuntimeError("slow")})):
        assert (await api.post(f"/api/conversations/{conv.id}/typing")).json() == {"success": False, "error": "slow"}
    with offline():
        assert (await api.post(f"/api/conversations/{conv.id}/typing")).status_code == 503


# ── forum topics, slow mode, post stats ──────────────────────────────────────

@pytest.mark.asyncio
async def test_topics_list_create_close_and_fallback(api, db):
    conv = await _group(db, is_forum=True)
    topic = types.ForumTopic(
        id=3, date=DATE, peer=types.PeerChannel(1001), title="Ops", icon_color=7, top_message=3,
        read_inbox_max_id=0, read_outbox_max_id=0, unread_count=0, unread_mentions_count=0,
        unread_reactions_count=0, unread_poll_votes_count=0, from_id=types.PeerUser(5),
        notify_settings=types.PeerNotifySettings(), closed=True,
    )
    created = SimpleNamespace(updates=[SimpleNamespace(message=SimpleNamespace(id=99))])
    client = FakeClient({"GetForumTopicsRequest": SimpleNamespace(topics=[topic]), "CreateForumTopicRequest": created})
    with online(client):
        r = await api.get(f"/api/conversations/{conv.id}/topics")
        assert r.status_code == 200
        assert [(t["id"], t["title"], t["icon_color"], t["is_closed"]) for t in r.json()] == [(3, "Ops", 7, True)]
        r = await api.post(f"/api/conversations/{conv.id}/topics", json={"title": "New"})
        assert r.json() == {"success": True, "topic_id": 99}
        r = await api.post(f"/api/conversations/{conv.id}/topics/3/state", json={"closed": False})
        assert r.json() == {"success": True, "topic_id": 3, "closed": False}
    assert client.requests("CreateForumTopicRequest")[0].title == "New"
    edit = client.requests("EditForumTopicRequest")[0]
    assert (edit.peer, edit.topic_id, edit.closed) == (-1001, 3, False)

    await f.message(db, conv, 50, "in topic", topic_id=8)
    with online(FakeClient({"GetForumTopicsRequest": RuntimeError("not a forum")})):
        r = await api.get(f"/api/conversations/{conv.id}/topics")
    assert [(t["id"], t["title"]) for t in r.json()] == [(8, "Topic 8")]
    with online(FakeClient({"CreateForumTopicRequest": RuntimeError("TOPICS_DISABLED")})):
        r = await api.post(f"/api/conversations/{conv.id}/topics", json={"title": "x"})
    assert r.status_code == 400 and r.json()["detail"] == "Cannot create topic: TOPICS_DISABLED"


@pytest.mark.asyncio
async def test_slow_mode_and_post_stats(api, db):
    conv = await _group(db, kind="channel")
    await f.message(db, conv, 31, "stored post")
    post = _msg(31, "hello", peer=-1001)
    post.views, post.forwards = 120, 4
    post.replies = types.MessageReplies(replies=2, replies_pts=0)
    empty = _msg(32, "", peer=-1001)
    client = FakeClient({"iter_messages": [post, empty]})
    with online(client):
        r = await api.post(f"/api/conversations/{conv.id}/slow-mode", json={"seconds": 30})
        assert r.json() == {"success": True, "slow_mode_seconds": 30}
        r = await api.get(f"/api/conversations/{conv.id}/post-stats", params={"limit": 500})
    assert r.status_code == 200, r.text
    [stat] = r.json()
    assert (stat["external_id"], stat["views"], stat["forwards"], stat["replies_count"]) == ("31", 120, 4, 2)
    assert stat["message_id"] is not None
    assert client.methods("iter_messages") == [(-1001, 100)]
    slow = client.requests("ToggleSlowModeRequest")[0]
    assert (slow.channel, slow.seconds) == (-1001, 30)
    await db.refresh(conv)
    assert conv.slow_mode_seconds == 30
    group = await _group(db, peer=-1002)
    with online(FakeClient()):
        assert (await api.get(f"/api/conversations/{group.id}/post-stats")).status_code == 400


# ── search ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_global_message_search_stores_hit_conversations(api, db):
    sender = _user(77, "Alice")
    hit = types.Message(id=5, peer_id=types.PeerUser(77), from_id=types.PeerUser(77), date=DATE, message="the plan")
    client = FakeClient({"SearchGlobalRequest": SimpleNamespace(chats=[], users=[sender], messages=[hit])})
    with online(client):
        r = await api.get("/api/conversations/search/messages", params={"q": "plan"})
    assert r.status_code == 200, r.text
    [res] = r.json()
    assert res["conversation_title"] == "Alice" and res["conversation_kind"] == "direct"
    assert res["messages"] == [{"external_id": "5", "text": "the plan", "sender_name": "Alice",
                                "date": "2026-01-01T12:00:00Z"}]
    assert client.requests("SearchGlobalRequest")[0].q == "plan"
    with offline():
        assert (await api.get("/api/conversations/search/messages", params={"q": "plan"})).json() == []


@pytest.mark.asyncio
async def test_directory_search_combines_local_and_remote(api, db):
    local = await _dm(db)
    channel = _channel(2002, "Alpha News", username="alphanews")
    client = FakeClient({"SearchRequest": SimpleNamespace(users=[_user(77, "Alice")], chats=[channel])})
    with online(client):
        r = await api.get("/api/telegram/search", params={"q": "Al"})
    assert r.status_code == 200, r.text
    results = r.json()
    assert results[0]["id"] == local.id
    assert results[1] == {"id": None, "peer_id": -1000000002002, "channel": "telegram", "title": "Alpha News",
                          "kind": "channel", "username": "alphanews"}
    assert len(results) == 2  # the stored Alice isn't listed twice


@pytest.mark.asyncio
async def test_folders(api, db):
    conv = await _dm(db)
    folder = types.DialogFilter(id=2, title=types.TextWithEntities(text="Work", entities=[]), pinned_peers=[],
                                include_peers=[types.InputPeerUser(77, 0)], exclude_peers=[], groups=True)
    with online(FakeClient({"GetDialogFiltersRequest": SimpleNamespace(filters=[types.DialogFilterDefault(), folder])})):
        r = await api.get("/api/telegram/folders")
    assert r.status_code == 200, r.text
    [out] = r.json()
    assert (out["id"], out["title"], out["conversation_ids"], out["include_groups"]) == (2, "Work", [conv.id], True)
    with offline():
        assert (await api.get("/api/telegram/folders")).json() == []


# ── open / create / join / resolve ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_create_and_join_store_conversations(api, db):
    person = await f.identity(db, "88", display_name="Bob")
    chat = types.Chat(id=3003, title="Team", photo=types.ChatPhotoEmpty(), participants_count=2, date=DATE, version=1)
    news = _channel(4004, "Fresh")
    joined = _channel(5005, "Joined")
    client = FakeClient({
        "get_entity": lambda x: {88: _user(88, "Bob"), -1004004: news, "pub": joined}.get(x, _user(88, "Bob")),
        "CreateChatRequest": SimpleNamespace(chats=[chat]),
        "CreateChannelRequest": SimpleNamespace(chats=[news]),
        "ImportChatInviteRequest": SimpleNamespace(chats=[joined]),
        "JoinChannelRequest": SimpleNamespace(chats=[joined]),
    })
    with online(client):
        r = await api.post("/api/telegram/conversations/ensure", json={"peer_id": -1004004})
        assert r.status_code == 200 and r.json()["title"] == "Fresh" and r.json()["kind"] == "channel"
        r = await api.post("/api/telegram/create/private", json={"person_id": person.person_id})
        assert r.status_code == 200 and r.json()["kind"] == "direct"
        r = await api.post("/api/telegram/create/group", json={"name": "Team", "person_ids": [person.person_id]})
        assert r.status_code == 200 and r.json()["title"] == "Team" and r.json()["kind"] == "group"
        r = await api.post("/api/telegram/create/channel", json={"name": "Fresh", "about": "a"})
        assert r.status_code == 200 and r.json()["title"] == "Fresh"
        r = await api.post("/api/telegram/join", json={"link": "https://t.me/+abc"})
        assert r.status_code == 200 and r.json()["title"] == "Joined"
        r = await api.post("/api/telegram/join", json={"link": "@pub"})
        assert r.status_code == 200
        assert (await api.post("/api/telegram/join", json={"link": " "})).status_code == 400
        assert (await api.post("/api/telegram/join", json={"link": "t.me/"})).status_code == 400
    assert client.requests("ImportChatInviteRequest")[0].hash == "abc"
    assert client.requests("CreateChatRequest")[0].title == "Team"
    channel_req = client.requests("CreateChannelRequest")[0]
    assert (channel_req.title, channel_req.about, channel_req.broadcast) == ("Fresh", "a", True)
    titles = {c.title for c in (await db.execute(select(Conversation))).scalars()}
    assert {"Fresh", "Team", "Joined"} <= titles


@pytest.mark.asyncio
async def test_create_group_needs_resolvable_members(api, db):
    nobody = await f.identity(db, "x@y.z", channel="gmail", display_name="Mail only")
    with online(FakeClient()):
        r = await api.post("/api/telegram/create/group", json={"name": "T", "person_ids": [nobody.person_id]})
    assert r.status_code == 400 and r.json()["detail"] == "No valid users could be resolved"
    with online(FakeClient()):
        r = await api.post("/api/telegram/create/group", json={"name": "T", "person_ids": []})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_resolve_username(api, db):
    client = FakeClient({"get_entity": lambda name: _user(91, "Dana") if name == "dana" else _channel(6006, "Chan")})
    with online(client):
        r = await api.get("/api/telegram/resolve", params={"username": "@dana"})
        assert r.status_code == 200 and r.json()["person_id"] and r.json()["conversation_id"] is None
        r = await api.get("/api/telegram/resolve", params={"username": "chan"})
        assert r.status_code == 200 and r.json()["person_id"] is None and r.json()["conversation_id"]
    with online(FakeClient({"get_entity": ValueError("No user has \"ghost\" as username")})):
        r = await api.get("/api/telegram/resolve", params={"username": "ghost"})
    assert r.status_code == 404 and r.json()["detail"].startswith("Not found:")


# ── address book ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_address_book_sync_marks_contacts(api, db):
    client = FakeClient({"GetContactsRequest": SimpleNamespace(users=[_user(201, "C1"), _user(202, "C2")])})
    with online(client):
        r = await api.post("/api/people/sync")
    assert r.status_code == 200 and r.json() == {"imported": 2, "total": 2}
    with online(client):
        assert (await api.post("/api/people/sync")).json() == {"imported": 0, "total": 2}


# ── history sync routes ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_backfill_and_archive_history_delegate_to_the_coordinator(api, db):
    from backend.connectors.base import SyncResult
    from backend.telegram.coordinator import coordinator

    conv = await _dm(db)
    await f.message(db, conv, 40, "oldest")
    results = [SyncResult("changed", conv.id, added=3), SyncResult("unchanged", conv.id, exhausted=True)]
    with patch.object(coordinator, "sync_conversation", AsyncMock(side_effect=results)) as sync, \
         patch("backend.telegram.archive.request_conversation", AsyncMock()) as archive:
        r = await api.post(f"/api/conversations/{conv.id}/sync", params={"limit": 999})
        assert r.status_code == 200 and r.json()["status"] == "changed" and r.json()["added"] == 3
        r = await api.post(f"/api/conversations/{conv.id}/backfill", params={"limit": 20})
        assert r.json() == {"added": 0, "exhausted": True, "status": "unchanged"}
        r = await api.post(f"/api/conversations/{conv.id}/archive-history")
        assert r.status_code == 200 and r.json()["queued"] is True
    assert sync.call_args_list[0].args == (conv.id, 200)
    assert sync.call_args_list[1].args == (conv.id, 20) and sync.call_args_list[1].kwargs["offset_seq"] == 40
    archive.assert_awaited_once_with(conv.id, priority=True)

    flood = SyncResult("skipped", conv.id, flood_wait=17)
    with patch.object(coordinator, "sync_conversation", AsyncMock(return_value=flood)):
        r = await api.post(f"/api/conversations/{conv.id}/sync")
    assert r.status_code == 429 and r.headers["retry-after"] == "17"
    gone = SyncResult("unavailable", conv.id, error="CHANNEL_PRIVATE")
    with patch.object(coordinator, "sync_conversation", AsyncMock(return_value=gone)), online(FakeClient()):
        r = await api.post(f"/api/conversations/{conv.id}/backfill")
    assert r.status_code == 422 and r.json()["detail"] == "CHANNEL_PRIVATE"
    with patch.object(coordinator, "sync_conversation", AsyncMock(return_value=gone)), offline():
        r = await api.post(f"/api/conversations/{conv.id}/sync")
    assert r.status_code == 503


# ── admin ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_actions_call_telegram_and_are_logged(api, db):
    conv = await _group(db)
    target = await f.identity(db, "301", display_name="Target")
    invite = SimpleNamespace(link="https://t.me/+x", title="t", expire_date=None, usage_limit=5, usage=1,
                             request_needed=False, revoked=False)
    client = FakeClient({
        "GetExportedChatInvitesRequest": SimpleNamespace(invites=[invite]),
        "ExportChatInviteRequest": invite,
        "get_entity": SimpleNamespace(title="Group"),
        "GetFullChannelRequest": SimpleNamespace(full_chat=SimpleNamespace(about="About us")),
    })
    perms = {"change_info": True, "post_messages": False, "edit_messages": False, "delete_messages": True,
             "ban_users": True, "invite_users": False, "pin_messages": True, "manage_call": False,
             "anonymous": False, "add_admins": False}
    base = f"/api/admin/{conv.id}"
    with online(client):
        assert (await api.post(f"{base}/promote", json={"identity_id": target.id, "permissions": perms})).json() == {"success": True}
        assert (await api.post(f"{base}/demote", json={"identity_id": target.id})).json() == {"success": True}
        assert (await api.post(f"{base}/ban", json={"identity_id": target.id})).json() == {"success": True}
        assert (await api.post(f"{base}/restrict", json={"identity_id": target.id, "send_messages": True,
                                                        "until_date": 1767225600})).json() == {"success": True}
        assert (await api.post(f"{base}/approve", json={"identity_id": target.id})).json() == {"success": True}
        links = (await api.get(f"{base}/invite-links")).json()
        assert links == [{"link": "https://t.me/+x", "title": "t", "expire_date": None, "usage_limit": 5,
                          "usage": 1, "request_needed": False, "revoked": False}]
        created = (await api.post(f"{base}/invite-links", json={"usage_limit": 5})).json()
        assert created["link"] == "https://t.me/+x" and created["revoked"] is False
        assert (await api.post(f"{base}/invite-links/revoke", json={"link": "https://t.me/+x"})).json() == {"success": True}
        assert (await api.get(f"{base}/settings")).json() == {"title": "Group", "about": "About us"}
        r = await api.put(f"{base}/settings", json={"title": "New", "about": "Desc"})
        assert r.json() == {"success": True, "updated": ["title", "about"]}

    promote = client.requests("EditAdminRequest")[0]
    assert (promote.channel, promote.user_id, promote.rank) == (-1001, 301, "Admin")
    assert promote.admin_rights.change_info and promote.admin_rights.ban_users and not promote.admin_rights.add_admins
    demote = client.requests("EditAdminRequest")[1]
    assert demote.rank == "" and not demote.admin_rights.ban_users
    ban, restrict, approve = client.requests("EditBannedRequest")
    assert ban.banned_rights.view_messages and ban.banned_rights.send_messages
    assert restrict.banned_rights.send_messages is False and restrict.banned_rights.send_media is True
    assert restrict.banned_rights.until_date is not None
    assert not approve.banned_rights.send_messages and not approve.banned_rights.view_messages
    assert client.requests("ExportChatInviteRequest")[0].usage_limit == 5
    assert client.requests("EditExportedChatInviteRequest")[0].revoked is True
    assert client.requests("EditTitleRequest")[0].title == "New"
    assert client.requests("EditChatAboutRequest")[0].about == "Desc"
    actions = [a.action for a in (await db.execute(select(AdminActionLog).order_by(AdminActionLog.id))).scalars()]
    assert actions == ["promote", "demote", "ban", "restrict", "approve", "create_invite_link",
                       "revoke_invite_link", "update_settings"]


@pytest.mark.asyncio
async def test_admin_guards(api, db):
    dm = await _dm(db)
    group = await _group(db)
    with online(FakeClient()):
        r = await api.post(f"/api/admin/{dm.id}/ban", json={"identity_id": 1})
        assert r.status_code == 400
    with online(FakeClient({"EditBannedRequest": RuntimeError("USER_ADMIN_INVALID")})):
        target = await f.identity(db, "302", display_name="T")
        r = await api.post(f"/api/admin/{group.id}/ban", json={"identity_id": target.id})
    assert r.status_code == 500 and r.json()["detail"] == "Failed to ban user: USER_ADMIN_INVALID"
    with offline():
        assert (await api.post(f"/api/admin/{group.id}/ban", json={"identity_id": target.id})).status_code == 503
