"""Catch-up triage routes: item lifecycle, the active / all lists, notes,
contexts, and an incoming message reopening a handled item."""

from datetime import datetime

import pytest

from backend.db.models import CatchUpSummary
from tests import factories as f


@pytest.mark.asyncio
async def test_triage_lifecycle_and_lists(api, db):
    alice = await f.conversation(db, "801", title="Alice")
    bob = await f.conversation(db, "802", title="Bob")

    snoozed = (await api.post(f"/api/catchup/{alice.id}/snooze", json={"hours": 2})).json()
    assert snoozed["status"] == "snoozed" and snoozed["snoozed_until"] and snoozed["person_id"] == alice.peer_identity.person_id
    handled = (await api.post(f"/api/catchup/{bob.id}/handled")).json()
    assert handled["status"] == "handled" and handled["handled_at"]

    assert (await api.get("/api/catchup/items")).json() == []  # snoozed in the future, handled
    assert sorted(i["conversation_id"] for i in (await api.get("/api/catchup/all")).json()) == [alice.id, bob.id]

    reopened = (await api.post(f"/api/catchup/{bob.id}/reopen")).json()
    assert reopened["status"] == "pending" and reopened["handled_at"] is None and reopened["id"] == handled["id"]
    assert [i["conversation_id"] for i in (await api.get("/api/catchup/items")).json()] == [bob.id]

    noted = (await api.post(f"/api/catchup/{bob.id}/note", json={"note": "  call back  "})).json()
    assert noted["note"] == "call back"
    assert (await api.post(f"/api/catchup/{bob.id}/note", json={"note": "   "})).json()["note"] == "call back"
    assert (await api.post(f"/api/catchup/{bob.id}/note", json={"clear": True})).json()["note"] is None
    assert (await api.post("/api/catchup/99999/handled")).status_code == 404


@pytest.mark.asyncio
async def test_contexts_count_new_messages_since_the_summary(api, db):
    conv = await f.conversation(db, "803", title="Cara")
    other = await f.conversation(db, "804", title="Dan")
    for i in range(1, 6):
        await f.message(db, conv, i, f"m{i}")
    db.add(CatchUpSummary(conversation_id=conv.id, summary="All good", total_messages_at_generation=3,
                          summary_messages_json='["m1"]', generated_at=datetime(2026, 1, 1),
                          last_activity_at=datetime(2026, 1, 1)))
    await db.commit()
    body = (await api.get("/api/catchup/contexts", params={"conversation_ids": f"{conv.id},{other.id}"})).json()
    assert body[str(conv.id)]["summary"] == "All good"
    assert body[str(conv.id)]["new_messages_since_summary"] == 2
    assert body[str(conv.id)]["summary_messages"] == ["m1"]
    assert body[str(other.id)]["summary"] is None
    single = (await api.get(f"/api/catchup/{other.id}/context")).json()
    assert single["summary"].startswith("Summary not yet generated")
    assert (await api.get("/api/catchup/contexts", params={"conversation_ids": "1,x"})).status_code == 400


@pytest.mark.asyncio
async def test_incoming_message_reopens_a_handled_item(api, db):
    from backend.ingest import fanout

    conv = await f.conversation(db, "805", title="Eve")
    await api.post(f"/api/catchup/{conv.id}/handled")
    await fanout._reopen_catchup(conv.id)
    [item] = (await api.get("/api/catchup/items")).json()
    assert item["conversation_id"] == conv.id and item["status"] == "pending"
