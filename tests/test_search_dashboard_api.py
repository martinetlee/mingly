"""⌘K search, the conversation finder and the home dashboard read models."""

from datetime import datetime, timedelta

import pytest

from backend.db import crud
from tests import factories as f


@pytest.mark.asyncio
async def test_global_search_finds_messages_conversations_people_and_orgs(api, db):
    now = datetime.utcnow()
    roadmap = await f.conversation(db, "-501", title="Roadmap crew", kind="group", last_activity_at=now)
    older = await f.conversation(db, "-502", title="Roadmap archive", kind="group",
                                 last_activity_at=now - timedelta(days=3))
    await f.conversation(db, "-503", title="Roadmap hidden", kind="group", is_hidden=True)
    await f.message(db, roadmap, 1, "the roadmap is ready")
    mail = await f.conversation(db, "t-9", title="Mail", kind="email_thread", channel="gmail")
    await f.message(db, mail, 1, "body", subject="Roadmap review")
    ann = await f.identity(db, "601", display_name="Ann Roadmapper")
    await crud.add_person_position(db, ann.person_id, title="PM", organization="Acme")
    await crud.get_or_create_organization(db, "Roadmap Labs")
    await db.commit()

    body = (await api.get("/api/search", params={"q": "roadmap"})).json()
    assert {m["text"] or m["subject"] for m in body["messages"]} == {"the roadmap is ready", "body"}
    assert next(m for m in body["messages"] if m["subject"] == "Roadmap review")["conversation_title"] == "Mail"
    assert [c["title"] for c in body["conversations"]] == ["Roadmap crew", "Roadmap archive"]
    [person] = body["people"]
    assert person["id"] == ann.person_id and person["positions"][0]["organization"] == "Acme"
    assert [o["name"] for o in body["organizations"]] == ["Roadmap Labs"]
    assert (await api.get("/api/search", params={"q": "r"})).status_code == 422

    finder = (await api.get("/api/conversations/search", params={"q": "roadmap"})).json()
    assert [c["id"] for c in finder["local"]] == [roadmap.id, older.id]
    assert (await api.get("/api/conversations/search", params={"q": "  "})).json() == {"local": []}


@pytest.mark.asyncio
async def test_dashboard_counts_relevant_unread_and_shows_latest_briefing(api, db):
    now = datetime.utcnow()
    vip = await f.identity(db, "701", display_name="Vip")
    vip_person = await crud.get_person(db, vip.person_id)
    vip_person.is_important = True
    await db.commit()
    dm = await f.conversation(db, "701", title="Vip", peer=vip)
    tracked = await f.conversation(db, "-702", title="Team", kind="group", is_tracked=True)
    other = await f.conversation(db, "-703", title="Noise", kind="group")
    await f.message(db, dm, 1, "ping", sender=vip, date=now - timedelta(hours=1))
    await f.message(db, tracked, 1, "update", date=now - timedelta(hours=2))
    await f.message(db, tracked, 2, "mine", out=True, date=now - timedelta(hours=2))
    await f.message(db, tracked, 3, "old", date=now - timedelta(days=2))
    await f.message(db, other, 1, "noise", date=now - timedelta(hours=1))

    run = await crud.create_briefing_run(db, days=1)
    for i in range(7):
        await crud.add_briefing_entry(db, run.id, conversation_id=tracked.id, category="tracked",
                                      conversation_title=f"C{i}", summary=f"s{i}")
    await db.commit()

    body = (await api.get("/api/dashboard")).json()
    assert body["stats"]["unread_today"] == 2
    assert body["latest_briefing"]["id"] == run.id
    assert [e["summary"] for e in body["latest_briefing"]["entries"]] == ["s0", "s1", "s2", "s3", "s4"]
    assert [p["id"] for p in body["attention_people"]] == [vip.person_id]
