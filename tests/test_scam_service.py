"""Scam service + queue: status model, failure handling, targets vs context,
terminal WS events, sender dismissal and the bounded/deduped queue."""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.db import database
from backend.db.models import Chat, Message, ScamCheckRequest, ScamFlag
from backend.scam import queue as scan_queue
from backend.scam import service as service_mod
from backend.scam.client import ScamAPIClient, ScamCheckResult
from backend.ws.manager import ws_manager

CHAT = -100500


class FakeClient(ScamAPIClient):
    def __init__(self, results=None, delay=0.0):
        self.results = list(results or [])
        self.requests = []
        self.delay = delay

    async def are_flagged_scams(self, telegram_ids):
        return {}

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
                    row = (await s.execute(select(Message).where(
                        Message.chat_id == entry["chat_id"], Message.tg_message_id == entry["tg_message_id"]
                    ))).scalars().first()
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


async def _seed(db, *msgs, chat_type="group"):
    db.add(Chat(telegram_id=CHAT, name="G", type=chat_type))
    now = datetime.utcnow()
    for i, (tg_id, sender, text) in enumerate(msgs):
        db.add(Message(tg_message_id=tg_id, chat_id=CHAT, sender_id=sender, sender_name=f"u{sender}",
                       text=text, date=now - timedelta(minutes=len(msgs) - i)))
    await db.commit()


async def _row(tg_id):
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(select(Message).where(
            Message.chat_id == CHAT, Message.tg_message_id == tg_id))).scalars().first()


async def _check(tg_id, source="manual"):
    await scan_queue.enqueue_targets(CHAT, [tg_id], source=source)
    await scan_queue.get_scan_queue().join()


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
    assert req.target_ids == [3]
    assert [m["message_id"] for m in req.context] == [1]
    assert (await _row(3)).scam_score == 1.0
    assert (await _row(1)).scam_status is None and (await _row(1)).scam_score is None
    async with database.AsyncSessionLocal() as s:
        flags = (await s.execute(select(ScamFlag))).scalars().all()
    assert [f.telegram_id for f in flags] == [10]
    assert flags[0].flagged_message_id == 3
    term = _terminal(events)
    assert term[0]["type"] == "scam_detected" and term[0]["message_ids"] == [3]
    assert term[0]["messages"][0]["effective_is_scam"] is True
    assert term[0]["messages"][0]["scam_score"] == 1.0  # stored score, not raw confidence


@pytest.mark.asyncio
async def test_dismissed_sender_is_not_reflagged(db, client, events):
    from backend.scam.verdict import set_verdict

    await _seed(db, (1, 10, "scam one"), (2, 10, "scam two"))
    client.results = [ScamCheckResult(is_scam=True, confidence=1.0, reason="x")] * 2
    await _check(1)
    await set_verdict(scope="sender", verdict="not_scam", telegram_id=10)
    mark = len(events)
    await _check(2)
    async with database.AsyncSessionLocal() as s:
        flag = (await s.execute(select(ScamFlag).where(ScamFlag.telegram_id == 10))).scalars().one()
    assert flag.status == "dismissed"
    assert not [e for e in events[mark:] if e["type"] == "scam_users_flagged"]
    assert (await _row(2)).effective_is_scam  # the new message itself still shows its own verdict


@pytest.mark.asyncio
async def test_pipeline_rescore_never_overwrites_manual_verdict(db, client, events):
    from backend.scam.verdict import set_verdict

    await _seed(db, (1, 10, "hi"))
    await set_verdict(scope="message", verdict="scam", messages=[(CHAT, 1)])
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
    await scan_queue.enqueue_targets(CHAT, [1, 2, 1], source="scan")
    await scan_queue.enqueue_targets(CHAT, [1], source="realtime")  # already queued
    assert (await _row(1)).scam_status == "pending"
    await scan_queue.get_scan_queue().join()
    assert sorted(r.target_ids[0] for r in client.requests) == [1, 2]
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
    old = datetime.utcnow() - timedelta(days=10)
    db.add(Message(tg_message_id=999, chat_id=CHAT, text="old", date=old))  # not a first sync
    await _seed(db, (1, 10, "never"), (2, 10, "failed due"), (3, 10, "failed backoff"),
                (4, 10, "done"), (5, 10, "manual"), (6, 10, "failed too often"))
    async with database.AsyncSessionLocal() as s:
        rows = {m.tg_message_id: m for m in (await s.execute(select(Message))).scalars()}
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
    assert sorted(r.target_ids[0] for r in client.requests) == [1, 2]


@pytest.mark.asyncio
async def test_resume_requeues_pending_requests(db, client, events):
    import json
    await _seed(db, (1, 10, "hi"))
    db.add(ScamCheckRequest(request_id="r1", request_type="is_this_scam", status="pending",
                            payload_json="{}", related_chat_id=CHAT, related_message_ids_json=json.dumps([1]),
                            created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
    await db.commit()
    await service_mod.get_scam_service().resume_pending_requests()
    await scan_queue.get_scan_queue().join()
    assert [r.target_ids for r in client.requests] == [[1]]
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
        rows = {m.tg_message_id: m for m in (await s.execute(select(Message))).scalars()}
        rows[1].scam_status = "pending"
        rows[2].scam_status, rows[2].scam_attempts = "pending", 1
        rows[3].scam_status, rows[3].scam_attempts = "pending", service_mod.MAX_AUTO_ATTEMPTS
        await s.commit()

    q = scan_queue._ScanQueue()  # a fresh process
    await q.enqueue(CHAT, [], "scan")  # first use triggers the reclaim
    await q.join()
    assert sorted(r.target_ids[0] for r in client.requests) == [1, 2]
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
    await q.enqueue(CHAT, [1], "scan")
    await asyncio.sleep(0.01)  # worker takes 1 (running), queue empty
    await q.enqueue(CHAT, [2], "scan")  # fills the single slot
    await q.enqueue(CHAT, [3], "realtime")  # dropped → terminal error
    dropped = [e for e in events if e["type"] == "scam_check_error"]
    assert dropped and dropped[0]["message_ids"] == [3]
    added = await q.enqueue(CHAT, [3], "manual")  # over the limit but manual
    assert added == [3]
    gate.set()
    await q.join()
    assert sorted(r.target_ids[0] for r in client.requests) == [1, 2, 3]


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
    await q.enqueue(CHAT, [1], "realtime")
    await started.wait()
    async with database.AsyncSessionLocal() as s:
        m = (await s.execute(select(Message).where(Message.tg_message_id == 1))).scalars().one()
        m.text = "new text with a scam link"
        await s.commit()
    await q.enqueue(CHAT, [1], "edit")
    await q.enqueue(CHAT, [1], "edit")  # coalesced
    release.set()
    await q.join()
    await q.join()
    assert [r.targets[0]["text"] for r in client.requests] == ["old text", "new text with a scam link"]
    assert [e["source"] for e in _terminal(events)] == ["realtime", "edit"]
