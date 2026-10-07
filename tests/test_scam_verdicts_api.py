"""Verdict model through the API: one set_verdict for every entry point,
effective_is_scam everywhere, keyset pagination, status and export."""

import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.db import database
from backend.db.models import Chat, Message, ScamDismissal, ScamFlag
from backend.ws.manager import ws_manager

CHAT = -100700


@pytest.fixture(autouse=True)
def _quiet_ws(monkeypatch):
    sent = []

    async def _broadcast(event):
        sent.append(event)

    monkeypatch.setattr(ws_manager, "broadcast", _broadcast)
    return sent


async def _seed(db, rows):
    """rows: (tg_id, sender, status, score, manual)"""
    db.add(Chat(telegram_id=CHAT, name="G", type="group", members_count=50))
    base = datetime.utcnow() - timedelta(hours=1)
    for i, (tg, sender, status, score, manual) in enumerate(rows):
        db.add(Message(tg_message_id=tg, chat_id=CHAT, sender_id=sender, sender_name=f"u{sender}",
                       text=f"m{tg}", date=base + timedelta(seconds=i // 2),  # duplicate dates on purpose
                       scam_status=status, scam_score=score, scam_manual_verdict=manual,
                       scam_updated_at=base))
    await db.commit()


async def _get(tg):
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(select(Message).where(Message.chat_id == CHAT, Message.tg_message_id == tg))).scalars().one()


async def _flag(tid):
    async with database.AsyncSessionLocal() as s:
        return (await s.execute(select(ScamFlag).where(ScamFlag.telegram_id == tid))).scalars().first()


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
    ids = (await db.execute(select(Message.tg_message_id).where(Message.effective_is_scam))).scalars().all()
    assert sorted(ids) == [1, 4]


@pytest.mark.asyncio
async def test_dismiss_flag_keeps_scores_and_manual_verdicts(api, db, monkeypatch):
    from backend import prefs
    monkeypatch.setattr(prefs, "get_pref", lambda k, d=None: True if k == "shield_sync_dismissals" else d)
    import backend.scam.verdict as verdict_mod
    monkeypatch.setattr(verdict_mod, "get_pref", lambda k, d=None: True if k == "shield_sync_dismissals" else d)

    await _seed(db, [(1, 10, "completed", 0.9, None), (2, 10, "completed", 0.8, "scam"), (3, 10, "completed", 0.1, None)])
    db.add(ScamFlag(telegram_id=10, source="local", flagged_at=datetime.utcnow()))
    await db.commit()

    r = await api.delete("/api/scam/flags/10")
    assert r.status_code == 200, r.text
    m1, m2, m3 = await _get(1), await _get(2), await _get(3)
    assert m1.scam_score == 0.9 and m1.scam_manual_verdict == "not_scam"  # banner cleared, score kept
    assert m2.scam_manual_verdict == "scam"  # user's own verdict never overwritten
    assert m3.scam_manual_verdict is None  # wasn't scam, untouched
    flag = await _flag(10)
    assert flag.status == "dismissed" and flag.synced_to_cloud is False
    assert m1.scam_updated_at > datetime.utcnow() - timedelta(minutes=1)  # change-tracked for cloud

    # Dismissed messages stay visible in Shield
    r = await api.get("/api/scam/scanned-messages")
    assert {m["tg_message_id"] for m in r.json()["messages"]} == {1, 2, 3}
    # Dismissed flags leave the flag list; queued for cloud
    r = await api.get("/api/scam/flags")
    assert r.json()["flags"] == []
    async with database.AsyncSessionLocal() as s:
        assert [d.dismissal_type for d in (await s.execute(select(ScamDismissal))).scalars()] == ["scammer"]


@pytest.mark.asyncio
async def test_mark_not_scam_on_only_flagged_message_equals_dismiss(api, db):
    await _seed(db, [(1, 10, "completed", 0.9, None)])
    db.add(ScamFlag(telegram_id=10, source="local", flagged_at=datetime.utcnow()))
    await db.commit()

    r = await api.put("/api/scam/scanned-messages/verdict",
                      json={"messages": [{"chat_id": CHAT, "tg_message_id": 1}], "verdict": "not_scam"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["messages"][0]["effective_is_scam"] is False
    assert body["flags"] == [{"telegram_id": 10, "status": "dismissed"}]
    assert (await _flag(10)).status == "dismissed"
    assert (await _get(1)).scam_score == 0.9


@pytest.mark.asyncio
async def test_legacy_delete_scanned_message_is_mark_not_scam(api, db):
    await _seed(db, [(1, 10, "completed", 0.9, None)])
    r = await api.delete(f"/api/scam/scanned-messages/{CHAT}/1")
    assert r.status_code == 200
    m = await _get(1)
    assert m.scam_manual_verdict == "not_scam" and m.scam_score == 0.9


@pytest.mark.asyncio
async def test_mark_scam_flags_sender_and_clear_restores_pipeline(api, db, _quiet_ws):
    await _seed(db, [(1, 10, "completed", 0.2, None)])
    r = await api.put("/api/scam/scanned-messages/verdict",
                      json={"messages": [{"chat_id": CHAT, "tg_message_id": 1}], "verdict": "scam"})
    assert r.json()["messages"][0]["effective_is_scam"] is True
    assert (await _flag(10)).status == "active"
    assert any(e["type"] == "scam_verdict_changed" for e in _quiet_ws)

    r = await api.put("/api/scam/scanned-messages/verdict",
                      json={"messages": [{"chat_id": CHAT, "tg_message_id": 1}], "verdict": None})
    assert r.json()["messages"][0]["effective_is_scam"] is False


@pytest.mark.asyncio
async def test_sender_verdict_endpoint(api, db):
    await _seed(db, [(1, 10, "completed", 0.2, None)])
    assert (await api.put("/api/scam/senders/10/verdict", json={"verdict": "scam"})).status_code == 200
    assert (await _flag(10)).status == "active"
    assert (await api.put("/api/scam/senders/10/verdict", json={"verdict": None})).status_code == 200
    assert await _flag(10) is None
    assert (await api.put("/api/scam/senders/10/verdict", json={"verdict": "bogus"})).status_code == 400


@pytest.mark.asyncio
async def test_filters_use_effective_verdict_and_status(api, db):
    await _seed(db, [(1, 1, "completed", 0.9, None), (2, 1, "failed", None, None), (3, 1, "completed", 0.9, "not_scam"),
                     (4, 2, None, None, "scam"), (5, 2, "completed", 0.1, None), (6, 2, "pending", None, None)])

    async def ids(f):
        r = await api.get("/api/scam/scanned-messages", params={"verdict_filter": f})
        assert r.status_code == 200, r.text
        return sorted(m["tg_message_id"] for m in r.json()["messages"])

    assert await ids("scam") == [1, 4]
    assert await ids("failed") == [2]
    assert await ids("not_scam") == [3, 5]
    assert await ids("pending") == [6]
    assert await ids("manual") == [3, 4]
    r = await api.get("/api/scam/scanned-messages", params={"verdict_filter": "nope"})
    assert r.status_code == 400

    r = await api.get(f"/api/scam/status/{CHAT}")
    assert r.json()["flagged_message_count"] == 2 and sorted(r.json()["flagged_message_ids"]) == [1, 4]


@pytest.mark.asyncio
async def test_keyset_pagination_is_stable_with_ties_and_inserts(api, db):
    await _seed(db, [(i, 1, "completed", 0.1, None) for i in range(1, 8)])
    seen = []
    r = await api.get("/api/scam/scanned-messages", params={"page_size": 3})
    body = r.json()
    seen += [m["tg_message_id"] for m in body["messages"]]
    # a new scan lands at the top between page loads
    db.add(Message(tg_message_id=100, chat_id=CHAT, text="new", date=datetime.utcnow(), scam_status="completed", scam_score=0.1))
    await db.commit()
    while body["next_cursor"]:
        body = (await api.get("/api/scam/scanned-messages", params={"page_size": 3, "cursor": body["next_cursor"]})).json()
        seen += [m["tg_message_id"] for m in body["messages"]]
    assert sorted(seen) == list(range(1, 8)) and len(seen) == 7
    m = body["messages"][0] if body["messages"] else None
    assert m is None or (m["date"].endswith("Z") and "effective_is_scam" in m and "scam_status" in m)


@pytest.mark.asyncio
async def test_export_has_no_cap_and_cleans_old_files(api, db, tmp_path, monkeypatch):
    import os
    import time
    import backend.api.scam as scam_api
    monkeypatch.setattr(scam_api, "_EXPORTS_DIR", tmp_path / "exports")
    monkeypatch.setattr(scam_api, "_EXPORT_BATCH", 4)
    (tmp_path / "exports").mkdir()
    stale = tmp_path / "exports" / "old.json"
    stale.write_text("[]")
    os.utime(stale, (time.time() - 3 * 86400, time.time() - 3 * 86400))

    await _seed(db, [(i, 1, "completed", 0.9 if i % 2 else 0.1, None) for i in range(1, 11)])
    r = await api.get("/api/scam/scanned-messages/export")
    body = r.json()
    assert body["count"] == 10
    rows = json.loads(open(body["temp_path"]).read())
    assert len(rows) == 10 and len({r["tg_message_id"] for r in rows}) == 10
    assert sum(r["effective_is_scam"] for r in rows) == 5
    assert not stale.exists()


@pytest.mark.asyncio
async def test_check_messages_queues_and_404s(api, db, monkeypatch):
    from backend.scam import queue as q
    from backend.scam import service as svc
    calls = []

    async def fake_enqueue(chat_id, ids, source):
        calls.append((chat_id, ids, source))

    monkeypatch.setattr("backend.api.scam.enqueue_targets", fake_enqueue)
    monkeypatch.setattr(svc, "_service", object())
    await _seed(db, [(1, 1, None, None, None)])
    r = await api.post("/api/scam/check-messages", json={"chat_id": CHAT, "message_telegram_ids": [1, 77]})
    assert r.status_code == 200, r.text
    assert calls == [(CHAT, [1], "manual")]
    assert r.json()["missing_ids"] == [77]
    r = await api.post("/api/scam/check-messages", json={"chat_id": CHAT, "message_telegram_ids": [55]})
    assert r.status_code == 404
    assert q  # module import sanity
