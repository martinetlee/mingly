"""Scam service + queue: status model, failure handling, targets vs context,
terminal WS events, sender dismissal and the bounded/deduped queue."""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.db import crud, database
from backend.db.models import Conversation, Message, ScamCheckRequest, ScamFlag
from backend.scam import queue as scan_queue
from backend.scam import service as service_mod
from backend.scam.client import ScamAPIClient, ScamCheckResult
from backend.ws.manager import ws_manager
from tests import factories as f

CHAT = "-100500"  # the conversation's external (peer) id


class FakeClient(ScamAPIClient):
    def __init__(self, results=None, delay=0.0):
        self.results = list(results or [])
        self.requests = []
        self.flag_lookups = []
        self.flagged = {}
        self.delay = delay

    async def are_flagged_scams(self, telegram_ids):
        self.flag_lookups.append(list(telegram_ids))
        return {t: self.flagged[t] for t in telegram_ids if t in self.flagged}

    async def is_this_scam(self, request_id, req):
        self.requests.append(req)
        if self.delay:
            await asyncio.sleep(self.delay)
        res = self.results.pop(0) if self.results else ScamCheckResult(is_scam=False, confidence=0.9, reason="fine")
        if isinstance(res, Exception):
            raise res
        return res

    async def get_result(self, request_id):
        return None

    async def respond_to_followup(self, request_id, data):
        return None


@pytest.fixture
def events(monkeypatch):
    captured = []

    async def _broadcast(event):
        # Terminal events must be observable in the DB when they are sent
        if event.get("type") in ("scam_detected", "scam_check_safe", "scam_check_error"):
            async with database.AsyncSessionLocal() as s:
                for entry in event.get("messages", []):
                    row = await s.get(Message, entry["message_id"])
                    assert row.conversation_id == entry["conversation_id"] == event["conversation_id"]
                    assert row.sender_identity_id == entry["identity_id"]
                    assert row.scam_status == entry["scam_status"]
                    assert row.scam_score == entry["scam_score"]
        captured.append(event)

    monkeypatch.setattr(ws_manager, "broadcast", _broadcast)
    return captured


@pytest.fixture
def client(db_engine, monkeypatch):
    fake = FakeClient()
    service_mod.init_scam_service(fake)
    yield fake
    monkeypatch.setattr(service_mod, "_service", None)


async def _seed(db, *msgs, kind="group", channel="telegram"):
    """msgs: (seq, sender external id, text). Returns the conversation."""
    conv = await f.conversation(db, CHAT, title="G", kind=kind, channel=channel)
    now = datetime.utcnow()
    for i, (seq, sender, text) in enumerate(msgs):
        ident = await f.identity(db, sender, channel=channel, display_name=f"u{sender}")
        await f.message(db, conv, seq, text, sender=ident, date=now - timedelta(minutes=len(msgs) - i))
    return conv


async def _conv_id():
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(select(Conversation.id).where(Conversation.external_id == CHAT))).scalar_one()


async def _mid(seq):
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(select(Message.id).where(Message.external_id == str(seq)))).scalar_one()


async def _identity_id(external_id, channel="telegram"):
    async with database.AsyncSessionLocal() as s:
        return (await crud.get_identity_by_external(s, channel, str(external_id))).id


async def _row(seq):
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(select(Message).where(Message.external_id == str(seq)))).scalars().first()


async def _enqueue(seqs, source, q=None):
    return await (q or scan_queue.get_scan_queue()).enqueue(await _conv_id(), [await _mid(x) for x in seqs], source)


async def _check(seq, source="manual"):
    await scan_queue.enqueue_targets(await _conv_id(), [await _mid(seq)], source=source)
    await scan_queue.get_scan_queue().join()


async def _seqs(requests):
    """Target seqs of the requests the client saw."""
    async with database.AsyncSessionLocal() as s:
        by_id = dict((await s.execute(select(Message.id, Message.seq))).all())
    return [[by_id[i] for i in r.target_ids] for r in requests]


def _terminal(events):
    return [e for e in events if e["type"] in ("scam_detected", "scam_check_safe", "scam_check_error")]


@pytest.mark.asyncio
async def test_failure_never_touches_score_and_emits_only_error(db, client, events):
    await _seed(db, (1, 10, "hello"))
    client.results = [ScamCheckResult(is_scam=False, confidence=0.0, reason="", error="Node 'classify' failed: x")]
    await _check(1)

    row = await _row(1)
    assert row.scam_status == "failed"
    assert row.scam_score is None and row.scam_reason is None
    assert row.scam_error.startswith("Node 'classify'")
    assert row.scam_attempts == 1 and row.scam_next_retry_at > datetime.utcnow()
    assert not row.effective_is_scam
    term = _terminal(events)
    assert [e["type"] for e in term] == ["scam_check_error"]
    assert term[0]["messages"][0]["scam_status"] == "failed"


@pytest.mark.asyncio
async def test_failed_rescan_keeps_earlier_good_result(db, client, events):
    await _seed(db, (1, 10, "send me btc"))
    client.results = [
        ScamCheckResult(is_scam=True, confidence=0.8, reason="crypto ask", category="investment_scam"),
        ScamCheckResult(is_scam=False, confidence=0.0, reason="", error="timeout"),
    ]
    await _check(1)
    await _check(1)
    row = await _row(1)
    assert row.scam_status == "completed" and row.scam_score == pytest.approx(0.9)
    assert row.scam_reason == "crypto ask" and row.scam_error == "timeout"
    assert row.effective_is_scam
    assert [e["type"] for e in _terminal(events)] == ["scam_detected", "scam_check_error"]


@pytest.mark.asyncio
async def test_exception_in_client_is_a_failure_with_one_terminal_event(db, client, events):
    await _seed(db, (1, 10, "hi"))
    client.results = [RuntimeError("ollama down")]
    await _check(1)
    row = await _row(1)
    assert row.scam_status == "failed" and row.scam_score is None
    assert [e["type"] for e in _terminal(events)] == ["scam_check_error"]
    async with database.AsyncSessionLocal() as s:
        req = (await s.execute(select(ScamCheckRequest))).scalars().one()
    assert req.status == "failed"
    assert "messages" not in req.payload_json  # compact payload


@pytest.mark.asyncio
async def test_only_target_scored_and_only_target_sender_flagged(db, client, events):
    # Context from the same sender exists; a bystander posted in between
    await _seed(db, (1, 10, "earlier context"), (2, 20, "bystander"), (3, 10, "send 1 BTC to double it"))
    client.results = [ScamCheckResult(is_scam=True, confidence=1.0, reason="doubling", category="investment_scam")]
    await _check(3)

    req = client.requests[0]
    assert req.target_ids == [await _mid(3)]
    assert [m["message_id"] for m in req.context] == [await _mid(1)]
    assert (await _row(3)).scam_score == 1.0
    assert (await _row(1)).scam_status is None and (await _row(1)).scam_score is None
    sender = await _identity_id(10)
    async with database.AsyncSessionLocal() as s:
        flags = (await s.execute(select(ScamFlag))).scalars().all()
    assert [fl.identity_id for fl in flags] == [sender]
    assert flags[0].flagged_message_id == await _mid(3) and flags[0].flagged_conversation_id == await _conv_id()
    term = _terminal(events)
    assert term[0]["type"] == "scam_detected" and term[0]["message_ids"] == [await _mid(3)]
    assert term[0]["conversation_id"] == await _conv_id() and term[0]["identity_ids"] == [sender]
    assert [e["identity_ids"] for e in events if e["type"] == "scam_users_flagged"] == [[sender]]
    assert term[0]["messages"][0]["effective_is_scam"] is True
    assert term[0]["messages"][0]["scam_score"] == 1.0  # stored score, not raw confidence


@pytest.mark.asyncio
async def test_dismissed_sender_is_not_reflagged(db, client, events):
    from backend.scam.verdict import set_verdict

    await _seed(db, (1, 10, "scam one"), (2, 10, "scam two"))
    client.results = [ScamCheckResult(is_scam=True, confidence=1.0, reason="x")] * 2
    await _check(1)
    sender = await _identity_id(10)
    await set_verdict(scope="sender", verdict="not_scam", identity_id=sender)
    mark = len(events)
    await _check(2)
    async with database.AsyncSessionLocal() as s:
        flag = (await s.execute(select(ScamFlag).where(ScamFlag.identity_id == sender))).scalars().one()
    assert flag.status == "dismissed"
    assert not [e for e in events[mark:] if e["type"] == "scam_users_flagged"]
    assert (await _row(2)).effective_is_scam  # the new message itself still shows its own verdict


@pytest.mark.asyncio
async def test_pipeline_rescore_never_overwrites_manual_verdict(db, client, events):
    from backend.scam.verdict import set_verdict

    await _seed(db, (1, 10, "hi"))
    await set_verdict(scope="message", verdict="scam", messages=[await _mid(1)])
    client.results = [ScamCheckResult(is_scam=False, confidence=0.9, reason="fine")]
    await _check(1)
    row = await _row(1)
    assert row.scam_manual_verdict == "scam"
    assert row.scam_status == "completed" and row.scam_score < 0.5
    assert row.effective_is_scam  # manual wins
    assert _terminal(events)[0]["type"] == "scam_detected"


@pytest.mark.asyncio
async def test_queue_dedupes_and_bounds_concurrency(db, client, events, monkeypatch):
    await _seed(db, (1, 10, "a"), (2, 11, "b"))
    client.delay = 0.05
    active = {"n": 0, "max": 0}
    orig = client.is_this_scam

    async def tracking(request_id, req):
        active["n"] += 1
        active["max"] = max(active["max"], active["n"])
        try:
            return await orig(request_id, req)
        finally:
            active["n"] -= 1

    monkeypatch.setattr(client, "is_this_scam", tracking)
    await _enqueue([1, 2, 1], "scan")
    await _enqueue([1], "realtime")  # already queued
    assert (await _row(1)).scam_status == "pending"
    await scan_queue.get_scan_queue().join()
    assert sorted(await _seqs(client.requests)) == [[1], [2]]
    assert active["max"] == 1  # default 1 worker
    assert len(_terminal(events)) == 2


@pytest.mark.asyncio
async def test_unjudgeable_target_still_gets_terminal_event(db, client, events):
    await _seed(db, (1, 10, ""))
    await _check(1)
    assert [e["type"] for e in _terminal(events)] == ["scam_check_error"]
    assert (await _row(1)).scam_status is None


@pytest.mark.asyncio
async def test_scan_unscanned_picks_due_failures_and_skips_done(db, client, events):
    conv = await _seed(db, (1, 10, "never"), (2, 10, "failed due"), (3, 10, "failed backoff"),
                       (4, 10, "done"), (5, 10, "manual"), (6, 10, "failed too often"))
    await f.message(db, conv, 999, "old", date=datetime.utcnow() - timedelta(days=10))  # not a first sync
    async with database.AsyncSessionLocal() as s:
        rows = {m.seq: m for m in (await s.execute(select(Message))).scalars()}
        past, future = datetime.utcnow() - timedelta(minutes=1), datetime.utcnow() + timedelta(hours=1)
        rows[2].scam_status, rows[2].scam_attempts, rows[2].scam_next_retry_at = "failed", 1, past
        rows[3].scam_status, rows[3].scam_attempts, rows[3].scam_next_retry_at = "failed", 1, future
        rows[4].scam_status, rows[4].scam_score = "completed", 0.1
        rows[5].scam_manual_verdict = "not_scam"
        rows[6].scam_status, rows[6].scam_attempts = "failed", service_mod.MAX_AUTO_ATTEMPTS
        await s.commit()

    n = await scan_queue.scan_unscanned_messages(max_messages=50, max_age_hours=48)
    await scan_queue.get_scan_queue().join()
    assert n == 2
    assert sorted(await _seqs(client.requests)) == [[1], [2]]


@pytest.mark.asyncio
async def test_resume_requeues_pending_requests(db, client, events):
    import json
    conv = await _seed(db, (1, 10, "hi"))
    db.add(ScamCheckRequest(request_id="r1", request_type="is_this_scam", status="pending",
                            payload_json="{}", related_conversation_id=conv.id,
                            related_message_ids_json=json.dumps([await _mid(1)]),
                            created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
    await db.commit()
    await service_mod.get_scam_service().resume_pending_requests()
    await scan_queue.get_scan_queue().join()
    assert await _seqs(client.requests) == [[1]]
    async with database.AsyncSessionLocal() as s:
        old = (await s.execute(select(ScamCheckRequest).where(ScamCheckRequest.request_id == "r1"))).scalars().one()
    assert old.status == "superseded"


@pytest.mark.asyncio
async def test_edit_source_rejudges_completed_message(db, client, events):
    await _seed(db, (1, 10, "hello"))
    client.results = [ScamCheckResult(is_scam=False, confidence=0.9, reason="fine"),
                      ScamCheckResult(is_scam=True, confidence=0.9, reason="now a scam link")]
    await _check(1)
    await _check(1, source="edit")
    await _check(1, source="something-new")  # unknown sources are accepted
    row = await _row(1)
    assert row.scam_reason in ("now a scam link", "fine") and len(client.requests) == 3
    assert [e["source"] for e in _terminal(events)] == ["manual", "edit", "something-new"]


@pytest.mark.asyncio
async def test_stale_pending_rows_are_reclaimed_on_queue_start(db, client, events):
    await _seed(db, (1, 10, "left pending, never tried"), (2, 10, "left pending after a failure"),
                (3, 10, "pending, out of attempts"))
    async with database.AsyncSessionLocal() as s:
        rows = {m.seq: m for m in (await s.execute(select(Message))).scalars()}
        rows[1].scam_status = "pending"
        rows[2].scam_status, rows[2].scam_attempts = "pending", 1
        rows[3].scam_status, rows[3].scam_attempts = "pending", service_mod.MAX_AUTO_ATTEMPTS
        await s.commit()

    q = scan_queue._ScanQueue()  # a fresh process
    await q.enqueue(await _conv_id(), [], "scan")  # first use triggers the reclaim
    await q.join()
    assert sorted(await _seqs(client.requests)) == [[1], [2]]
    assert (await _row(3)).scam_status == "failed"  # released, not re-run
    assert (await _row(1)).scam_status == "completed"


@pytest.mark.asyncio
async def test_full_queue_never_drops_manual_and_reports_drops(db, client, events, monkeypatch):
    await _seed(db, (1, 10, "a"), (2, 10, "b"), (3, 10, "c"))
    monkeypatch.setattr(scan_queue, "MAX_QUEUE", 1)
    gate = asyncio.Event()
    orig = client.is_this_scam

    async def gated(request_id, req):
        await gate.wait()
        return await orig(request_id, req)

    monkeypatch.setattr(client, "is_this_scam", gated)
    q = scan_queue.get_scan_queue()
    await _enqueue([1], "scan")
    await asyncio.sleep(0.01)  # worker takes 1 (running), queue empty
    await _enqueue([2], "scan")  # fills the single slot
    await _enqueue([3], "realtime")  # dropped → terminal error
    dropped = [e for e in events if e["type"] == "scam_check_error"]
    assert dropped and dropped[0]["message_ids"] == [await _mid(3)]
    assert dropped[0]["conversation_id"] == await _conv_id() and dropped[0]["identity_ids"] == []
    added = await _enqueue([3], "manual")  # over the limit but manual
    assert added == [await _mid(3)]
    gate.set()
    await q.join()
    assert sorted(await _seqs(client.requests)) == [[1], [2], [3]]


@pytest.mark.asyncio
async def test_edit_during_scan_reruns_once_with_new_text(db, client, events, monkeypatch):
    await _seed(db, (1, 10, "old text"))
    started, release = asyncio.Event(), asyncio.Event()
    orig = client.is_this_scam

    async def slow(request_id, req):
        if not started.is_set():
            started.set()
            await release.wait()
        return await orig(request_id, req)

    monkeypatch.setattr(client, "is_this_scam", slow)
    q = scan_queue.get_scan_queue()
    await _enqueue([1], "realtime")
    await started.wait()
    async with database.AsyncSessionLocal() as s:
        m = (await s.execute(select(Message).where(Message.seq == 1))).scalars().one()
        m.text = "new text with a scam link"
        await s.commit()
    await _enqueue([1], "edit")
    await _enqueue([1], "edit")  # coalesced
    release.set()
    await q.join()
    await q.join()
    assert [r.targets[0]["text"] for r in client.requests] == ["old text", "new text with a scam link"]
    assert [e["source"] for e in _terminal(events)] == ["realtime", "edit"]


# ---------------------------------------------------------------------------
# Channels: identities, Telegram user ids, conversation kind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_carries_channel_kind_and_telegram_user_ids(db, client, events):
    await _seed(db, (1, 10, "earlier"), (2, 10, "target"), kind="direct")
    await _check(2)
    req = client.requests[0]
    sender = await _identity_id(10)
    assert req.channel == "telegram" and req.conversation_kind == "direct"
    assert req.metadata == {"conversation_id": await _conv_id(), "title": "G", "kind": "direct", "channel": "telegram"}
    assert req.targets[0]["identity_id"] == sender and req.targets[0]["telegram_user_id"] == 10
    assert req.context[0]["telegram_user_id"] == 10
    assert req.profiles[0]["identity_id"] == sender and req.profiles[0]["telegram_user_id"] == 10
    assert req.target_identity_ids == [sender]


@pytest.mark.asyncio
async def test_non_telegram_request_has_no_telegram_ids(db, client, events):
    await _seed(db, (1, "eve@example.com", "wire me money"), kind="email_thread", channel="gmail")
    client.results = [ScamCheckResult(is_scam=True, confidence=1.0, reason="x")]
    await _check(1)
    req = client.requests[0]
    assert req.channel == "gmail" and req.conversation_kind == "email_thread"
    assert "telegram_user_id" not in req.targets[0]
    assert client.flag_lookups == []  # the Telegram-id lookup is skipped
    async with database.AsyncSessionLocal() as s:
        flags = (await s.execute(select(ScamFlag))).scalars().all()
    assert [fl.identity_id for fl in flags] == [await _identity_id("eve@example.com", "gmail")]


@pytest.mark.asyncio
async def test_sender_profile_hints_override_the_stored_identity(db, client, events):
    await _seed(db, (1, 10, "hi"))
    sender = await _identity_id(10)
    scan_queue.note_sender_profile(sender, bio="crypto coach", is_contact=True)
    try:
        await _check(1)
    finally:
        scan_queue._profile_hints.clear()
    assert client.requests[0].profiles[0]["bio"] == "crypto coach"
    assert client.requests[0].profiles[0]["is_contact"] is True


@pytest.mark.asyncio
async def test_check_flagged_users_translates_identities(db, client, events):
    tg = await f.identity(db, 42, display_name="Mallory")
    mail = await f.identity(db, "m@example.com", channel="gmail")
    client.flagged = {42: ScamCheckResult(is_scam=True, confidence=1.0, reason="known")}
    await service_mod.get_scam_service().check_flagged_users([tg.id, mail.id])
    assert client.flag_lookups == [[42]]
    async with database.AsyncSessionLocal() as s:
        flags = (await s.execute(select(ScamFlag))).scalars().all()
        req = (await s.execute(select(ScamCheckRequest))).scalars().one()
    assert [(fl.identity_id, fl.source) for fl in flags] == [(tg.id, "cloud")]
    assert req.status == "completed" and req.related_identity_ids_json == f"[{tg.id}, {mail.id}]"
    assert [e for e in events if e["type"] == "scam_users_flagged"] == [
        {"type": "scam_users_flagged", "identity_ids": [tg.id]}]


@pytest.mark.asyncio
async def test_scans_count_by_conversation_kind(db, client, events):
    from backend.db.models import ScamAnalytics

    await _seed(db, (1, 10, "hi"), kind="direct")
    await _check(1)
    async with database.AsyncSessionLocal() as s:
        row = (await s.execute(select(ScamAnalytics))).scalars().one()
    assert (row.scans_direct, row.scans_group, row.scans_channel) == (1, 0, 0)


@pytest.mark.asyncio
async def test_unscanned_scan_enqueues_only_recent_incoming_text(db, monkeypatch):
    """Old rows, service lines and outgoing messages are never scanned."""
    now = datetime.utcnow()
    conv = await f.conversation(db, "1501", title="Pia")
    pia = await f.identity(db, "1501")
    await f.message(db, conv, 1, "old", sender=pia, date=now - timedelta(days=5))
    await f.message(db, conv, 2, "Pia joined Telegram", kind="service", date=now - timedelta(hours=1))
    await f.message(db, conv, 3, "Missed call", kind="service", sender=pia, date=now - timedelta(hours=1))
    await f.message(db, conv, 4, "my reply", out=True, date=now - timedelta(hours=1))
    target = await f.message(db, conv, 5, "send me your seed phrase", sender=pia, date=now - timedelta(hours=1))
    queued = []

    async def enqueue(conversation_id, ids, source):
        queued.append((conversation_id, list(ids), source))
        return ids

    monkeypatch.setattr(service_mod, "get_scam_service", lambda: object())
    monkeypatch.setattr(scan_queue._scan_queue, "enqueue", enqueue)
    assert await scan_queue.scan_unscanned_messages(max_messages=200, max_age_hours=48) == 1
    assert queued == [(conv.id, [target.id], "scan")]
