"""Organizations keyed by FK; people, positions and relationship status (review §2-D, §2-B, §3)."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.db import crud
from backend.db.models import Organization, Person
from tests import factories as f


async def _person(db, ext="1", name="P", **profile) -> int:
    return (await f.identity(db, ext, display_name=name, **profile)).person_id


async def _add_pos(api, pid, org, **kw):
    r = await api.post(f"/api/people/{pid}/positions", json={"organization": org, **kw})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def sent(monkeypatch):
    events = []

    async def _capture(data):
        events.append(data)

    from backend.ws.manager import ws_manager
    monkeypatch.setattr(ws_manager, "broadcast", _capture)
    return events


@pytest.mark.asyncio
async def test_positions_match_orgs_case_insensitively(api, db):
    p1, p2 = await _person(db, "1"), await _person(db, "2")
    a = await _add_pos(api, p1, "Acme")
    b = await _add_pos(api, p2, "  acme ")
    assert a["organization_id"] == b["organization_id"]
    assert b["organization"] == "Acme" and b["person_id"] == p2
    r = await api.post(f"/api/people/{p1}/positions", json={"organization": "ACME"})
    assert r.status_code == 409
    assert (await api.post(f"/api/people/{p1}/positions", json={"organization": "  "})).status_code == 400
    orgs = (await api.get("/api/organizations")).json()
    assert [(o["name"], o["member_count"]) for o in orgs] == [("Acme", 2)]


@pytest.mark.asyncio
async def test_positions_of_unknown_person_are_404(api):
    assert (await api.post("/api/people/999/positions", json={"organization": "Beta"})).status_code == 404
    assert (await api.get("/api/people/999/positions")).status_code == 404
    assert (await api.get("/api/people/999")).status_code == 404


@pytest.mark.asyncio
async def test_create_org_rejects_blank_and_duplicate(api):
    assert (await api.post("/api/organizations", json={"name": "   "})).status_code == 400
    org = (await api.post("/api/organizations", json={"name": "Gamma"})).json()
    r = await api.post("/api/organizations", json={"name": "gamma "})
    assert r.status_code == 409
    assert r.json()["existing_id"] == org["id"]


@pytest.mark.asyncio
async def test_rename_onto_existing_requires_merge_and_merges(api, db):
    p1, p2 = await _person(db, "1", "Ann"), await _person(db, "2", "Bob")
    a = await _add_pos(api, p1, "Alpha", title="CTO")
    await _add_pos(api, p2, "Alpha")
    omega = await _add_pos(api, p1, "Omega", is_active=False)

    r = await api.put(f"/api/organizations/{a['organization_id']}", json={"name": "omega"})
    assert r.status_code == 409 and r.json()["code"] == "organization_exists"

    r = await api.put(f"/api/organizations/{a['organization_id']}", json={"name": "omega", "merge": True})
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["id"] == omega["organization_id"]
    # person 1 had positions at both: the active titled one wins
    assert [(m["person_id"], m["name"], m["title"]) for m in merged["members"]] == [(p1, "Ann", "CTO"), (p2, "Bob", None)]
    assert merged["former_members"] == []
    assert await db.get(Organization, a["organization_id"]) is None


@pytest.mark.asyncio
async def test_rename_updates_position_names(api, db):
    pid = await _person(db)
    p = await _add_pos(api, pid, "Delta")
    r = await api.put(f"/api/organizations/{p['organization_id']}", json={"name": "Delta Labs"})
    assert r.status_code == 200
    assert (await api.get(f"/api/people/{pid}/positions")).json()[0]["organization"] == "Delta Labs"


@pytest.mark.asyncio
async def test_delete_org_detach_keeps_history_and_delete_mode_removes(api, db):
    p1, p2 = await _person(db, "1"), await _person(db, "2")
    p = await _add_pos(api, p1, "Eps")
    await _add_pos(api, p2, "Eps", is_active=False)
    r = await api.delete(f"/api/organizations/{p['organization_id']}")
    assert r.json() == {"success": True, "mode": "detach", "positions_detached": 2, "positions_deleted": 0}
    pos = (await api.get(f"/api/people/{p1}/positions")).json()
    assert pos[0]["organization"] == "Eps" and pos[0]["organization_id"] is None

    # auto-discover re-creates the org and re-attaches the history
    assert (await api.post("/api/organizations/auto-discover")).json() == {"created": 1, "total": 1}
    org = (await api.get("/api/organizations")).json()[0]
    assert (org["member_count"], org["former_member_count"]) == (1, 1)

    r = await api.delete(f"/api/organizations/{org['id']}", params={"mode": "delete"})
    assert r.json()["positions_deleted"] == 2
    assert (await api.get(f"/api/people/{p1}/positions")).json() == []
    assert (await api.delete("/api/organizations/999", params={"mode": "nuke"})).status_code == 422


@pytest.mark.asyncio
async def test_deleting_person_cascades_positions(api, db):
    pid = await _person(db)
    await _add_pos(api, pid, "Kappa")
    await db.delete(await db.get(Person, pid))
    await db.commit()
    org = (await api.get("/api/organizations")).json()[0]
    assert org["member_count"] == 0


@pytest.mark.asyncio
async def test_org_detail_related_conversations_escape_like_and_batch_latest(api, db):
    team = await f.conversation(db, "-10", title="100% Team", kind="group")
    await f.conversation(db, "-11", title="1000 Team", kind="group")
    await f.message(db, team, 1, "old", date=datetime(2026, 1, 1))
    bob = await f.identity(db, "5", display_name="Bob")
    await f.message(db, team, 2, "new", date=datetime(2026, 1, 2), sender=bob)
    org = (await api.post("/api/organizations", json={"name": "100%"})).json()
    related = org["related_conversations"]
    assert [c["conversation_id"] for c in related] == [team.id]
    assert related[0]["conversation_title"] == "100% Team" and related[0]["conversation_kind"] == "group"
    assert related[0]["last_message"]["text"] == "new" and related[0]["last_message"]["sender_name"] == "Bob"
    assert related[0]["last_message"]["date"].endswith("Z")


@pytest.mark.asyncio
async def test_generate_context_uses_registry_and_compare_and_set(api, db):
    org = (await api.post("/api/organizations", json={"name": "Zeta"})).json()

    async def fake_complete(messages, **kw):
        # A manual edit lands while the LLM is running
        await api.put(f"/api/organizations/{org['id']}", json={"context": "manual"})
        return "generated"

    with patch("backend.api.organizations.complete_text", fake_complete):
        r = await api.post(f"/api/organizations/{org['id']}/generate-context")
    assert r.status_code == 409
    assert (await api.get(f"/api/organizations/{org['id']}")).json()["context"] == "manual"

    async def ok_complete(messages, **kw):
        return "generated"

    with patch("backend.api.organizations.complete_text", ok_complete):
        r = await api.post(f"/api/organizations/{org['id']}/generate-context")
    assert r.status_code == 200 and r.json()["context"] == "generated"

    from backend.llm.base import LLMError
    from backend.llm.resolve import LLMUnavailable

    async def unavailable(messages, **kw):
        raise LLMUnavailable("none")

    async def failing(messages, **kw):
        raise LLMError("boom")

    with patch("backend.api.organizations.complete_text", unavailable):
        assert (await api.post(f"/api/organizations/{org['id']}/generate-context")).status_code == 503
    with patch("backend.api.organizations.complete_text", failing):
        assert (await api.post(f"/api/organizations/{org['id']}/generate-context")).status_code == 502


@pytest.mark.asyncio
async def test_people_list_server_side_status_filter_and_total(api, db):
    for i in range(5):
        pid = await _person(db, str(100 + i), f"P{i}")
        (await db.get(Person, pid)).relationship_status = "needs_reply" if i % 2 == 0 else "active"
    await db.commit()
    body = (await api.get("/api/people", params={"status": "needs_reply", "limit": 2})).json()
    assert body["total"] == 3 and [p["name"] for p in body["items"]] == ["P0", "P2"]
    assert body["status_counts"] == {"needs_reply": 3, "active": 2}
    body = (await api.get("/api/people", params={"status": "needs_reply", "limit": 2, "offset": 2})).json()
    assert [p["name"] for p in body["items"]] == ["P4"]
    assert (await api.get("/api/people", params={"status": "bogus"})).status_code == 400


@pytest.mark.asyncio
async def test_people_list_important_filter_and_count(api, db):
    rows = [("A", True, "active"), ("B", True, "needs_reply"), ("C", False, "active"), ("D", True, "active")]
    for i, (name, imp, st) in enumerate(rows):
        person = await db.get(Person, await _person(db, str(500 + i), name))
        person.is_important, person.relationship_status = imp, st
    await db.commit()
    body = (await api.get("/api/people", params={"important": "true"})).json()
    assert body["total"] == 3 and {p["name"] for p in body["items"]} == {"A", "B", "D"}
    assert body["status_counts"] == {"active": 2, "needs_reply": 1}
    assert body["important_count"] == 3
    body = (await api.get("/api/people", params={"important": "true", "status": "active", "limit": 1})).json()
    assert body["total"] == 2 and len(body["items"]) == 1
    body = (await api.get("/api/people", params={"important": "false", "search": "a"})).json()
    assert body["total"] == 1 and body["important_count"] == 1  # "A" only; filter off
    assert body["status_counts"] == {"active": 1}


@pytest.mark.asyncio
async def test_people_list_contacts_only_search_and_never_self(api, db):
    await _person(db, "1", "Me", is_self=True)
    await _person(db, "2", "Saved Sam", is_contact=True)
    await _person(db, "3", "Group Gus", username="gussy")
    body = (await api.get("/api/people")).json()
    assert sorted(p["name"] for p in body["items"]) == ["Group Gus", "Saved Sam"]
    body = (await api.get("/api/people", params={"contacts_only": "true"})).json()
    assert [(p["name"], p["is_contact"]) for p in body["items"]] == [("Saved Sam", True)]
    body = (await api.get("/api/people", params={"search": "GUSS"})).json()  # identity username
    assert [p["name"] for p in body["items"]] == ["Group Gus"]


@pytest.mark.asyncio
async def test_person_payload_update_and_broadcast(api, db, sent):
    pid = await _person(db, "77", "Theo", username="theo")
    await _add_pos(api, pid, "Theta", title="Eng")
    person = (await api.get(f"/api/people/{pid}")).json()
    assert person["id"] == pid and person["name"] == "Theo" and person["is_contact"] is False
    assert [i["username"] for i in person["identities"]] == ["theo"]
    assert person["primary_position"] == {"title": "Eng", "organization": "Theta",
                                          "organization_id": person["positions"][0]["organization_id"]}
    assert person["note_count"] == 0 and person["direct_conversation_ids"] == []
    assert person["status_explanation"]

    r = await api.put(f"/api/people/{pid}", json={
        "display_name": "Theodore", "twitter_handle": " @theo ", "linkedin_url": "linkedin.com/in/theo",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["name"], body["display_name"], body["twitter_handle"], body["linkedin_url"]) == (
        "Theodore", "Theodore", "@theo", "https://linkedin.com/in/theo")
    assert (await api.put(f"/api/people/{pid}", json={"linkedin_url": "https://evil.com/x"})).status_code == 400
    body = (await api.put(f"/api/people/{pid}", json={"display_name": ""})).json()
    assert body["name"] == "Theo" and body["display_name"] is None  # falls back to the identity

    body = (await api.post(f"/api/people/{pid}/important", json={"is_important": True})).json()
    assert body["is_important"] is True and body["attention_reason"]
    assert {"type": "person_changed", "person_id": pid} in sent
    assert all(e == {"type": "person_changed", "person_id": pid} for e in sent)


@pytest.mark.asyncio
async def test_position_update_and_delete_are_scoped_to_person(api, db):
    p1, p2 = await _person(db, "1"), await _person(db, "2")
    pos = await _add_pos(api, p1, "Iota")
    assert (await api.put(f"/api/people/{p2}/positions/{pos['id']}", json={"title": "x"})).status_code == 404
    r = await api.put(f"/api/people/{p1}/positions/{pos['id']}",
                      json={"is_active": False, "ended_at": "2025-06-01T00:00:00Z"})
    assert r.json()["is_active"] is False and r.json()["ended_at"].startswith("2025-06-01")
    r = await api.put(f"/api/people/{p1}/positions/{pos['id']}", json={"is_active": True})
    assert r.json()["ended_at"] is None
    assert (await api.delete(f"/api/people/{p2}/positions/{pos['id']}")).status_code == 404
    assert (await api.delete(f"/api/people/{p1}/positions/{pos['id']}")).json() == {"success": True}


@pytest.mark.asyncio
async def test_relationship_status_aggregates_every_direct_conversation(api, db):
    """One person, two identities on two channels: the status sees both threads."""
    tg = await f.conversation(db, "77", title="Ann")
    mail_ident = await f.identity(db, "ann@x.io", channel="gmail", display_name="Ann")
    # link the gmail identity to the Telegram identity's person (what Phase 2 linking does)
    person_id = (await crud.get_conversation(db, tg.id)).peer_identity.person_id
    mail_ident.person_id = person_id
    await db.commit()
    mail = await f.conversation(db, "t1", title="Ann", kind="direct", channel="gmail", peer=mail_ident)

    now = datetime.utcnow()
    await f.message(db, tg, 1, "hi", out=True, date=now - timedelta(days=3))
    await f.message(db, mail, 1, "reply please", date=now - timedelta(days=1))
    assert set((await api.get(f"/api/people/{person_id}")).json()["direct_conversation_ids"]) == {tg.id, mail.id}

    assert (await api.post("/api/people/refresh-statuses")).json() == {"refreshed": 1}
    body = (await api.get(f"/api/people/{person_id}")).json()
    assert body["relationship_status"] == "needs_reply" and body["days_since_incoming"] == 1

    await f.message(db, tg, 2, "answered", out=True, date=now)
    r = await api.post(f"/api/people/{person_id}/relationship-status/auto")
    assert r.json()["relationship_status"] == "active" and r.json()["status_source"] == "auto"

    r = await api.post(f"/api/people/{person_id}/relationship-status", json={"status": "cold"})
    assert (r.json()["relationship_status"], r.json()["status_source"]) == ("cold", "manual")
    assert (await api.post("/api/people/refresh-statuses")).json() == {"refreshed": 0}  # manual kept
    assert (await api.post(f"/api/people/{person_id}/relationship-status", json={"status": "x"})).status_code == 400


@pytest.mark.asyncio
async def test_reaction_acknowledges_latest_incoming(db):
    conv = await f.conversation(db, "9", title="Rae")
    now = datetime.utcnow()
    await f.message(db, conv, 1, "hi", out=True, date=now - timedelta(days=2))
    await f.message(db, conv, 2, "thanks!", date=now - timedelta(days=1), my_reaction="👍")
    person = await crud.get_person(db, conv.peer_identity.person_id)
    assert (await crud.compute_relationship_status(db, person))[0] == "active"
    other = await crud.get_person(db, await _person(db, "10"))
    assert await crud.compute_relationship_status(db, other) == ("new", None, None, None)


@pytest.mark.asyncio
async def test_address_book_sync_marks_identities_as_contacts(api, db):
    from telethon.tl.types import User

    stale = await f.identity(db, "300", display_name="Gone", is_contact=True)
    users = [User(id=100, first_name="Ann", contact=True, phone="15550001"),
             User(id=200, first_name="Bot", bot=True, contact=True)]

    class FakeClient:
        async def __call__(self, _req):
            return SimpleNamespace(users=users)

    async def connected(require=True):
        return FakeClient()

    from backend.telegram.runtime import runtime
    with patch("backend.telegram.client.connected_client", connected):
        r = await api.post("/api/people/sync")
    assert r.json() == {"imported": 2, "total": 2}
    body = (await api.get("/api/people", params={"contacts_only": "true"})).json()
    assert sorted(p["name"] for p in body["items"]) == ["Ann", "Bot"]
    await db.refresh(stale)
    assert stale.is_contact is False
    with patch("backend.telegram.client.connected_client", connected):
        assert (await api.post("/api/people/sync")).json() == {"imported": 0, "total": 2}


@pytest.mark.asyncio
async def test_dashboard_attention_people_use_live_status(api, db):
    conv = await f.conversation(db, "42", title="Vip", is_tracked=True)
    person = await crud.get_person(db, conv.peer_identity.person_id)
    person.is_important, person.relationship_status = True, "active"  # stale stored status
    await db.commit()
    now = datetime.utcnow()
    await f.message(db, conv, 1, "hi", out=True, date=now - timedelta(hours=3))
    await f.message(db, conv, 2, "ping?", date=now - timedelta(hours=1))
    body = (await api.get("/api/dashboard")).json()
    assert body["stats"]["unread_today"] == 1
    [vip] = body["attention_people"]
    assert (vip["id"], vip["relationship_status"]) == (person.id, "needs_reply")
    assert vip["attention_reason"] == "Important contact is waiting for your reply."
    assert [r["conversation"]["id"] for r in body["needs_reply"]] == [conv.id]


@pytest.mark.asyncio
async def test_service_rows_never_count_as_awaiting_reply(api, db):
    conv = await f.conversation(db, "55", title="Sam")
    pid = conv.peer_identity.person_id
    await api.post(f"/api/people/{pid}/important", json={"is_important": True})
    now = datetime.utcnow()
    await f.message(db, conv, 1, "hi", out=True, date=now - timedelta(hours=5))
    await f.message(db, conv, 2, "Sam joined Telegram", kind="service", date=now - timedelta(hours=4))
    await f.message(db, conv, 3, "Missed call", kind="service", date=now - timedelta(hours=3))
    body = (await api.post(f"/api/people/{pid}/relationship-status/auto")).json()
    assert body["relationship_status"] == "active"
    assert body["last_incoming_at"] is None
    await f.message(db, conv, 4, "hey, are you there?", date=now - timedelta(hours=1))
    body = (await api.post(f"/api/people/{pid}/relationship-status/auto")).json()
    assert body["relationship_status"] == "needs_reply"
