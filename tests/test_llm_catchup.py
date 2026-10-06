"""Catch-up: server-side needs-reply triage, summary generation job/upsert,
note semantics, bulk contexts and dashboard payload."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

import backend.catchup_summary_scheduler as cs
from backend import jobs
from backend.db import crud
from backend.db.models import CatchUpItem, CatchUpSummary
from backend.llm.base import LLMUnavailable
from tests.llm_fakes import FakeProvider, seed_chat, seed_msg


async def _tracked_waiting(db, n):
    for i in range(n):
        cid = -2000 - i
        await seed_chat(db, cid, f"C{i}", is_tracked=True)
        await seed_msg(db, cid, 1, f"question {i}?", minutes_ago=10 + i)


@pytest.mark.asyncio
async def test_needs_reply_filters_triage_before_limit(db):
    await _tracked_waiting(db, 12)
    now = datetime.utcnow()
    # The 10 newest are triaged away: handled (after the last message) or snoozed.
    for i in range(10):
        status = "handled" if i % 2 else "snoozed"
        db.add(CatchUpItem(
            chat_telegram_id=-2000 - i, status=status,
            handled_at=now if status == "handled" else None,
            snoozed_until=now + timedelta(hours=5) if status == "snoozed" else None,
        ))
    await db.commit()
    active, handled = await crud.get_needs_reply(db, limit=10)
    assert sorted(r[0].telegram_id for r in active) == [-2011, -2010]
    assert len(handled) == 5


@pytest.mark.asyncio
async def test_handled_chat_returns_when_a_new_message_arrives(db):
    await _tracked_waiting(db, 1)
    db.add(CatchUpItem(chat_telegram_id=-2000, status="handled",
                       handled_at=datetime.utcnow() - timedelta(hours=1)))
    await db.commit()
    active, _ = await crud.get_needs_reply(db)
    assert [r[0].telegram_id for r in active] == [-2000]
    assert active[0][4].status == "handled"  # triage metadata travels with the row


@pytest.mark.asyncio
async def test_expired_snooze_is_active(db):
    await _tracked_waiting(db, 1)
    db.add(CatchUpItem(chat_telegram_id=-2000, status="snoozed",
                       snoozed_until=datetime.utcnow() - timedelta(minutes=1)))
    await db.commit()
    active, _ = await crud.get_needs_reply(db)
    assert len(active) == 1


@pytest.mark.asyncio
async def test_generate_all_uses_needs_reply_and_upserts(db):
    await _tracked_waiting(db, 2)
    provider = FakeProvider(summary="They asked a question.")
    with patch.object(cs, "get_provider", return_value=provider):
        assert await cs.generate_all_summaries() == 2
        # unchanged chats are skipped, forced ones are rewritten without UNIQUE errors
        assert await cs.generate_all_summaries() == 0
        assert await cs.generate_all_summaries([-2000], force=True) == 1
    rows = (await db.execute(select(CatchUpSummary))).scalars().all()
    assert len(rows) == 2 and all(r.summary == "They asked a question." for r in rows)


@pytest.mark.asyncio
async def test_llm_down_falls_back_once_to_rule_based(db):
    await _tracked_waiting(db, 3)
    provider = FakeProvider(fail_with=LLMUnavailable("down"))
    progress = {}
    with patch.object(cs, "get_provider", return_value=provider):
        assert await cs.generate_all_summaries(progress=progress) == 3
    assert len(provider.complete_calls) == 1  # stopped calling the dead model
    assert progress["llm_error"]
    rows = (await db.execute(select(CatchUpSummary))).scalars().all()
    assert all("since your last activity" in r.summary for r in rows)


@pytest.mark.asyncio
async def test_post_anchor_fetch_is_capped(db):
    await seed_chat(db, -3000, "Busy", is_tracked=True)
    await seed_msg(db, -3000, 1, "my last message", minutes_ago=500, is_outgoing=True)
    for i in range(2, 200):
        await seed_msg(db, -3000, i, f"update number {i}", minutes_ago=400 - i)
    provider = FakeProvider(summary="Lots happened.")
    with patch.object(cs, "get_provider", return_value=provider):
        await cs.generate_all_summaries([-3000], force=True)
    prompt = provider.complete_calls[0]["messages"][-1].content
    assert "update number 199" in prompt and "update number 2\n" not in prompt
    assert prompt.count("update number") <= cs.POST_ANCHOR_LIMIT


@pytest.mark.asyncio
async def test_generate_endpoint_returns_job_and_is_single_flight(api, db):
    await _tracked_waiting(db, 1)
    gate = asyncio.Event()

    async def slow(chat_ids=None, *, force=False, progress=None):
        await gate.wait()
        return 1

    with patch.object(cs, "generate_all_summaries", side_effect=slow):
        r1 = await api.post("/api/catchup/generate-summaries", json={})
        r2 = await api.post("/api/catchup/generate-summaries", json={"chat_ids": [-2000]})
        assert r1.status_code == 202 and r1.json()["started"] is True
        assert r2.json()["started"] is False and r2.json()["job_id"] == r1.json()["job_id"]
        status = (await api.get("/api/catchup/generate-summaries/status")).json()
        assert status["status"] == "running"
        gate.set()
        await jobs.get_running(cs.JOB_KEY)
    status = (await api.get("/api/catchup/generate-summaries/status")).json()
    assert status["status"] == "done" and status["summaries_generated"] == 1


@pytest.mark.asyncio
async def test_note_blank_does_not_erase_and_clear_is_explicit(api):
    r = await api.post("/api/catchup/-5/note", json={"note": "call back Tuesday"})
    assert r.json()["note"] == "call back Tuesday"
    r = await api.post("/api/catchup/-5/note", json={"note": "  "})
    assert r.json()["note"] == "call back Tuesday"
    r = await api.post("/api/catchup/-5/note", json={"clear": True})
    assert r.json()["note"] is None
    await api.post("/api/catchup/-5/note", json={"note": "x"})
    r = await api.post("/api/catchup/-5/note", json={"note": None})
    assert r.json()["note"] is None


@pytest.mark.asyncio
async def test_dashboard_payload_has_triage_context_and_utc_dates(api, db):
    await _tracked_waiting(db, 2)
    await api.post("/api/catchup/-2001/handled")
    provider = FakeProvider(summary="Summary A")
    with patch.object(cs, "get_provider", return_value=provider):
        await cs.generate_all_summaries([-2000], force=True)
    data = (await api.get("/api/dashboard")).json()
    [item] = data["needs_reply"]
    assert item["chat"]["telegram_id"] == -2000
    assert item["last_message"]["date"].endswith("Z")
    assert item["context"]["summary"] == "Summary A"
    assert item["triage"] is None
    assert "is_pinned" in item["chat"]  # canonical chat serializer
    [handled] = data["recently_handled"]
    assert handled["triage"]["status"] == "handled"

    ctx = (await api.get("/api/catchup/contexts", params={"chat_ids": "-2000,-2001"})).json()
    assert ctx["-2000"]["summary"] == "Summary A" and ctx["-2001"]["summary"] is None


@pytest.mark.asyncio
async def test_org_directory_matches_attached_positions_by_org_id(db):
    from backend.db.models import Contact, ContactPosition, Organization
    from backend.org_context import OrgDirectory
    zircuit, other = Organization(name="Zircuit"), Organization(name="OtherCo")
    db.add_all([zircuit, other])
    await db.commit()
    db.add_all([
        Contact(telegram_id=1, first_name="Alice", last_name="Lee"),
        Contact(telegram_id=2, first_name="Bob"),
        Contact(telegram_id=3, first_name="Carol"),
        ContactPosition(telegram_id=1, organization_id=zircuit.id, organization="Zircuit", title="Engineer"),
        ContactPosition(telegram_id=2, organization_id=other.id, organization="OtherCo", title="PM"),
        # detached: org deleted with mode=detach, name frozen — must not count
        ContactPosition(telegram_id=3, organization_id=None, organization="Zircuit", title="Ex"),
    ])
    await db.commit()

    by_name = await OrgDirectory.load(db, ["  zircuit "])
    assert by_name.for_senders(["Alice Lee", "Bob", "Carol"]) == \
        "Your colleagues in this chat: Alice Lee (Engineer at Zircuit)."
    assert by_name.for_contact("alice") == "This person is your colleague: alice (Engineer at Zircuit)."
    assert by_name.for_contact("Carol") is None

    # Ids win over (stale) names, so a rename can't break the match.
    zircuit.name = "Zircuit Labs"
    await db.commit()
    by_id = await OrgDirectory.load(db, ["Zircuit"], [zircuit.id, 999])
    assert by_id.for_contact("Alice") == "This person is your colleague: Alice (Engineer at Zircuit Labs)."
    assert (await OrgDirectory.load(db, ["Zircuit"])).for_contact("Alice") is None  # stale name only
    assert (await OrgDirectory.load(db, [], [])).for_senders(["Alice"]) is None

    # Prefs path goes through backend.user_orgs (ids are the source of truth).
    prefs = {"user_organization_ids": [zircuit.id], "user_organizations": ["Zircuit Labs"]}
    with patch("backend.user_orgs.load_prefs", side_effect=lambda: dict(prefs)), \
         patch("backend.user_orgs.update_prefs", side_effect=lambda fn: fn(prefs)):
        from_prefs = await OrgDirectory.load(db)
    assert from_prefs.for_contact("Alice") == "This person is your colleague: Alice (Engineer at Zircuit Labs)."
    assert from_prefs.for_contact("Carol") is None


@pytest.mark.asyncio
async def test_degraded_summaries_recover_when_llm_returns(api, db):
    await _tracked_waiting(db, 3)
    down = FakeProvider(fail_with=LLMUnavailable("down"))
    with patch.object(cs, "get_provider", return_value=down):
        assert await cs.generate_all_summaries() == 3
        # Still down: one probe per pass, degraded summaries are not rewritten in bulk.
        down.complete_calls.clear()
        await cs.generate_all_summaries()
        assert len(down.complete_calls) == 1
    rows = (await db.execute(select(CatchUpSummary))).scalars().all()
    assert all(r.is_degraded for r in rows)
    ctx = (await api.get("/api/catchup/-2000/context")).json()
    assert ctx["degraded"] is True

    up = FakeProvider(summary="They asked you something.")
    with patch.object(cs, "get_provider", return_value=up):
        # no new messages, yet degraded summaries are regenerated
        assert await cs.generate_all_summaries() == 3
        assert await cs.generate_all_summaries() == 0  # now fresh
    for r in (await db.execute(select(CatchUpSummary))).scalars().all():
        await db.refresh(r)
        assert not r.is_degraded and r.summary == "They asked you something."
    assert (await api.get("/api/catchup/-2000/context")).json()["degraded"] is False
