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
from tests import factories
from tests.llm_fakes import FakeProvider, seed_chat, seed_msg


async def _tracked_waiting(db, n):
    """n tracked conversations whose last message is an unanswered question;
    convs[0] is the newest."""
    convs = []
    for i in range(n):
        conv = await seed_chat(db, -2000 - i, f"C{i}", is_tracked=True)
        await seed_msg(db, conv, 1, f"question {i}?", minutes_ago=10 + i)
        convs.append(conv)
    return convs


@pytest.mark.asyncio
async def test_needs_reply_filters_triage_before_limit(db):
    convs = await _tracked_waiting(db, 12)
    now = datetime.utcnow()
    # The 10 newest are triaged away: handled (after the last message) or snoozed.
    for i in range(10):
        status = "handled" if i % 2 else "snoozed"
        db.add(CatchUpItem(
            conversation_id=convs[i].id, status=status,
            handled_at=now if status == "handled" else None,
            snoozed_until=now + timedelta(hours=5) if status == "snoozed" else None,
        ))
    await db.commit()
    active, handled = await crud.get_needs_reply(db, limit=10)
    assert sorted(r[0].id for r in active) == sorted([convs[10].id, convs[11].id])
    assert len(handled) == 5


@pytest.mark.asyncio
async def test_handled_chat_returns_when_a_new_message_arrives(db):
    [conv] = await _tracked_waiting(db, 1)
    db.add(CatchUpItem(conversation_id=conv.id, status="handled",
                       handled_at=datetime.utcnow() - timedelta(hours=1)))
    await db.commit()
    active, _ = await crud.get_needs_reply(db)
    assert [r[0].id for r in active] == [conv.id]
    assert active[0][4].status == "handled"  # triage metadata travels with the row


@pytest.mark.asyncio
async def test_expired_snooze_is_active(db):
    [conv] = await _tracked_waiting(db, 1)
    db.add(CatchUpItem(conversation_id=conv.id, status="snoozed",
                       snoozed_until=datetime.utcnow() - timedelta(minutes=1)))
    await db.commit()
    active, _ = await crud.get_needs_reply(db)
    assert len(active) == 1


@pytest.mark.asyncio
async def test_generate_all_uses_needs_reply_and_upserts(db):
    convs = await _tracked_waiting(db, 2)
    provider = FakeProvider(summary="They asked a question.")
    with patch.object(cs, "get_provider", return_value=provider):
        assert await cs.generate_all_summaries() == 2
        # unchanged conversations are skipped, forced ones are rewritten without UNIQUE errors
        assert await cs.generate_all_summaries() == 0
        assert await cs.generate_all_summaries([convs[0].id], force=True) == 1
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
    conv = await seed_chat(db, -3000, "Busy", is_tracked=True)
    await seed_msg(db, conv, 1, "my last message", minutes_ago=500, is_outgoing=True)
    for i in range(2, 200):
        await seed_msg(db, conv, i, f"update number {i}", minutes_ago=400 - i)
    provider = FakeProvider(summary="Lots happened.")
    with patch.object(cs, "get_provider", return_value=provider):
        await cs.generate_all_summaries([conv.id], force=True)
    prompt = provider.complete_calls[0]["messages"][-1].content
    assert "update number 199" in prompt and "update number 2\n" not in prompt
    assert prompt.count("update number") <= cs.POST_ANCHOR_LIMIT


@pytest.mark.asyncio
async def test_generate_endpoint_returns_job_and_is_single_flight(api, db):
    [conv] = await _tracked_waiting(db, 1)
    gate = asyncio.Event()

    async def slow(conversation_ids=None, *, force=False, progress=None):
        await gate.wait()
        return 1

    with patch.object(cs, "generate_all_summaries", side_effect=slow):
        r1 = await api.post("/api/catchup/generate-summaries", json={})
        r2 = await api.post("/api/catchup/generate-summaries", json={"conversation_ids": [conv.id]})
        assert r1.status_code == 202 and r1.json()["started"] is True
        assert r2.json()["started"] is False and r2.json()["job_id"] == r1.json()["job_id"]
        status = (await api.get("/api/catchup/generate-summaries/status")).json()
        assert status["status"] == "running"
        gate.set()
        await jobs.get_running(cs.JOB_KEY)
    status = (await api.get("/api/catchup/generate-summaries/status")).json()
    assert status["status"] == "done" and status["summaries_generated"] == 1


@pytest.mark.asyncio
async def test_note_blank_does_not_erase_and_clear_is_explicit(api, db):
    conv = await seed_chat(db, -5, "G")
    url = f"/api/catchup/{conv.id}/note"
    r = await api.post(url, json={"note": "call back Tuesday"})
    assert r.json()["note"] == "call back Tuesday"
    assert r.json()["conversation_id"] == conv.id and r.json()["person_id"] is None
    r = await api.post(url, json={"note": "  "})
    assert r.json()["note"] == "call back Tuesday"
    r = await api.post(url, json={"clear": True})
    assert r.json()["note"] is None
    await api.post(url, json={"note": "x"})
    r = await api.post(url, json={"note": None})
    assert r.json()["note"] is None
    assert (await api.post("/api/catchup/999999/note", json={"note": "x"})).status_code == 404


@pytest.mark.asyncio
async def test_catchup_item_and_status_belong_to_the_peers_person(api, db):
    conv = await factories.conversation(db, 77, title="Alice")
    r = (await api.post(f"/api/catchup/{conv.id}/snooze", json={"hours": 2})).json()
    assert r["person_id"] == conv.peer_identity.person_id and r["status"] == "snoozed"
    r = await api.post(f"/api/catchup/{conv.id}/relationship-status", json={"status": "nurturing"})
    assert r.json() == {"person_id": conv.peer_identity.person_id, "relationship_status": "nurturing"}
    person = await crud.get_person(db, conv.peer_identity.person_id)
    await db.refresh(person)
    assert person.relationship_status_manual is True
    group = await seed_chat(db, -6, "Group")
    r = await api.post(f"/api/catchup/{group.id}/relationship-status", json={"status": "nurturing"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dashboard_payload_has_triage_context_and_utc_dates(api, db):
    a, b = await _tracked_waiting(db, 2)
    await api.post(f"/api/catchup/{b.id}/handled")
    provider = FakeProvider(summary="Summary A")
    with patch.object(cs, "get_provider", return_value=provider):
        await cs.generate_all_summaries([a.id], force=True)
    data = (await api.get("/api/dashboard")).json()
    [item] = data["needs_reply"]
    assert item["conversation"]["id"] == a.id
    assert item["last_message"]["date"].endswith("Z")
    assert item["context"]["summary"] == "Summary A"
    assert item["context"]["summary_messages"][0]["message_id"] is not None
    assert item["triage"] is None
    assert "is_pinned" in item["conversation"]  # canonical conversation serializer
    [handled] = data["recently_handled"]
    assert handled["triage"]["status"] == "handled"
    assert handled["triage"]["conversation_id"] == b.id

    ctx = (await api.get("/api/catchup/contexts", params={"conversation_ids": f"{a.id},{b.id}"})).json()
    assert ctx[str(a.id)]["summary"] == "Summary A" and ctx[str(b.id)]["summary"] is None


@pytest.mark.asyncio
async def test_org_directory_matches_attached_positions_by_org_id(db):
    from backend.db.models import Organization, PersonPosition
    from backend.org_context import OrgDirectory
    zircuit, other = Organization(name="Zircuit"), Organization(name="OtherCo")
    db.add_all([zircuit, other])
    await db.commit()
    alice = await factories.identity(db, 1, display_name="Alice Lee", first_name="Alice", last_name="Lee")
    bob = await factories.identity(db, 2, display_name="Bob", first_name="Bob")
    carol = await factories.identity(db, 3, display_name="Carol", first_name="Carol")
    db.add_all([
        PersonPosition(person_id=alice.person_id, organization_id=zircuit.id, organization="Zircuit", title="Engineer"),
        PersonPosition(person_id=bob.person_id, organization_id=other.id, organization="OtherCo", title="PM"),
        # detached: org deleted with mode=detach, name frozen — must not count
        PersonPosition(person_id=carol.person_id, organization_id=None, organization="Zircuit", title="Ex"),
    ])
    await db.commit()

    by_name = await OrgDirectory.load(db, ["  zircuit "])
    assert by_name.for_senders(["Alice Lee", "Bob", "Carol"]) == \
        "Your colleagues in this conversation: Alice Lee (Engineer at Zircuit)."
    assert by_name.for_person("alice") == "This person is your colleague: alice (Engineer at Zircuit)."
    assert by_name.for_person("Carol") is None

    # Ids win over (stale) names, so a rename can't break the match.
    zircuit.name = "Zircuit Labs"
    await db.commit()
    by_id = await OrgDirectory.load(db, ["Zircuit"], [zircuit.id, 999])
    assert by_id.for_person("Alice") == "This person is your colleague: Alice (Engineer at Zircuit Labs)."
    assert (await OrgDirectory.load(db, ["Zircuit"])).for_person("Alice") is None  # stale name only
    assert (await OrgDirectory.load(db, [], [])).for_senders(["Alice"]) is None

    # Prefs path goes through backend.user_orgs (ids are the source of truth).
    prefs = {"user_organization_ids": [zircuit.id], "user_organizations": ["Zircuit Labs"]}
    with patch("backend.user_orgs.load_prefs", side_effect=lambda: dict(prefs)), \
         patch("backend.user_orgs.update_prefs", side_effect=lambda fn: fn(prefs)):
        from_prefs = await OrgDirectory.load(db)
    assert from_prefs.for_person("Alice") == "This person is your colleague: Alice (Engineer at Zircuit Labs)."
    assert from_prefs.for_person("Carol") is None


@pytest.mark.asyncio
async def test_degraded_summaries_recover_when_llm_returns(api, db):
    convs = await _tracked_waiting(db, 3)
    down = FakeProvider(fail_with=LLMUnavailable("down"))
    with patch.object(cs, "get_provider", return_value=down):
        assert await cs.generate_all_summaries() == 3
        # Still down: one probe per pass, degraded summaries are not rewritten in bulk.
        down.complete_calls.clear()
        await cs.generate_all_summaries()
        assert len(down.complete_calls) == 1
    rows = (await db.execute(select(CatchUpSummary))).scalars().all()
    assert all(r.is_degraded for r in rows)
    ctx = (await api.get(f"/api/catchup/{convs[0].id}/context")).json()
    assert ctx["degraded"] is True

    up = FakeProvider(summary="They asked you something.")
    with patch.object(cs, "get_provider", return_value=up):
        # no new messages, yet degraded summaries are regenerated
        assert await cs.generate_all_summaries() == 3
        assert await cs.generate_all_summaries() == 0  # now fresh
    for r in (await db.execute(select(CatchUpSummary))).scalars().all():
        await db.refresh(r)
        assert not r.is_degraded and r.summary == "They asked you something."
    assert (await api.get(f"/api/catchup/{convs[0].id}/context")).json()["degraded"] is False


@pytest.mark.asyncio
async def test_service_rows_do_not_count_as_needing_reply(db):
    from sqlalchemy import update
    from backend.db.models import Person

    conv = await factories.conversation(db, 1201, title="Nia")
    nia = conv.peer_identity
    await db.execute(update(Person).where(Person.id == nia.person_id).values(is_important=True))
    await db.commit()
    await factories.message(db, conv, 1, "hi", out=True)
    await factories.message(db, conv, 2, "Nia joined Telegram", sender=nia, kind="service")
    active, _ = await crud.get_needs_reply(db)
    assert active == []
    # A missed call is a service line too: still no reply owed
    await factories.message(db, conv, 3, "Missed call", sender=nia, kind="service")
    active, _ = await crud.get_needs_reply(db)
    assert active == []
    # …while a real incoming message after them does need a reply
    await factories.message(db, conv, 4, "are you there?", sender=nia)
    active, _ = await crud.get_needs_reply(db)
    assert [row[0].id for row in active] == [conv.id]
