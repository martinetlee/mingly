"""Gmail connector against a fake Google (httpx.MockTransport, no network):
sign-in, backfill, history polling, send, attachments, payload shapes and
per-channel notification preferences."""

import asyncio
import base64
import email
import json
import os
from contextlib import ExitStack
from email import policy
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend import secrets as vault
from backend.connectors.gmail import oauth
from backend.connectors.gmail.connector import GmailConnector
from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import Account, Conversation, Message, MessageParticipant

ME = "me@example.com"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def gmsg(mid, thread, *, frm, to=ME, cc=None, subject="Plans", text="hello", html=None, labels=("INBOX", "UNREAD"),
         date_ms=1_767_000_000_000, msgid=None, in_reply_to=None, attachment=None):
    headers = [("From", frm), ("To", to), ("Subject", subject), ("Message-ID", msgid or f"<{mid}@mail>")]
    if cc:
        headers.append(("Cc", cc))
    if in_reply_to:
        headers.append(("In-Reply-To", in_reply_to))
    parts = [{"partId": "0", "mimeType": "text/plain", "filename": "", "headers": [],
              "body": {"size": len(text), "data": b64(text.encode())}}]
    if html:
        parts.append({"partId": "1", "mimeType": "text/html", "filename": "", "headers": [],
                      "body": {"size": len(html), "data": b64(html.encode())}})
    payload = {"partId": "", "mimeType": "multipart/alternative", "filename": "",
               "headers": [{"name": k, "value": v} for k, v in headers], "body": {"size": 0}, "parts": parts}
    if attachment:
        payload = {"partId": "", "mimeType": "multipart/mixed", "filename": "", "headers": payload["headers"],
                   "body": {"size": 0}, "parts": [dict(payload, partId="0", headers=[]), attachment]}
    return {"id": mid, "threadId": thread, "labelIds": list(labels), "snippet": text[:50],
            "internalDate": str(date_ms), "payload": payload}


class FakeGoogle:
    """Just enough of oauth2 + Gmail v1 for the connector."""

    def __init__(self):
        self.threads: dict[str, list] = {}
        self.history_pages: list = []
        self.history_status = 200
        self.history_id = "1000"
        self.sent: list[dict] = []
        self.attachments: dict[tuple, bytes] = {}
        self.token_requests: list[dict] = []
        self.fail_next: list[int] = []
        self.calls: list[str] = []
        self.modified: list = []
        self.labels = [{"id": "Label_1", "name": "Clients", "type": "user"}, {"id": "INBOX", "type": "system"}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.calls.append(f"{request.method} {url.path}")
        if url.host == "oauth2.googleapis.com":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if url.path == "/revoke":
                return httpx.Response(200)
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600, "refresh_token": "rt-1"})
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={"error": {"message": "boom"}})
        path = url.path.replace("/gmail/v1/users/me", "")
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": ME, "historyId": self.history_id})
        if path == "/labels":
            return httpx.Response(200, json={"labels": self.labels})
        if path == "/threads":
            return httpx.Response(200, json={"threads": [{"id": t} for t in self.threads]})
        if path.startswith("/threads/") and path.endswith("/modify"):
            tid = path.split("/")[2]
            body = json.loads(request.content)
            self.modified.append((tid, body))
            for m in self.threads.get(tid, []):
                m["labelIds"] = [l for l in m["labelIds"] if l not in body["removeLabelIds"]] + body["addLabelIds"]
            return httpx.Response(200, json={"id": tid})
        if path.startswith("/threads/"):
            tid = path.split("/")[2]
            if tid not in self.threads:
                return httpx.Response(404, json={"error": {"message": "not found"}})
            return httpx.Response(200, json={"id": tid, "messages": self.threads[tid]})
        if path == "/history":
            if self.history_status != 200:
                return httpx.Response(self.history_status, json={"error": {"message": "expired"}})
            page = self.history_pages.pop(0) if self.history_pages else {"historyId": self.history_id}
            return httpx.Response(200, json=page)
        if path == "/messages/send":
            body = json.loads(request.content)
            self.sent.append(body)
            raw = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]), policy=policy.default)
            tid = body.get("threadId") or "t-new"
            mid = f"sent-{len(self.sent)}"
            self.threads.setdefault(tid, []).append(gmsg(
                mid, tid, frm=ME, to=str(raw["To"]), subject=str(raw["Subject"]), text="sent body",
                labels=("SENT",), date_ms=1_767_000_900_000,
            ))
            return httpx.Response(200, json={"id": mid, "threadId": tid, "labelIds": ["SENT"]})
        if "/attachments/" in path:
            _, _, mid, _, att = path.split("/")
            data = self.attachments.get((mid, att))
            if data is None:
                return httpx.Response(400, json={"error": {"message": "Invalid attachment token"}})
            return httpx.Response(200, json={"size": len(data), "data": b64(data)})
        if path.startswith("/messages/"):
            mid = path.split("/")[2]
            for msgs in self.threads.values():
                for m in msgs:
                    if m["id"] == mid:
                        return httpx.Response(200, json=m)
            return httpx.Response(404, json={"error": {"message": "not found"}})
        return httpx.Response(500, json={"error": {"message": f"unhandled {path}"}})


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(oauth, "transport", httpx.MockTransport(fake.handler))
    monkeypatch.setattr(oauth, "_pending", {})
    return fake


@pytest.fixture
def unlocked_vault(monkeypatch):
    monkeypatch.setattr(vault, "_fernet", Fernet(Fernet.generate_key()))
    monkeypatch.setattr(vault, "_salt", os.urandom(16))
    monkeypatch.setattr(vault, "_cache", {})


@pytest.fixture
def broadcasts(monkeypatch):
    events = []

    async def capture(data):
        events.append(data)

    from backend.ingest import fanout, service
    from backend.connectors.gmail import connector as conn_mod

    for mod in (fanout, service, conn_mod):
        monkeypatch.setattr(mod.ws_manager, "broadcast", capture)
    monkeypatch.setattr(fanout, "enqueue_scam", AsyncMock())
    monkeypatch.setattr(fanout, "stamp_triage", AsyncMock())
    monkeypatch.setattr(fanout, "send_native_notification", MagicMock())
    return events


async def _signed_in(db, google) -> GmailConnector:
    """A connector for a stored account with credentials in the vault."""
    acc = Account(channel="gmail", external_id=ME, label=ME)
    db.add(acc)
    await db.commit()
    oauth.Credentials("cid", "secret", "rt-1").save(acc.id)
    c = GmailConnector(acc.id)
    c._self_email = ME
    from backend.connectors.gmail.api import GmailApi
    c._api = GmailApi(oauth.Credentials.load(acc.id))
    return c


# ── sign-in ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sign_in_fields_browser_callback_done(db, google, unlocked_vault, broadcasts):
    from backend.api.router import create_app
    from backend.connectors.manager import ConnectorManager
    from backend.connectors import gmail

    m = ConnectorManager()
    gmail.register(m)
    step = await m.begin_auth("gmail", {})
    assert step.kind == "fields" and [f.name for f in step.fields] == ["client_id", "client_secret"]
    assert "Desktop app" in step.message and "Gmail API" in step.message

    again = await m.continue_auth(step.flow_id, {"client_id": "cid"})
    assert again.kind == "fields" and "both" in again.message.lower()

    browser = await m.continue_auth(step.flow_id, {"client_id": "cid.apps.googleusercontent.com", "client_secret": "s3"})
    assert browser.kind == "oauth_browser" and browser.poll
    q = {k: v[0] for k, v in parse_qs(urlsplit(browser.auth_url).query).items()}
    assert q["code_challenge_method"] == "S256" and q["code_challenge"] and q["access_type"] == "offline"
    assert q["prompt"] == "consent" and q["redirect_uri"].endswith("/api/connectors/gmail/oauth/callback")
    assert "gmail.modify" in q["scope"] and "gmail.send" in q["scope"] and "calendar.readonly" in q["scope"]
    assert (await m.continue_auth(step.flow_id, {})).kind == "oauth_browser"  # still waiting

    # Google redirects the browser: no session token, loopback Host.
    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1:8765") as browser_client:
        bad = await browser_client.get("/api/connectors/gmail/oauth/callback", params={"state": "nope", "code": "c"})
        assert bad.status_code == 400
        ok = await browser_client.get(  # a cross-site navigation GET passes the Host/Origin middleware
            "/api/connectors/gmail/oauth/callback", params={"state": q["state"], "code": "code-1"},
            headers={"Origin": "https://accounts.google.com", "Referer": "https://accounts.google.com/"},
        )
    assert ok.status_code == 200 and "close this tab" in ok.text
    exchange = google.token_requests[-1]
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "code-1" and exchange["code_verifier"]

    with patch.object(GmailConnector, "start", AsyncMock(return_value=True)) as start:
        done = await m.continue_auth(step.flow_id, {})
        await asyncio.sleep(0)
    assert done.kind == "done" and ME in done.message
    start.assert_awaited()
    acc = (await db.execute(select(Account).where(Account.channel == "gmail"))).scalar_one()
    assert acc.external_id == ME and acc.label == ME and acc.self_identity_id is not None
    me = await crud.get_identity(db, acc.self_identity_id)
    assert me.is_self and me.external_id == ME
    assert vault.get_secret(f"gmail:{acc.id}:refresh_token") == "rt-1"
    assert vault.get_secret(f"gmail:{acc.id}:client_secret") == "s3"
    assert m.get(f"gmail:{acc.id}") is not None


@pytest.mark.asyncio
async def test_denied_consent_ends_in_error_step(google, unlocked_vault):
    c = GmailConnector()
    step = await c.begin_auth({})
    browser = await c.continue_auth(step.flow_id, {"client_id": "a", "client_secret": "b"})
    state = parse_qs(urlsplit(browser.auth_url).query)["state"][0]
    await oauth.complete(state, None, "access_denied")
    err = await c.continue_auth(step.flow_id, {})
    assert err.kind == "error" and "denied" in err.message


# ── sync ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfill_maps_threads_messages_participants_and_labels(db, google, unlocked_vault, broadcasts):
    google.threads = {
        "t1": [
            gmsg("m1", "t1", frm="Ann Lee <Ann@Example.com>", html='<p>Hi <img src="https://track/x.gif"></p>',
                 labels=("INBOX", "UNREAD", "Label_1")),
            gmsg("m2", "t1", frm=ME, to="ann@example.com", subject="Re: Plans", text="reply",
                 labels=("SENT",), date_ms=1_767_000_100_000, in_reply_to="<m1@mail>"),
            gmsg("d1", "t1", frm=ME, to="ann@example.com", labels=("DRAFT",)),
        ],
        "t2": [gmsg("m3", "t2", frm="news@shop.example", subject="Sale", labels=("INBOX", "CATEGORY_PROMOTIONS"))],
        "t3": [gmsg("m4", "t3", frm="bob@example.com", cc="carol@example.com", subject="Group", labels=("INBOX",))],
    }
    c = await _signed_in(db, google)
    await c._backfill(90)

    convs = {cv.external_id: cv for cv in (await db.execute(select(Conversation))).scalars().all()}
    t1, t2, t3 = convs["t1"], convs["t2"], convs["t3"]
    assert t1.kind == "email_thread" and t1.title == "Plans" and not t1.is_archived and t1.unread_count == 1
    assert json.loads(t1.labels_json) == ["Clients", "Inbox", "Sent"]
    peer = await crud.get_identity(db, t1.peer_identity_id)
    assert peer.external_id == "ann@example.com" and peer.display_name == "Ann Lee"
    assert t2.is_archived  # promotions stay out of the inbox
    assert t3.peer_identity_id is None  # two counterparts → not one-to-one

    msgs = {m.external_id: m for m in (await db.execute(select(Message))).scalars().all()}
    assert "d1" not in msgs  # drafts are skipped
    m1, m2 = msgs["m1"], msgs["m2"]
    assert m1.subject == "Plans" and m1.text == "hello" and 'data-src="https://track/x.gif"' in m1.text_html
    assert not m1.is_outgoing and m2.is_outgoing and m2.reply_to_external_id == "m1"
    assert json.loads(m1.meta_json)["rfc822_message_id"] == "<m1@mail>"
    roles = (await db.execute(select(MessageParticipant.role).where(MessageParticipant.message_id == m1.id))).scalars().all()
    assert sorted(roles) == ["from", "to"]

    state = json.loads((await crud.get_account(db, c.account_id)).sync_state_json)
    assert state["history_id"] == "1000"
    assert not any(e.get("type") == "new_message" for e in broadcasts)  # backfill never notifies
    await c._api.close()


@pytest.mark.asyncio
async def test_history_poll_fans_out_only_new_messages_and_applies_deletes(db, google, unlocked_vault, broadcasts):
    google.threads = {"t1": [gmsg("m1", "t1", frm="ann@example.com"), gmsg("m9", "t1", frm="ann@example.com",
                                                                            date_ms=1_767_000_000_500)]}
    c = await _signed_in(db, google)
    await c._backfill(90)
    broadcasts.clear()

    # A reply arrives in a thread whose older message we never stored, and m9 is deleted for good.
    google.threads["t1"] = [gmsg("m0", "t1", frm="ann@example.com", date_ms=1_766_000_000_000)] + google.threads["t1"]
    google.threads["t1"] = [m for m in google.threads["t1"] if m["id"] != "m9"]
    google.threads["t1"].append(gmsg("m5", "t1", frm="ann@example.com", text="new!", date_ms=1_767_000_200_000))
    google.history_pages = [{
        "history": [
            {"messagesAdded": [{"message": {"id": "m5", "threadId": "t1"}}]},
            {"messagesDeleted": [{"message": {"id": "m9", "threadId": "t1"}}]},
        ],
        "historyId": "1100",
    }]
    await c._incremental({"history_id": "1000"})

    new = [e for e in broadcasts if e["type"] == "new_message"]
    assert [e["message"]["external_id"] for e in new] == ["m5"]  # m0 is history, not news
    assert {p["role"] for p in new[0]["message"]["participants"]} == {"from", "to"}
    deleted = [e for e in broadcasts if e["type"] == "messages_deleted"]
    assert len(deleted) == 1 and len(deleted[0]["message_ids"]) == 1
    exts = set((await db.execute(select(Message.external_id))).scalars().all())
    assert exts == {"m0", "m1", "m5"}
    assert json.loads((await crud.get_account(db, c.account_id)).sync_state_json)["history_id"] == "1100"
    await c._api.close()


@pytest.mark.asyncio
async def test_expired_history_id_triggers_bounded_resync(db, google, unlocked_vault, broadcasts):
    google.threads = {"t1": [gmsg("m1", "t1", frm="ann@example.com")]}
    google.history_status = 404
    google.history_id = "2000"
    c = await _signed_in(db, google)
    await c._incremental({"history_id": "5", "last_sync_at": "2000-01-01T00:00:00"})
    assert "GET /gmail/v1/users/me/threads" in google.calls
    assert json.loads((await crud.get_account(db, c.account_id)).sync_state_json)["history_id"] == "2000"
    await c._api.close()


@pytest.mark.asyncio
async def test_run_loop_backs_off_on_rate_limit_and_stops_on_revoked_grant(db, google, unlocked_vault, broadcasts):
    c = await _signed_in(db, google)
    c._wake = asyncio.Event()
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) == 2:
            raise asyncio.CancelledError

    c._sleep = fake_sleep
    google.fail_next = [429, 503]
    with pytest.raises(asyncio.CancelledError):
        await c._run()
    assert delays == [5.0, 10.0]
    states = [e["state"] for e in broadcasts if e["type"] == "connector_status"]
    assert states == ["syncing", "reconnecting", "reconnecting"]
    assert "429" in c.status().error or "503" in c.status().error
    assert all(e["account_key"] == f"gmail:{c.account_id}" and e["channel"] == "gmail"
               for e in broadcasts if e["type"] == "connector_status")

    # A revoked grant: the token endpoint refuses → unauthorized, loop ends.
    def refuse(request):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been revoked."})

    c._api._token = None
    with patch.object(oauth, "transport", httpx.MockTransport(refuse)):
        c._api._client = None
        await c._run()
    assert c.status().state == "unauthorized" and "revoked" in c.status().error
    await c._api.close()


# ── send / attachments ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reply_threads_with_headers_and_returns_stored_id(db, google, unlocked_vault, broadcasts):
    google.threads = {"t1": [gmsg("m1", "t1", frm="Ann <ann@example.com>", msgid="<orig@mail>")]}
    c = await _signed_in(db, google)
    await c._backfill(90)
    conv = (await db.execute(select(Conversation))).scalar_one()
    from backend.connectors.base import OutgoingMessage

    ids = await c.send(conv, OutgoingMessage(text="Sounds good", cc=["Bob@Example.com", ME]))
    [sent] = google.sent
    assert sent["threadId"] == "t1"
    raw = email.message_from_bytes(base64.urlsafe_b64decode(sent["raw"]), policy=policy.default)
    assert raw["To"] == "ann@example.com" and raw["Cc"] == "bob@example.com" and raw["From"] == ME
    assert raw["Subject"] == "Re: Plans" and raw["In-Reply-To"] == "<orig@mail>"
    assert raw["References"] == "<orig@mail>"
    stored = await crud.get_message(db, ids[0])
    assert stored.external_id == "sent-1" and stored.is_outgoing and stored.conversation_id == conv.id
    await c._api.close()


@pytest.mark.asyncio
async def test_compose_api_sends_through_the_connector_with_participants(api, db, google, unlocked_vault, broadcasts):
    from backend.connectors.manager import manager

    google.threads = {"t1": [gmsg("m1", "t1", frm="ann@example.com")]}
    c = await _signed_in(db, google)
    await c._backfill(90)
    conv = (await db.execute(select(Conversation))).scalar_one()
    manager.register(c)
    try:
        r = await api.post("/api/compose/send", json={"conversation_id": conv.id, "text": "hi", "subject": "New subject"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert {p["role"] for p in body["message"]["participants"]} == {"from", "to"}

        win = (await api.get(f"/api/conversations/{conv.id}/messages/window")).json()
        assert all(m["participants"] for m in win["messages"])
        conv_payload = (await api.get(f"/api/conversations/{conv.id}")).json()
        assert conv_payload["labels"] == ["Inbox", "Sent"] and "subject" in conv_payload["capabilities"]
        assert "history_backfill" not in conv_payload["capabilities"]  # threads are synced whole
    finally:
        manager._connectors.pop(c.account_key, None)
        await c._api.close()
    raw = email.message_from_bytes(base64.urlsafe_b64decode(google.sent[0]["raw"]), policy=policy.default)
    assert raw["Subject"] == "New subject"


@pytest.mark.asyncio
async def test_fetch_attachment_recovers_from_a_reissued_attachment_id(db, google, unlocked_vault, broadcasts):
    from backend import config

    pdf = {"partId": "1", "mimeType": "application/pdf", "filename": "deck.pdf",
           "headers": [{"name": "Content-Disposition", "value": "attachment"}],
           "body": {"size": 5, "attachmentId": "OLD"}}
    google.threads = {"t1": [gmsg("m1", "t1", frm="ann@example.com", attachment=pdf)]}
    c = await _signed_in(db, google)
    await c._backfill(90)
    msg = (await db.execute(select(Message))).scalar_one()
    [att] = msg.attachments
    assert att.kind == "document" and att.external_ref == "1:OLD"

    # Gmail re-issued the id: the old one fails, the part is looked up again.
    google.threads["t1"][0]["payload"]["parts"][1]["body"]["attachmentId"] = "NEW"
    google.attachments[("m1", "NEW")] = b"%PDF!"
    conv = await crud.get_conversation(db, msg.conversation_id)
    rel = await c.fetch_attachment(att, msg, conv)
    assert rel == f"media/{conv.id}/{att.id}.pdf"
    assert (config.DATA_DIR / rel).read_bytes() == b"%PDF!"
    async with AsyncSessionLocal() as s:
        stored = await crud.get_message(s, msg.id)
        assert stored.attachments[0].local_path == rel
    await c._api.close()


# ── preferences ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_email_notifications_default_off_and_are_per_channel(api):
    from backend.api.settings import get_notif_pref

    assert get_notif_pref("gmail") == "off"
    assert get_notif_pref("telegram") == get_notif_pref() == "tracked"
    r = await api.post("/api/settings/prefs/notifications", json={"pref": "all", "channel": "gmail"})
    assert r.status_code == 200
    assert get_notif_pref("gmail") == "all" and get_notif_pref() == "tracked"
    r = await api.post("/api/settings/prefs/email-remote-images", json={"sender": "Ann@Example.com", "allowed": True})
    assert r.json()["senders"] == ["ann@example.com"]
    prefs = (await api.get("/api/settings/prefs")).json()
    assert prefs["notif_pref_channels"] == {"gmail": "all"}
    assert prefs["email_remote_images_senders"] == ["ann@example.com"]


@pytest.mark.asyncio
async def test_fanout_uses_the_channel_preference(db, broadcasts):
    from backend.ingest import fanout
    from backend.ingest.normalized import NormalizedConversation, NormalizedIdentity, NormalizedMessage
    from backend.ingest.service import ingest
    from datetime import datetime

    acc = Account(channel="gmail", external_id=ME)
    db.add(acc)
    await db.commit()
    with ExitStack() as stack:
        notify = stack.enter_context(patch.object(fanout, "send_native_notification"))
        await ingest(acc.id, "gmail", NormalizedConversation("t1", "email_thread", title="Hi"), [NormalizedMessage(
            "m1", datetime(2026, 1, 1), sender=NormalizedIdentity("ann@example.com"), text="x")], source="realtime")
    assert not notify.called  # email notifications are off by default


@pytest.mark.asyncio
async def test_start_backfills_then_polls_on_sync_now_and_stops(db, google, unlocked_vault, broadcasts):
    google.threads = {"t1": [gmsg("m1", "t1", frm="ann@example.com")]}
    c = await _signed_in(db, google)
    await c._api.close()
    c._api = None
    assert c.is_configured()
    assert await c.start() is True and await c.start() is True  # idempotent

    async def until(cond):
        for _ in range(200):
            if cond():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")

    await until(lambda: c.status().state == "ready")
    assert (await db.execute(select(Message.external_id))).scalars().all() == ["m1"]
    await c.sync_now()
    await until(lambda: "GET /gmail/v1/users/me/history" in google.calls)
    await c.stop()
    assert c.status().state == "stopped" and c._task is None

    await c.logout()
    assert not c.is_configured() and c.status().state == "unauthorized"
    assert vault.get_secret(f"gmail:{c.account_id}:client_id") == "cid"  # kept for re-auth
    assert google.token_requests[-1] == {"token": "rt-1"}  # revoked at Google
    assert await c.start() is False


@pytest.mark.asyncio
async def test_list_route_filters_by_kind_account_and_label(api, db, google, unlocked_vault, broadcasts):
    from tests import factories as f

    tg = await f.conversation(db, title="Telegram chat")
    google.threads = {
        "t1": [gmsg("m1", "t1", frm="ann@example.com", labels=("INBOX", "Label_1"))],
        "t3": [gmsg("m3", "t3", frm="bob@example.com", labels=("Label_1",))],  # archived, still labelled
    }
    c = await _signed_in(db, google)
    await c._backfill(90)
    await c._api.close()

    everything = {cv["title"] for cv in (await api.get("/api/conversations")).json()}
    assert everything == {"Telegram chat", "Plans"}  # no filter: unchanged inbox behaviour
    chats = (await api.get("/api/conversations", params={"exclude_kind": "email_thread"})).json()
    assert [cv["id"] for cv in chats] == [tg.id]
    mail = (await api.get("/api/conversations", params={"kind": "email_thread", "account_id": c.account_id})).json()
    assert [cv["title"] for cv in mail] == ["Plans"] and mail[0]["labels"] == ["Clients", "Inbox"]
    clients = (await api.get("/api/conversations", params={"label": "Clients"})).json()
    assert len(clients) == 2  # a label view includes archived threads
    other = (await api.get("/api/conversations", params={"account_id": c.account_id + 99})).json()
    assert other == []


@pytest.mark.asyncio
async def test_mail_routes_folders_labels_archive_read_and_compose(api, db, google, unlocked_vault, broadcasts):
    from backend.connectors.manager import manager

    google.threads = {
        "t1": [gmsg("m1", "t1", frm="Ann <ann@example.com>", labels=("INBOX", "UNREAD", "Label_1")),
               gmsg("m2", "t1", frm=ME, to="ann@example.com", labels=("SENT",), date_ms=1_767_000_100_000)],
        "t2": [gmsg("m3", "t2", frm="news@shop.example", subject="Sale", labels=("INBOX", "CATEGORY_PROMOTIONS"))],
    }
    c = await _signed_in(db, google)
    await c._backfill(90)
    manager.register(c)
    try:
        inbox = (await api.get("/api/connectors/gmail/threads", params={"account_id": c.account_id})).json()
        assert [t["title"] for t in inbox] == ["Plans"]
        assert inbox[0]["thread"]["message_count"] == 2 and inbox[0]["unread_count"] == 1
        assert [s["name"] for s in inbox[0]["thread"]["senders"]] == ["Ann", "me"]
        allmail = (await api.get("/api/connectors/gmail/threads", params={"folder": "all"})).json()
        assert {t["title"] for t in allmail} == {"Plans", "Sale"}
        archive = (await api.get("/api/connectors/gmail/threads", params={"folder": "archive"})).json()
        assert [t["title"] for t in archive] == ["Sale"]  # Promotions are kept out of the inbox
        sent = (await api.get("/api/connectors/gmail/threads", params={"folder": "sent"})).json()
        assert len(sent) == 1 and sent[0]["id"] == inbox[0]["id"]
        labels = (await api.get("/api/connectors/gmail/labels", params={"account_id": c.account_id})).json()
        assert {"label": "Clients", "count": 1} in labels and {"label": "Promotions", "count": 1} in labels

        tid = inbox[0]["id"]
        r = await api.post(f"/api/connectors/gmail/threads/{tid}/read", json={"read": True})
        assert r.status_code == 200 and r.json()["unread_count"] == 0
        assert google.modified[-1] == ("t1", {"addLabelIds": [], "removeLabelIds": ["UNREAD"]})
        r = await api.post(f"/api/connectors/gmail/threads/{tid}/archive", json={"archived": True})
        assert r.json()["is_archived"] is True and "Inbox" not in r.json()["labels"]
        assert (await api.get("/api/connectors/gmail/threads")).json() == []
        r = await api.post(f"/api/connectors/gmail/threads/{tid}/archive", json={"archived": False})
        assert r.json()["is_archived"] is False

        r = await api.post("/api/connectors/gmail/compose", data={
            "account_id": str(c.account_id), "subject": "Hello", "text": "First mail",
            "to": ["New@Example.com"], "cc": ["boss@example.com"],
        }, files=[("files", ("a.txt", b"hi", "text/plain"))])
        assert r.status_code == 200, r.text
        body = r.json()
        raw = email.message_from_bytes(base64.urlsafe_b64decode(google.sent[-1]["raw"]), policy=policy.default)
        assert "threadId" not in google.sent[-1] and raw["To"] == "new@example.com" and raw["Cc"] == "boss@example.com"
        assert raw["Subject"] == "Hello" and [a.get_filename() for a in raw.iter_attachments()] == ["a.txt"]
        assert body["conversation_id"] and body["message"]["is_outgoing"]

        # Explicit recipients replace the default reply-to-sender
        conv = await crud.get_conversation(db, tid)
        from backend.connectors.base import OutgoingMessage
        await c.send(conv, OutgoingMessage(text="re", to=["carol@example.com", "ann@example.com"]))
        raw = email.message_from_bytes(base64.urlsafe_b64decode(google.sent[-1]["raw"]), policy=policy.default)
        assert raw["To"] == "carol@example.com, ann@example.com"
    finally:
        manager._connectors.pop(c.account_key, None)
        await c._api.close()


@pytest.mark.asyncio
async def test_mail_routes_require_the_session_token(db_engine):
    from backend.api.router import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1") as anon:
        for method, path in (("GET", "/api/connectors/gmail/threads"), ("GET", "/api/connectors/gmail/labels"),
                             ("POST", "/api/connectors/gmail/threads/1/read")):
            r = await anon.request(method, path, json={"read": True})
            assert r.status_code == 401, (path, r.status_code)


# ── lifecycle and failure isolation ─────────────────────────────────────────────

def _live_tasks(name: str) -> list:
    from backend.tasks import _background_tasks
    return [t for t in _background_tasks if not t.done() and t.get_name() == name]


@pytest.mark.asyncio
async def test_concurrent_starts_run_one_sync_loop_that_stop_ends(db, google, unlocked_vault, broadcasts):
    c = await _signed_in(db, google)
    await c._api.close()
    c._api = None
    assert await asyncio.gather(c.start(), c.start()) == [True, True]
    mail, cal = f"gmail_sync:{c.account_id}", f"gcal_sync:{c.account_id}"
    assert len(_live_tasks(mail)) == 1 and len(_live_tasks(cal)) == 1
    await c.stop()
    await asyncio.sleep(0)
    assert _live_tasks(mail) == [] and _live_tasks(cal) == []


@pytest.mark.asyncio
async def test_stop_while_starting_leaves_nothing_running(db, google, unlocked_vault, broadcasts):
    # e.g. the vault locks while an unlock's start is still reading the DB
    c = await _signed_in(db, google)
    await c._api.close()
    c._api = None
    starting = asyncio.ensure_future(c.start())
    await asyncio.sleep(0)
    await c.stop()
    assert await starting is False
    await asyncio.sleep(0)
    assert _live_tasks(f"gmail_sync:{c.account_id}") == [] and _live_tasks(f"gcal_sync:{c.account_id}") == []
    assert c.status().state == "stopped" and c._api is None


@pytest.mark.asyncio
async def test_token_endpoint_outage_is_retried_not_a_lost_grant(db, google, unlocked_vault, broadcasts):
    c = await _signed_in(db, google)
    c._wake = asyncio.Event()
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        if len(delays) == 2:
            raise asyncio.CancelledError

    c._sleep = fake_sleep
    outage = iter([503, 429])

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(next(outage), text="Service Unavailable")
        return google.handler(request)

    c._api._token = None
    with patch.object(oauth, "transport", httpx.MockTransport(handler)):
        c._api._client = None
        with pytest.raises(asyncio.CancelledError):
            await c._run()
    assert c.status().state == "reconnecting" and "429" in c.status().error
    await c._api.close()


@pytest.mark.asyncio
async def test_token_errors_are_classified(google):
    creds = oauth.Credentials("cid", "secret", "rt")
    for status, body, expected in [
        (503, {}, oauth.OAuthUnreachable),
        (429, {"error": "rate_limit_exceeded"}, oauth.OAuthUnreachable),
        (400, {"error": "invalid_grant"}, oauth.OAuthError),
    ]:
        with patch.object(oauth, "transport", httpx.MockTransport(lambda r: httpx.Response(status, json=body))):
            with pytest.raises(expected) as info:
                await oauth.refresh(creds)
        assert info.type is expected
    from backend.connectors.gmail.api import GmailApi, GmailAuthError, GmailError

    for body, auth in [({"error": "invalid_grant"}, True), ({"error": "invalid_request"}, False)]:
        api = GmailApi(creds)
        with patch.object(oauth, "transport", httpx.MockTransport(lambda r: httpx.Response(400, json=body))):
            with pytest.raises(GmailError) as info:
                await api.profile()
        assert isinstance(info.value, GmailAuthError) is auth
        await api.close()


@pytest.mark.asyncio
async def test_retry_after_is_capped_at_the_backoff_max(db, google, unlocked_vault, broadcasts):
    from backend.connectors.gmail import connector as conn_mod

    c = await _signed_in(db, google)
    c._wake = asyncio.Event()
    delays = []

    async def fake_sleep(seconds):
        delays.append(seconds)
        raise asyncio.CancelledError

    c._sleep = fake_sleep

    def handler(request):
        if request.url.path.endswith("/profile"):
            return httpx.Response(429, headers={"Retry-After": "86400"}, json={"error": {"message": "quota"}})
        return google.handler(request)

    with patch.object(oauth, "transport", httpx.MockTransport(handler)):
        c._api._client = None
        with pytest.raises(asyncio.CancelledError):
            await c._run()
    assert delays == [conn_mod.BACKOFF_MAX]
    await c._api.close()


def _failing_thread(google, monkeypatch, *, status=400, slow=None):
    """Make thread ``bad`` fail with ``status`` while ``failing["on"]``; a
    ``slow`` thread answers late (records when it finished)."""
    failing = {"on": True, "slow_done": False}

    async def handler(request):
        if request.url.path.endswith("/threads/bad") and failing["on"]:
            return httpx.Response(status, json={"error": {"message": "Invalid id value"}})
        if slow and request.url.path.endswith(f"/threads/{slow}"):
            await asyncio.sleep(0.1)
            failing["slow_done"] = True
        return google.handler(request)

    monkeypatch.setattr(oauth, "transport", httpx.MockTransport(handler))
    return failing


@pytest.mark.asyncio
async def test_a_failing_thread_does_not_block_backfill_and_is_retried(db, google, unlocked_vault, broadcasts, monkeypatch):
    from backend.connectors.gmail.connector import load_sync_state

    google.threads = {"t0": [gmsg("m0", "t0", frm="ann@example.com")],
                      "bad": [gmsg("mb", "bad", frm="bob@example.com")]}
    failing = _failing_thread(google, monkeypatch)
    c = await _signed_in(db, google)
    await c._backfill(90)
    state = await load_sync_state(c.account_id)
    assert state["history_id"] == "1000" and set(state["failed_threads"]) == {"bad"}
    assert (await db.execute(select(Message.external_id))).scalars().all() == ["m0"]

    failing["on"] = False  # the next poll retries it
    await c._incremental(state)
    state = await load_sync_state(c.account_id)
    assert state["failed_threads"] == {}
    assert sorted((await db.execute(select(Message.external_id))).scalars().all()) == ["m0", "mb"]
    await c._api.close()


@pytest.mark.asyncio
async def test_a_failing_thread_does_not_freeze_the_history_cursor(db, google, unlocked_vault, broadcasts, monkeypatch):
    from backend.connectors.gmail.connector import load_sync_state, merge_sync_state

    google.threads = {"t0": [gmsg("m0", "t0", frm="ann@example.com")]}
    google.history_pages = [{"historyId": "2000", "history": [
        {"messagesAdded": [{"message": {"id": "x", "threadId": "bad"}}]},
        {"messagesAdded": [{"message": {"id": "m0", "threadId": "t0"}}]},
    ]}]
    _failing_thread(google, monkeypatch)
    c = await _signed_in(db, google)
    await merge_sync_state(c.account_id, history_id="1000")
    await c._incremental(await load_sync_state(c.account_id))
    state = await load_sync_state(c.account_id)
    assert state["history_id"] == "2000" and set(state["failed_threads"]) == {"bad"}
    assert (await db.execute(select(Message.external_id))).scalars().all() == ["m0"]
    await c._api.close()


@pytest.mark.asyncio
async def test_a_retryable_thread_error_still_waits_for_the_other_threads(db, google, unlocked_vault, broadcasts, monkeypatch):
    from backend.connectors.gmail.api import GmailError

    google.threads = {"slow": [gmsg("ms", "slow", frm="ann@example.com")],
                      "bad": [gmsg("mb", "bad", frm="bob@example.com")]}
    failing = _failing_thread(google, monkeypatch, status=503, slow="slow")
    c = await _signed_in(db, google)
    with pytest.raises(GmailError):
        await c._backfill(90)
    assert failing["slow_done"]  # no fetch left running behind the failed pass
    await c._api.close()


# ── sign-in callback and compose races ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_oauth_callbacks_exchange_the_code_once(google):
    p = oauth.start("flow-race", "cid", "secret")
    calls = []

    async def slow_token(request):
        calls.append(request)
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600, "refresh_token": "rt"})

    with patch.object(oauth, "transport", httpx.MockTransport(slow_token)):
        first = asyncio.ensure_future(oauth.complete(p.state, "code", None))
        await asyncio.sleep(0)
        # a reload of the callback page, and a stale "denied" from another tab
        results = await asyncio.gather(oauth.complete(p.state, "code", None),
                                       oauth.complete(p.state, None, "access_denied"))
        first.cancel()  # the browser went away: the exchange still finishes
        await asyncio.sleep(0.1)
    assert len(calls) == 1
    assert results == [p, p] and p.access_token == "at" and p.refresh_token == "rt" and p.error is None
    assert (await oauth.complete(p.state, None, "access_denied")) is p and p.error is None


@pytest.mark.asyncio
async def test_compose_new_keeps_files_when_the_request_is_cancelled(db, monkeypatch):
    import io

    from fastapi import UploadFile

    from backend.connectors.gmail import routes

    seen = {}
    started, finished = asyncio.Event(), asyncio.Event()

    class _Connector:
        async def send_new(self, draft):
            started.set()
            await asyncio.sleep(0.05)
            seen["paths"] = [f.path for f in draft.files]
            seen["exists"] = [os.path.exists(f.path) for f in draft.files]
            finished.set()
            return 1, []

    monkeypatch.setattr(routes, "_connector", lambda account_id: _Connector())
    request = asyncio.ensure_future(routes.compose_new(
        account_id=1, text="hi", subject="Files", to=["ann@example.com"], cc=[], client_nonce="nonce-mail-cancel",
        files=[UploadFile(io.BytesIO(b"data"), filename="a.pdf")],
    ))
    await started.wait()
    request.cancel()  # the client went away; the shielded send carries on
    await asyncio.wait_for(finished.wait(), 2)
    await asyncio.sleep(0.01)
    assert seen["exists"] == [True]
    assert not any(os.path.exists(p) for p in seen["paths"])  # cleaned up by the send


@pytest.mark.asyncio
async def test_removing_an_account_collects_its_correspondents_but_keeps_annotated_people(
    db, google, unlocked_vault, broadcasts,
):
    from backend.connectors.manager import ConnectorManager
    from backend.db.models import Identity, Person, PersonNote

    google.threads = {"t1": [gmsg("m1", "t1", frm="ann@example.com")],
                      "t2": [gmsg("m2", "t2", frm="bob@example.com")]}
    c = await _signed_in(db, google)
    await c._backfill(90)
    bob = (await db.execute(select(Identity).where(Identity.external_id == "bob@example.com"))).scalar_one()
    ann = (await db.execute(select(Identity).where(Identity.external_id == "ann@example.com"))).scalar_one()
    bob_person, ann_person = bob.person_id, ann.person_id
    db.add(PersonNote(person_id=bob_person, kind="context", title="Met at the conference", body=""))
    await db.commit()

    m = ConnectorManager()
    m.register(c)
    assert await m.remove(c.account_key) is True
    async with AsyncSessionLocal() as s:
        emails = set((await s.execute(select(Identity.external_id).where(Identity.channel == "gmail"))).scalars())
        assert not {"ann@example.com", "bob@example.com"} & emails
        assert await s.get(Person, ann_person) is None  # nothing but this account's mail
        assert await s.get(Person, bob_person) is not None  # the user wrote a note about Bob
