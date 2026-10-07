"""Organizations keyed by FK; contacts/people/positions; conversations CAS (review §2-D, §2-B, §3)."""

from datetime import datetime
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from backend.db.models import Chat, Conversation, ConversationMessage, Message, Organization


@pytest_asyncio.fixture
async def papi(api):
    """`api` with /api/people mounted (router.py registration lives elsewhere)."""
    app = api._transport.app
    if not any(getattr(r, "path", "").startswith("/api/people") for r in app.routes):
        from fastapi import Depends
        from backend.api.contacts import people_router
        from backend.api.dependencies import require_session_token
        app.include_router(people_router, prefix="/api/people", dependencies=[Depends(require_session_token)])
        # ahead of the SPA catch-all, as a registration in create_app would be
        routes = app.router.routes
        people = [r for r in routes if getattr(r, "path", "").startswith("/api/people")]
        routes[:] = people + [r for r in routes if r not in people]
    return api


async def _add_pos(api, tid, org, **kw):
    r = await api.post(f"/api/people/{tid}/positions", json={"organization": org, **kw})
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_positions_match_orgs_case_insensitively(papi, db):
    a = await _add_pos(papi, 1, "Acme")
    b = await _add_pos(papi, 2, "  acme ")
    assert a["organization_id"] == b["organization_id"]
    assert b["organization"] == "Acme"
    r = await papi.post("/api/people/1/positions", json={"organization": "ACME"})
    assert r.status_code == 409
    assert (await papi.post("/api/people/1/positions", json={"organization": "  "})).status_code == 400
    orgs = (await papi.get("/api/organizations")).json()
    assert [(o["name"], o["member_count"]) for o in orgs] == [("Acme", 2)]


@pytest.mark.asyncio
async def test_old_positions_routes_are_aliases(papi):
    r = await papi.post("/api/contacts/5/positions", json={"organization": "Beta"})
    assert r.status_code == 200
    assert (await papi.get("/api/people/5/positions")).json()[0]["organization"] == "Beta"
    assert (await papi.delete(f"/api/contacts/5/positions/{r.json()['id']}")).status_code == 200


@pytest.mark.asyncio
async def test_create_org_rejects_blank_and_duplicate(papi):
    assert (await papi.post("/api/organizations", json={"name": "   "})).status_code == 400
    org = (await papi.post("/api/organizations", json={"name": "Gamma"})).json()
    r = await papi.post("/api/organizations", json={"name": "gamma "})
    assert r.status_code == 409
    assert r.json()["existing_id"] == org["id"]


@pytest.mark.asyncio
async def test_rename_onto_existing_requires_merge_and_merges(papi, db):
    a = await _add_pos(papi, 1, "Alpha", title="CTO")
    await _add_pos(papi, 2, "Alpha")
    await _add_pos(papi, 1, "Omega", is_active=False)
    omega_id = (await papi.get("/api/people/1/positions")).json()
    omega_id = next(p["organization_id"] for p in omega_id if p["organization"] == "Omega")

    r = await papi.put(f"/api/organizations/{a['organization_id']}", json={"name": "omega"})
    assert r.status_code == 409 and r.json()["code"] == "organization_exists"

    r = await papi.put(f"/api/organizations/{a['organization_id']}", json={"name": "omega", "merge": True})
    assert r.status_code == 200, r.text
    merged = r.json()
    assert merged["id"] == omega_id
    # person 1 had positions at both: the active titled one wins
    assert sorted((m["telegram_id"], m["title"]) for m in merged["members"]) == [(1, "CTO"), (2, None)]
    assert merged["former_members"] == []
    assert await db.get(Organization, a["organization_id"]) is None


@pytest.mark.asyncio
async def test_rename_updates_position_names(papi):
    p = await _add_pos(papi, 1, "Delta")
    r = await papi.put(f"/api/organizations/{p['organization_id']}", json={"name": "Delta Labs"})
    assert r.status_code == 200
    assert (await papi.get("/api/people/1/positions")).json()[0]["organization"] == "Delta Labs"


@pytest.mark.asyncio
async def test_delete_org_detach_keeps_history_and_delete_mode_removes(papi, db):
    p = await _add_pos(papi, 1, "Eps")
    await _add_pos(papi, 2, "Eps", is_active=False)
    r = await papi.delete(f"/api/organizations/{p['organization_id']}")
    assert r.json() == {"success": True, "mode": "detach", "positions_detached": 2, "positions_deleted": 0}
    pos = (await papi.get("/api/people/1/positions")).json()
    assert pos[0]["organization"] == "Eps" and pos[0]["organization_id"] is None

    # auto-discover re-creates the org and re-attaches the history
    assert (await papi.post("/api/organizations/auto-discover")).json() == {"created": 1, "total": 1}
    org = (await papi.get("/api/organizations")).json()[0]
    assert (org["member_count"], org["former_member_count"]) == (1, 1)

    r = await papi.delete(f"/api/organizations/{org['id']}", params={"mode": "delete"})
    assert r.json()["positions_deleted"] == 2
    assert (await papi.get("/api/people/1/positions")).json() == []
    assert (await papi.delete("/api/organizations/999", params={"mode": "nuke"})).status_code == 422


@pytest.mark.asyncio
async def test_org_detail_related_chats_escape_like_and_batch_latest(papi, db):
    db.add_all([
        Chat(telegram_id=-10, name="100% Team", type="group"),
        Chat(telegram_id=-11, name="1000 Team", type="group"),
    ])
    db.add(Message(tg_message_id=1, chat_id=-10, text="old", date=datetime(2026, 1, 1)))
    db.add(Message(tg_message_id=2, chat_id=-10, text="new", date=datetime(2026, 1, 2), sender_name="Bob"))
    await db.commit()
    org = (await papi.post("/api/organizations", json={"name": "100%"})).json()
    assert [c["chat_id"] for c in org["related_chats"]] == [-10]
    assert org["related_chats"][0]["last_message"]["text"] == "new"
    assert org["related_chats"][0]["last_message"]["date"].endswith("Z")


@pytest.mark.asyncio
async def test_generate_context_uses_registry_and_compare_and_set(papi, db):
    org = (await papi.post("/api/organizations", json={"name": "Zeta"})).json()

    async def fake_complete(messages, **kw):
        # A manual edit lands while the LLM is running
        await papi.put(f"/api/organizations/{org['id']}", json={"context": "manual"})
        return "generated"

    with patch("backend.api.organizations.complete_text", fake_complete):
        r = await papi.post(f"/api/organizations/{org['id']}/generate-context")
    assert r.status_code == 409
    assert (await papi.get(f"/api/organizations/{org['id']}")).json()["context"] == "manual"

    async def ok_complete(messages, **kw):
        return "generated"

    with patch("backend.api.organizations.complete_text", ok_complete):
        r = await papi.post(f"/api/organizations/{org['id']}/generate-context")
    assert r.status_code == 200 and r.json()["context"] == "generated"

    from backend.llm.resolve import LLMUnavailable
    from backend.llm.base import LLMError

    async def unavailable(messages, **kw):
        raise LLMUnavailable("none")

    async def failing(messages, **kw):
        raise LLMError("boom")

    with patch("backend.api.organizations.complete_text", unavailable):
        assert (await papi.post(f"/api/organizations/{org['id']}/generate-context")).status_code == 503
    with patch("backend.api.organizations.complete_text", failing):
        assert (await papi.post(f"/api/organizations/{org['id']}/generate-context")).status_code == 502


@pytest.mark.asyncio
async def test_suggest_title_never_overwrites_manual_rename(papi, db):
    conv = Conversation(title=None)
    db.add(conv)
    await db.commit()
    db.add(ConversationMessage(conversation_id=conv.id, role="user", content="hello"))
    await db.commit()

    async def rename_meanwhile(messages, **kw):
        await papi.put(f"/api/conversations/{conv.id}", json={"title": "Mine"})
        return "Auto"

    with patch("backend.api.conversations.complete_text", rename_meanwhile):
        r = await papi.post(f"/api/conversations/{conv.id}/suggest-title")
    assert r.json() == {"title": "Mine", "applied": False}
    title = (await db.execute(select(Conversation.title).where(Conversation.id == conv.id))).scalar()
    assert title == "Mine"


@pytest.mark.asyncio
async def test_contacts_list_server_side_status_filter_and_total(papi, db):
    from backend.db.models import Contact
    for i in range(5):
        db.add(Contact(telegram_id=100 + i, first_name=f"P{i}",
                       relationship_status="needs_reply" if i % 2 == 0 else "active"))
    await db.commit()
    r = await papi.get("/api/contacts", params={"status": "needs_reply", "limit": 2})
    body = r.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    assert body["status_counts"] == {"needs_reply": 3, "active": 2}
    r = await papi.get("/api/contacts", params={"status": "needs_reply", "limit": 2, "offset": 2})
    assert len(r.json()["items"]) == 1
    assert (await papi.get("/api/contacts", params={"status": "bogus"})).status_code == 400


@pytest.mark.asyncio
async def test_create_contact_returns_existing_positions_and_people_local_view(papi):
    await _add_pos(papi, 77, "Theta", title="Eng")
    person = (await papi.get("/api/people/77")).json()
    assert person["contact"] is None and person["positions"][0]["organization"] == "Theta"
    r = await papi.post("/api/contacts", json={"telegram_id": 77, "first_name": "T"})
    assert r.status_code == 200
    assert [p["organization"] for p in r.json()["positions"]] == ["Theta"]
    again = await papi.post("/api/contacts", json={"telegram_id": 77})
    assert again.status_code == 200 and again.json()["id"] == r.json()["id"]
    person = (await papi.get("/api/people/77")).json()
    assert person["contact"]["id"] == r.json()["id"] and person["name"] == "T"


@pytest.mark.asyncio
async def test_profile_degrades_to_cached_copy_when_offline(papi):
    import backend.api.profiles as profiles
    profiles.clear_profile_cache()

    async def online(user_id):
        return {"id": 9, "type": "user", "first_name": "Nine"}

    async def offline(user_id):
        raise ConnectionError("offline")

    with patch.object(profiles, "_fetch_profile", online):
        r = await papi.get("/api/profiles/9")
    assert r.json()["stale"] is False
    with patch.object(profiles, "_fetch_profile", offline):
        r = await papi.get("/api/profiles/9", params={"refresh": True})
        assert r.status_code == 200 and r.json()["stale"] is True and r.json()["first_name"] == "Nine"
        assert (await papi.get("/api/profiles/10")).status_code == 503
    profiles.clear_profile_cache()


@pytest.mark.asyncio
async def test_contacts_list_important_filter_and_count(papi, db):
    from backend.db.models import Contact
    rows = [("A", True, "active"), ("B", True, "needs_reply"), ("C", False, "active"), ("D", True, "active")]
    for i, (name, imp, st) in enumerate(rows):
        db.add(Contact(telegram_id=500 + i, first_name=name, is_important=imp, relationship_status=st))
    await db.commit()
    body = (await papi.get("/api/contacts", params={"important": "true"})).json()
    assert body["total"] == 3 and {c["first_name"] for c in body["items"]} == {"A", "B", "D"}
    assert body["status_counts"] == {"active": 2, "needs_reply": 1}
    assert body["important_count"] == 3
    body = (await papi.get("/api/contacts", params={"important": "true", "status": "active", "limit": 1})).json()
    assert body["total"] == 2 and len(body["items"]) == 1
    body = (await papi.get("/api/contacts", params={"important": "false", "search": "a"})).json()
    assert body["total"] == 1 and body["important_count"] == 1  # "A" only; filter off
    assert body["status_counts"] == {"active": 1}
