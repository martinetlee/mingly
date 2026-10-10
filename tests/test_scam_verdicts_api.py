"""Verdict model through the API: one set_verdict for every entry point,
effective_is_scam everywhere, keyset pagination, status and export."""

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.db import crud, database
from backend.db.models import Message, ScamDismissal, ScamFlag
from backend.ws.manager import ws_manager
from tests import factories as f

CHAT = "-100700"  # the conversation's external (peer) id


@pytest.fixture(autouse=True)
def _quiet_ws(monkeypatch):
    sent = []

    async def _broadcast(event):
        sent.append(event)

    monkeypatch.setattr(ws_manager, "broadcast", _broadcast)
    return sent


async def _seed(db, rows):
    """rows: (seq, sender external id, status, score, manual). Returns the conversation."""
    conv = await f.conversation(db, CHAT, title="G", kind="group", members_count=50)
    base = datetime.utcnow() - timedelta(hours=1)
    for i, (seq, sender, status, score, manual) in enumerate(rows):
        ident = await f.identity(db, sender, display_name=f"u{sender}")
        await f.message(db, conv, seq, f"m{seq}", sender=ident,
                        date=base + timedelta(seconds=i // 2),  # duplicate dates on purpose
                        scam_status=status, scam_score=score, scam_manual_verdict=manual, scam_updated_at=base)
    return conv


async def _get(seq):
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(select(Message).where(Message.external_id == str(seq)))).scalars().one()


async def _mid(seq):
    return (await _get(seq)).id


async def _sender(external_id):
    async with database.AsyncSessionLocal() as s:
        return await crud.get_identity_by_external(s, "telegram", str(external_id))


async def _flag(identity_id):
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(select(ScamFlag).where(ScamFlag.identity_id == identity_id))).scalars().first()


async def _seqs(messages, key="message_id"):
    async with database.AsyncSessionLocal() as s:
        by_id = dict((await s.execute(select(Message.id, Message.seq))).all())
    return sorted(by_id[m[key]] for m in messages)


def test_effective_is_scam_python_and_sql_agree():
    cases = [
        (None, "completed", 0.9, True), (None, "completed", 0.2, False), (None, "failed", 0.9, False),
        (None, "short_circuited", 0.95, True), ("not_scam", "completed", 0.9, False),
        ("scam", "completed", 0.1, True), ("scam", None, None, True), (None, None, None, False),
        (None, "pending", 0.9, False),
    ]
    for manual, status, score, expected in cases:
        m = Message(scam_manual_verdict=manual, scam_status=status, scam_score=score)
        assert bool(m.effective_is_scam) is expected, (manual, status, score)


@pytest.mark.asyncio
async def test_sql_expression_matches(db):
    await _seed(db, [(1, 1, "completed", 0.9, None), (2, 1, "failed", None, None), (3, 1, "completed", 0.9, "not_scam"),
                     (4, 2, None, None, "scam"), (5, 2, "completed", 0.1, None)])
    ids = (await db.execute(select(Message.seq).where(Message.effective_is_scam))).scalars().all()
    assert sorted(ids) == [1, 4]


@pytest.mark.asyncio
async def test_dismiss_flag_keeps_scores_and_manual_verdicts(api, db, monkeypatch):
    from backend import prefs
    monkeypatch.setattr(prefs, "get_pref", lambda k, d=None: True if k == "shield_sync_dismissals" else d)
    import backend.scam.verdict as verdict_mod
    monkeypatch.setattr(verdict_mod, "get_pref", lambda k, d=None: True if k == "shield_sync_dismissals" else d)

    await _seed(db, [(1, 10, "completed", 0.9, None), (2, 10, "completed", 0.8, "scam"), (3, 10, "completed", 0.1, None)])
    sender = await _sender(10)
    db.add(ScamFlag(identity_id=sender.id, source="local", flagged_at=datetime.utcnow()))
    await db.commit()

    r = await api.delete(f"/api/scam/flags/{sender.id}")
    assert r.status_code == 200, r.text
    assert r.json()["identity_id"] == sender.id
    assert r.json()["flags"] == [{"identity_id": sender.id, "person_id": sender.person_id, "status": "dismissed"}]
    m1, m2, m3 = await _get(1), await _get(2), await _get(3)
    assert m1.scam_score == 0.9 and m1.scam_manual_verdict == "not_scam"  # banner cleared, score kept
    assert m2.scam_manual_verdict == "scam"  # user's own verdict never overwritten
    assert m3.scam_manual_verdict is None  # wasn't scam, untouched
    flag = await _flag(sender.id)
    assert flag.status == "dismissed" and flag.synced_to_cloud is False
    assert m1.scam_updated_at > datetime.utcnow() - timedelta(minutes=1)  # change-tracked for cloud

    # Dismissed messages stay visible in Shield
    r = await api.get("/api/scam/scanned-messages")
    assert await _seqs(r.json()["messages"]) == [1, 2, 3]
    assert {(m["sender_identity_id"], m["sender_person_id"]) for m in r.json()["messages"]} == {
        (sender.id, sender.person_id)}
    # Dismissed flags leave the flag list; queued for cloud
    r = await api.get("/api/scam/flags")
    assert r.json()["flags"] == []
    async with database.AsyncSessionLocal() as s:
        dismissals = (await s.execute(select(ScamDismissal))).scalars().all()
    assert [d.dismissal_type for d in dismissals] == ["scammer"]
    assert json.loads(dismissals[0].payload_json)["identity_id"] == sender.id  # internal ids only


@pytest.mark.asyncio
async def test_mark_not_scam_on_only_flagged_message_equals_dismiss(api, db):
    conv = await _seed(db, [(1, 10, "completed", 0.9, None)])
    sender = await _sender(10)
    db.add(ScamFlag(identity_id=sender.id, source="local", flagged_at=datetime.utcnow()))
    await db.commit()

    r = await api.put("/api/scam/scanned-messages/verdict",
                      json={"messages": [{"conversation_id": conv.id, "message_id": await _mid(1)}],
                            "verdict": "not_scam"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["messages"][0]["effective_is_scam"] is False
    assert body["messages"][0]["message_id"] == await _mid(1)
    assert body["messages"][0]["conversation_id"] == conv.id
    assert body["messages"][0]["identity_id"] == sender.id and body["messages"][0]["person_id"] == sender.person_id
    assert body["flags"] == [{"identity_id": sender.id, "person_id": sender.person_id, "status": "dismissed"}]
    assert (await _flag(sender.id)).status == "dismissed"
    assert (await _get(1)).scam_score == 0.9


@pytest.mark.asyncio
async def test_legacy_delete_scanned_message_is_mark_not_scam(api, db):
    await _seed(db, [(1, 10, "completed", 0.9, None)])
    r = await api.delete(f"/api/scam/scanned-messages/{await _mid(1)}")
    assert r.status_code == 200
    m = await _get(1)
    assert m.scam_manual_verdict == "not_scam" and m.scam_score == 0.9


@pytest.mark.asyncio
async def test_mark_scam_flags_sender_and_clear_restores_pipeline(api, db, _quiet_ws):
    await _seed(db, [(1, 10, "completed", 0.2, None)])
    sender = await _sender(10)
    r = await api.put("/api/scam/scanned-messages/verdict",
                      json={"messages": [{"message_id": await _mid(1)}], "verdict": "scam"})
    assert r.json()["messages"][0]["effective_is_scam"] is True
    flag = await _flag(sender.id)
    assert flag.status == "active" and flag.flagged_message_id == await _mid(1)
    assert any(e["type"] == "scam_verdict_changed" for e in _quiet_ws)

    r = await api.put("/api/scam/scanned-messages/verdict",
                      json={"messages": [{"message_id": await _mid(1)}], "verdict": None})
    assert r.json()["messages"][0]["effective_is_scam"] is False


@pytest.mark.asyncio
async def test_sender_verdict_endpoint(api, db):
    await _seed(db, [(1, 10, "completed", 0.2, None)])
    sid = (await _sender(10)).id
    assert (await api.put(f"/api/scam/senders/{sid}/verdict", json={"verdict": "scam"})).status_code == 200
    assert (await _flag(sid)).status == "active"
    flags = (await api.get("/api/scam/flags")).json()
    assert flags["flagged_identity_ids"] == [sid] and flags["flagged_person_ids"] == [(await _sender(10)).person_id]
    assert flags["flags"][0]["last_conversation_title"] == "G"
    assert flags["flags"][0]["last_message_id"] == await _mid(1)
    assert (await api.put(f"/api/scam/senders/{sid}/verdict", json={"verdict": None})).status_code == 200
    assert await _flag(sid) is None
    assert (await api.put(f"/api/scam/senders/{sid}/verdict", json={"verdict": "bogus"})).status_code == 400
    assert (await api.put("/api/scam/senders/999999/verdict", json={"verdict": "scam"})).status_code == 404


@pytest.mark.asyncio
async def test_filters_use_effective_verdict_and_status(api, db):
    await _seed(db, [(1, 1, "completed", 0.9, None), (2, 1, "failed", None, None), (3, 1, "completed", 0.9, "not_scam"),
                     (4, 2, None, None, "scam"), (5, 2, "completed", 0.1, None), (6, 2, "pending", None, None)])

    async def ids(vf):
        r = await api.get("/api/scam/scanned-messages", params={"verdict_filter": vf})
        assert r.status_code == 200, r.text
        return await _seqs(r.json()["messages"])

    assert await ids("scam") == [1, 4]
    assert await ids("failed") == [2]
    assert await ids("not_scam") == [3, 5]
    assert await ids("pending") == [6]
    assert await ids("manual") == [3, 4]
    r = await api.get("/api/scam/scanned-messages", params={"verdict_filter": "nope"})
    assert r.status_code == 400

    conv_id = (await _get(1)).conversation_id
    r = await api.get(f"/api/scam/status/{conv_id}")
    assert r.json()["flagged_message_count"] == 2
    assert sorted(r.json()["flagged_message_ids"]) == sorted([await _mid(1), await _mid(4)])
    r = await api.get("/api/scam/scanned-messages", params={"sender_identity_id": (await _sender(2)).id})
    assert await _seqs(r.json()["messages"]) == [4, 5, 6]
    r = await api.get("/api/scam/scanned-messages", params={"conversation_id": conv_id, "min_members": 51})
    assert r.json()["messages"] == []


@pytest.mark.asyncio
async def test_keyset_pagination_is_stable_with_ties_and_inserts(api, db):
    conv = await _seed(db, [(i, 1, "completed", 0.1, None) for i in range(1, 8)])
    seen = []
    r = await api.get("/api/scam/scanned-messages", params={"page_size": 3})
    body = r.json()
    seen += body["messages"]
    # a new scan lands at the top between page loads
    await f.message(db, conv, 100, "new", date=datetime.utcnow(), scam_status="completed", scam_score=0.1)
    while body["next_cursor"]:
        body = (await api.get("/api/scam/scanned-messages", params={"page_size": 3, "cursor": body["next_cursor"]})).json()
        seen += body["messages"]
    assert await _seqs(seen) == list(range(1, 8)) and len(seen) == 7
    m = body["messages"][0] if body["messages"] else None
    assert m is None or (m["date"].endswith("Z") and "effective_is_scam" in m and "scam_status" in m)


@pytest.mark.asyncio
async def test_export_has_no_cap_and_cleans_old_files(api, db, tmp_path, monkeypatch):
    import os
    import time
    import backend.api.scam as scam_api
    monkeypatch.setattr(scam_api, "_EXPORTS_DIR", tmp_path / "exports")
    from backend.scam import views as scam_views
    monkeypatch.setattr(scam_views, "EXPORT_BATCH", 4)
    (tmp_path / "exports").mkdir()
    stale = tmp_path / "exports" / "old.json"
    stale.write_text("[]")
    os.utime(stale, (time.time() - 3 * 86400, time.time() - 3 * 86400))

    await _seed(db, [(i, 1, "completed", 0.9 if i % 2 else 0.1, None) for i in range(1, 11)])
    r = await api.get("/api/scam/scanned-messages/export")
    body = r.json()
    assert body["count"] == 10
    rows = json.loads(open(body["temp_path"]).read())
    assert len(rows) == 10 and len({r["message_id"] for r in rows}) == 10
    assert {r["conversation_title"] for r in rows} == {"G"}
    assert sum(r["effective_is_scam"] for r in rows) == 5
    assert not stale.exists()


@pytest.mark.asyncio
async def test_check_messages_queues_and_404s(api, db, monkeypatch):
    from backend.scam import queue as q
    from backend.scam import service as svc
    calls = []

    async def fake_enqueue(conversation_id, ids, source):
        calls.append((conversation_id, ids, source))

    monkeypatch.setattr("backend.api.scam.enqueue_targets", fake_enqueue)
    monkeypatch.setattr(svc, "_service", object())
    conv = await _seed(db, [(1, 1, None, None, None)])
    mid = await _mid(1)
    r = await api.post("/api/scam/check-messages", json={"conversation_id": conv.id, "message_ids": [mid, 777777]})
    assert r.status_code == 200, r.text
    assert calls == [(conv.id, [mid], "manual")]
    assert r.json()["missing_ids"] == [777777] and r.json()["queued_ids"] == [mid]
    r = await api.post("/api/scam/check-messages", json={"conversation_id": conv.id, "message_ids": [555555]})
    assert r.status_code == 404
    other = await f.conversation(db, "-100701", title="Other", kind="group")
    r = await api.post("/api/scam/check-messages", json={"conversation_id": other.id, "message_ids": [mid]})
    assert r.status_code == 404  # message ids are checked against the conversation
    assert q  # module import sanity


@pytest.mark.asyncio
async def test_report_and_feedback_use_internal_ids(api, db):
    conv = await _seed(db, [(1, 10, "completed", 0.9, None), (2, 20, None, None, None), (3, 10, None, None, None)])
    sender = await _sender(10)
    ctx = await api.get("/api/scam/reports/context", params={"conversation_id": conv.id,
                                                            "target_message_id": await _mid(2)})
    assert ctx.status_code == 200, ctx.text
    assert await _seqs(ctx.json()["messages"]) == [1, 2, 3]
    assert ctx.json()["messages"][0]["sender_identity_id"] == sender.id

    r = await api.post("/api/scam/reports", json={
        "reported_identity_id": sender.id, "reported_in_conversation_id": conv.id,
        "included_message_ids": [await _mid(1), await _mid(3)],
    })
    assert r.status_code == 200, r.text
    reports = (await api.get("/api/scam/reports")).json()["reports"]
    assert reports[0]["reported_identity_id"] == sender.id and reports[0]["reported_person_id"] == sender.person_id
    assert reports[0]["conversation_title"] == "G" and reports[0]["conversation_kind"] == "group"
    assert reports[0]["reported_sender_name"] == "u10"
    bad = await api.post("/api/scam/reports", json={
        "reported_identity_id": 999999, "reported_in_conversation_id": conv.id, "included_message_ids": []})
    assert bad.status_code == 404

    r = await api.post("/api/scam/feedback", json={
        "feedback_type": "false_positive", "message": "not a scam", "related_conversation_id": conv.id,
        "related_message_id": await _mid(1), "related_flag_identity_id": sender.id,
    })
    assert r.status_code == 200, r.text
    fb = (await api.get("/api/scam/feedback")).json()["feedback"][0]
    assert (fb["related_conversation_id"], fb["related_message_id"], fb["related_flag_identity_id"]) == (
        conv.id, await _mid(1), sender.id)
    r = await api.post("/api/scam/feedback", json={"feedback_type": "bug", "message": "x", "related_message_id": 999999})
    assert r.status_code == 404
