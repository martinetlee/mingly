"""People linking (backend/people/linking.py): automatic merges on a shared
phone / email, merge suggestions, merge / undo / detach and their API."""

import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select

from backend.db import crud
from backend.db.models import (
    BriefingEntry, CatchUpItem, Person, PersonMerge, PersonMergeSuggestion, PersonNote, PersonPosition,
)
from backend.people import linking
from tests import factories as f


@pytest.fixture
def sent(monkeypatch):
    events = []

    async def _capture(data):
        events.append(data)

    from backend.ws.manager import ws_manager
    monkeypatch.setattr(ws_manager, "broadcast", _capture)
    return events


async def _people(db) -> list[int]:
    return list((await db.execute(select(Person.id).order_by(Person.id))).scalars().all())


async def _suggestions(db) -> list[tuple]:
    return [
        (s.person_a_id, s.person_b_id, s.reason, s.status)
        for s in (await db.execute(select(PersonMergeSuggestion).order_by(PersonMergeSuggestion.id))).scalars()
    ]


def test_normalize_phone_e164():
    n = crud.normalize_phone
    assert n("15551234567") == "+15551234567"  # Telegram: international without '+'
    assert n("+1 (555) 123-4567") == "+15551234567"
    assert n("0044 20 7946 0958") == "+442079460958"
    assert n("0912 345 678", "TW") == "+886912345678"
    for bad in (None, "", "abc", "12345"):
        assert n(bad) is None


@pytest.mark.asyncio
async def test_shared_phone_merges_automatically_and_broadcasts_after_commit(db, sent):
    tg = await f.identity(db, "5", display_name="Ann", phone="15551234567")
    sig = await crud.upsert_identity(
        db, channel="signal", external_id="aci-1", display_name="Ann S", phone="+1 555 123 4567"
    )
    await asyncio.sleep(0)
    assert sig.person_id == tg.person_id  # the older (Telegram) person survives
    assert await _people(db) == [tg.person_id]
    merge = (await db.execute(select(PersonMerge))).scalar_one()
    assert merge.automatic and merge.into_person_id == tg.person_id
    assert {e["person_id"] for e in sent if e["type"] == "person_changed"} == {tg.person_id, merge.from_person_id}


@pytest.mark.asyncio
async def test_two_matching_people_become_suggestions(db):
    # Two people already share a phone because one of them was detached
    a = await f.identity(db, "1", display_name="A", phone="+15550000001")
    b = await crud.upsert_identity(db, channel="signal", external_id="b", display_name="B", phone="+15550000001")
    assert a.person_id == b.person_id
    new_person = await linking.detach(db, b.id)
    await db.commit()
    c = await crud.upsert_identity(db, channel="sms", external_id="c", display_name="C", phone="15550000001")
    assert c.person_id not in (a.person_id, new_person.id)
    pairs = {(x, y, r) for x, y, r, st in await _suggestions(db) if st == "pending"}
    assert pairs == {
        tuple(sorted((a.person_id, c.person_id))) + ("same_phone",),
        tuple(sorted((new_person.id, c.person_id))) + ("same_phone",),
    }


@pytest.mark.asyncio
async def test_self_is_never_merged_with_others(db):
    me = await f.identity(db, "1", display_name="Me", phone="+15550000009", is_self=True)
    other = await crud.upsert_identity(db, channel="signal", external_id="s", display_name="X", phone="+15550000009")
    assert other.person_id != me.person_id
    assert await _suggestions(db) == []
    with pytest.raises(linking.LinkingError):
        await linking.merge(db, me.person_id, other.person_id)
    # Two self identities do link
    me2 = await crud.upsert_identity(
        db, channel="signal", external_id="me", display_name="Me", phone="+15550000009", is_self=True
    )
    assert me2.person_id == me.person_id


@pytest.mark.asyncio
async def test_dismissed_pair_is_not_merged_automatically(db):
    a = await f.identity(db, "1", display_name="A")
    b = await crud.upsert_identity(db, channel="signal", external_id="b", display_name="B")
    await crud.add_merge_suggestion(db, a.person_id, b.person_id, "same_phone", 0.9, status="dismissed")
    await db.commit()
    await crud.upsert_identity(db, channel="signal", external_id="b", phone="+15550000001")
    await f.identity(db, "1", phone="15550000001")
    assert len(await _people(db)) == 2
    assert [s[3] for s in await _suggestions(db)] == ["dismissed"]


async def _rich_pair(db):
    """Two people to merge, each with CRM rows. Returns (keep, gone, ids)."""
    org_x, _ = await crud.get_or_create_organization(db, "X Corp")
    org_y, _ = await crud.get_or_create_organization(db, "Y Labs")
    conv_keep = await f.conversation(db, "10", title="Keep")
    conv_gone = await f.conversation(db, "20", title="Gone")
    keep, gone = conv_keep.peer_identity.person_id, conv_gone.peer_identity.person_id
    await f.message(db, conv_gone, 1, "hi", sender=conv_gone.peer_identity, date=datetime(2026, 1, 2))
    p_keep = PersonPosition(person_id=keep, organization_id=org_x.id, organization=org_x.name, title="Old",
                            is_active=False, ended_at=datetime(2024, 1, 1))
    p_gone_x = PersonPosition(person_id=gone, organization_id=org_x.id, organization=org_x.name, title="CTO",
                              is_active=True)
    p_gone_y = PersonPosition(person_id=gone, organization_id=org_y.id, organization=org_y.name, title="Advisor")
    note = PersonNote(person_id=gone, kind="context", title="Met at a conference", body="")
    item = CatchUpItem(conversation_id=conv_gone.id, person_id=gone)
    run = await crud.create_briefing_run(db)
    entry = BriefingEntry(run_id=run.id, person_id=gone, conversation_title="Gone", category="important_contact",
                          summary="s")
    db.add_all([p_keep, p_gone_x, p_gone_y, note, item, entry])
    gone_person = await db.get(Person, gone)
    gone_person.twitter_handle = "gone_tw"
    gone_person.is_important = True
    keep_person = await db.get(Person, keep)
    keep_person.notes = "kept notes"
    await db.commit()
    return keep, gone, dict(conv_gone=conv_gone.id, p_keep=p_keep.id, p_gone_x=p_gone_x.id, p_gone_y=p_gone_y.id,
                            note=note.id, item=item.id, entry=entry.id, org_x=org_x.id,
                            gone_identity=conv_gone.peer_identity.id)


@pytest.mark.asyncio
async def test_merge_and_undo_api(api, db, sent):
    keep, gone, ids = await _rich_pair(db)

    resp = await api.post(f"/api/people/{keep}/merge", json={"other_person_id": gone})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == keep and body["merge_id"]
    assert {i["id"] for i in body["identities"]} >= {ids["gone_identity"]}
    assert body["twitter_handle"] == "gone_tw" and body["notes"] == "kept notes" and body["is_important"]
    assert body["relationship_status"] == "new"  # recomputed: an incoming message only
    assert body["note_count"] == 1
    assert body["direct_conversation_ids"] and ids["conv_gone"] in body["direct_conversation_ids"]
    positions = {p["organization"]: p for p in body["positions"]}
    assert positions["X Corp"]["title"] == "CTO" and positions["X Corp"]["is_active"]  # the active one wins
    assert positions["Y Labs"]["title"] == "Advisor"
    assert (await api.get(f"/api/people/{gone}")).status_code == 404
    assert {e["person_id"] for e in sent} == {keep, gone}
    db.expire_all()
    assert (await db.get(CatchUpItem, ids["item"])).person_id == keep
    assert (await db.get(BriefingEntry, ids["entry"])).person_id == keep

    merges = (await api.get(f"/api/people/{keep}/merges")).json()
    assert [m["id"] for m in merges] == [body["merge_id"]]
    assert merges[0]["from_person_id"] == gone and merges[0]["from_name"] == "Gone"
    assert merges[0]["automatic"] is False and merges[0]["identities"][0]["channel"] == "telegram"

    sent.clear()
    undo = await api.post(f"/api/people/merges/{body['merge_id']}/undo")
    assert undo.status_code == 200, undo.text
    restored = undo.json()
    assert restored["id"] != keep
    assert [i["id"] for i in restored["identities"]] == [ids["gone_identity"]]
    assert restored["twitter_handle"] == "gone_tw" and restored["is_important"] and restored["note_count"] == 1
    assert {(p["organization"], p["title"]) for p in restored["positions"]} == {("X Corp", "CTO"), ("Y Labs", "Advisor")}
    kept = (await api.get(f"/api/people/{keep}")).json()
    assert kept["twitter_handle"] is None and kept["notes"] == "kept notes" and not kept["is_important"]
    assert [(p["organization"], p["title"]) for p in kept["positions"]] == [("X Corp", "Old")]  # recreated
    assert {e["person_id"] for e in sent} == {keep, restored["id"]}
    db.expire_all()
    assert (await db.get(CatchUpItem, ids["item"])).person_id == restored["id"]
    assert (await db.get(BriefingEntry, ids["entry"])).person_id == restored["id"]
    assert (await api.get(f"/api/people/{keep}/merges")).json() == []
    again = await api.post(f"/api/people/merges/{body['merge_id']}/undo")
    assert again.status_code == 409
    # The pair is remembered as split: no suggestion, no automatic re-merge
    assert (await api.get(f"/api/people/{keep}/merge-suggestions")).json() == []


@pytest.mark.asyncio
async def test_merge_validation(api, db):
    a = (await f.identity(db, "1")).person_id
    me = (await f.identity(db, "2", is_self=True)).person_id
    assert (await api.post(f"/api/people/{a}/merge", json={"other_person_id": a})).status_code == 400
    assert (await api.post(f"/api/people/{a}/merge", json={"other_person_id": me})).status_code == 400
    assert (await api.post(f"/api/people/{a}/merge", json={"other_person_id": 999})).status_code == 404
    assert (await api.post("/api/people/merges/999/undo")).status_code == 404


@pytest.mark.asyncio
async def test_undo_restores_earlier_merges_into_the_removed_person(db):
    a = (await f.identity(db, "1")).person_id
    b = (await f.identity(db, "2")).person_id
    c_ident = await f.identity(db, "3")
    first = await linking.merge(db, b, c_ident.person_id)  # c → b
    second = await linking.merge(db, a, b)  # b (with c) → a
    await db.commit()
    restored_b = await linking.undo(db, second.id)
    await db.commit()
    assert (await db.get(PersonMerge, first.id)).into_person_id == restored_b.id
    restored_c = await linking.undo(db, first.id)
    await db.commit()
    await db.refresh(c_ident)
    assert c_ident.person_id == restored_c.id


@pytest.mark.asyncio
async def test_detach_api(api, db, sent):
    conv = await f.conversation(db, "10", title="Ann")
    tg = conv.peer_identity
    sig = await crud.upsert_identity(db, channel="signal", external_id="s", display_name="Ann S")
    await linking.merge(db, tg.person_id, sig.person_id)
    db.add(CatchUpItem(conversation_id=conv.id, person_id=tg.person_id))
    await db.commit()
    pid, tg_id, sig_id = tg.person_id, tg.id, sig.id

    only = await api.post(f"/api/people/{pid}/identities/999/detach")
    assert only.status_code == 404
    resp = await api.post(f"/api/people/{pid}/identities/{tg_id}/detach")
    assert resp.status_code == 200, resp.text
    new = resp.json()
    assert new["id"] != pid and [i["id"] for i in new["identities"]] == [tg_id]
    assert new["direct_conversation_ids"] == [conv.id]  # the peer stays with the identity
    old = (await api.get(f"/api/people/{pid}")).json()
    assert [i["id"] for i in old["identities"]] == [sig_id] and old["direct_conversation_ids"] == []
    db.expire_all()
    item = (await db.execute(select(CatchUpItem))).scalar_one()
    assert item.person_id == new["id"]
    assert {e["person_id"] for e in sent} == {pid, new["id"]}
    last = await api.post(f"/api/people/{pid}/identities/{sig_id}/detach")
    assert last.status_code == 400  # the only identity left
    # Detached people are not linked again automatically
    await crud.upsert_identity(db, channel="signal", external_id="s", phone="+15550000001")
    await f.identity(db, "10", phone="15550000001")
    db.expire_all()
    assert (await crud.get_identity(db, tg_id)).person_id == new["id"]


@pytest.mark.asyncio
async def test_suggestions_same_name_in_group_and_username_email(api, db, sent):
    group = await f.conversation(db, "-100", title="Builders", kind="group")
    x1 = await f.identity(db, "1", display_name="Pat Doe")
    x2 = await f.identity(db, "2", display_name="pat  doe")
    stranger = await f.identity(db, "3", display_name="Pat Doe")  # same name, never in the group
    await f.message(db, group, 1, sender=x1)
    await f.message(db, group, 2, sender=x2)
    tg_user = await f.identity(db, "4", display_name="Robin", username="robin_k")
    mail = await crud.upsert_identity(db, channel="gmail", external_id="robin_k@example.com", display_name="R K")
    # The username ↔ email suggestion appears as soon as the email identity exists
    assert await _suggestions(db) == [
        tuple(sorted((tg_user.person_id, mail.person_id))) + ("username_matches_email", "pending")
    ]

    refreshed = await api.post("/api/people/merge-suggestions/refresh")
    assert refreshed.status_code == 200
    listed = (await api.get("/api/people/merge-suggestions")).json()
    pairs = {(s["person_a"]["id"], s["person_b"]["id"], s["reason"]) for s in listed}
    assert pairs == {
        (x1.person_id, x2.person_id, "same_name_shared_group"),
        tuple(sorted((tg_user.person_id, mail.person_id))) + ("username_matches_email",),
    }
    assert stranger.person_id not in {pid for p in pairs for pid in p[:2]}
    assert set(listed[0]) == {"id", "reason", "score", "created_at", "person_a", "person_b"}

    mine = (await api.get(f"/api/people/{x1.person_id}/merge-suggestions")).json()
    assert len(mine) == 1 and mine[0]["reason"] == "same_name_shared_group"
    sent.clear()
    assert (await api.post(f"/api/people/merge-suggestions/{mine[0]['id']}/dismiss")).json() == {"success": True}
    assert {e["person_id"] for e in sent} == {x1.person_id, x2.person_id}
    assert (await api.get(f"/api/people/{x1.person_id}/merge-suggestions")).json() == []
    await api.post("/api/people/merge-suggestions/refresh")
    assert (await api.get(f"/api/people/{x1.person_id}/merge-suggestions")).json() == []  # stays dismissed
    assert (await api.post("/api/people/merge-suggestions/999/dismiss")).status_code == 404


@pytest.mark.asyncio
async def test_username_change_suggests_matching_email(db):
    mail = await crud.upsert_identity(db, channel="gmail", external_id="sam.lee@example.com")
    tg = await f.identity(db, "8", display_name="Sam")
    assert await _suggestions(db) == []
    await f.identity(db, "8", username="Sam.Lee")
    assert await _suggestions(db) == [
        tuple(sorted((tg.person_id, mail.person_id))) + ("username_matches_email", "pending")
    ]
