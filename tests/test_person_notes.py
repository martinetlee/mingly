"""Person notes REST API (/api/people/{telegram_id}/notes) and its WS event."""

from datetime import datetime

import pytest

from backend.db.models import Chat, Message, PersonNote

PERSON = 4242


@pytest.fixture
def sent(monkeypatch):
    events = []

    async def _capture(data):
        events.append(data)

    from backend.ws.manager import ws_manager
    monkeypatch.setattr(ws_manager, "broadcast", _capture)
    return events


async def _seed(db):
    db.add(Chat(telegram_id=PERSON, name="Pat", type="private"))
    db.add(Message(tg_message_id=7, chat_id=PERSON, sender_id=PERSON, text="hi", date=datetime(2025, 1, 1)))
    await db.commit()


@pytest.mark.asyncio
async def test_notes_crud_and_broadcast(api, db, sent):
    await _seed(db)
    assert (await api.get(f"/api/people/{PERSON}/notes")).json() == []

    resp = await api.post(f"/api/people/{PERSON}/notes", json={
        "kind": "context", "title": "  Met at ETHDenver ", "body": "**Founder** of X",
        "citations": [{"chat_id": PERSON, "tg_message_id": 7}],
    })
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["title"] == "Met at ETHDenver"
    assert first["author"] == "user" and first["agent_label"] is None
    assert first["citations"] == [{"chat_id": PERSON, "tg_message_id": 7}]
    assert first["created_at"].endswith("Z") and first["updated_at"].endswith("Z")
    assert sent == [{"type": "person_notes_changed", "telegram_id": PERSON}]

    second = (await api.post(f"/api/people/{PERSON}/notes", json={
        "kind": "todo", "title": "Follow up", "body": "",
    })).json()
    listed = (await api.get(f"/api/people/{PERSON}/notes")).json()
    assert [n["id"] for n in listed] == [second["id"], first["id"]]  # newest first
    assert set(listed[0]) == {
        "id", "telegram_id", "kind", "title", "body", "citations", "author",
        "agent_label", "created_at", "updated_at",
    }

    upd = await api.put(f"/api/people/{PERSON}/notes/{first['id']}", json={"kind": "risk", "citations": []})
    assert upd.status_code == 200
    assert upd.json()["kind"] == "risk" and upd.json()["citations"] == []
    assert upd.json()["title"] == "Met at ETHDenver"

    assert (await api.delete(f"/api/people/{PERSON}/notes/{second['id']}")).json() == {"success": True}
    assert [n["id"] for n in (await api.get(f"/api/people/{PERSON}/notes")).json()] == [first["id"]]
    assert len(sent) == 4 and all(e == {"type": "person_notes_changed", "telegram_id": PERSON} for e in sent)


@pytest.mark.asyncio
async def test_notes_validation(api, db, sent):
    await _seed(db)
    bad_kind = await api.post(f"/api/people/{PERSON}/notes", json={"kind": "gossip", "title": "t"})
    assert bad_kind.status_code == 400 and "kind" in bad_kind.json()["detail"]
    empty = await api.post(f"/api/people/{PERSON}/notes", json={"kind": "context", "title": "  "})
    assert empty.status_code == 400
    missing = await api.post(f"/api/people/{PERSON}/notes", json={
        "kind": "context", "title": "t", "citations": [{"chat_id": PERSON, "tg_message_id": 8}],
    })
    assert missing.status_code == 400 and "not found locally" in missing.json()["detail"]
    assert sent == []


@pytest.mark.asyncio
async def test_note_scoped_to_person(api, db, sent):
    await _seed(db)
    db.add(PersonNote(telegram_id=PERSON, kind="context", title="agent says", body="", author="agent",
                      agent_label="claude"))
    await db.commit()
    note = (await api.get(f"/api/people/{PERSON}/notes")).json()[0]
    assert note["author"] == "agent" and note["agent_label"] == "claude"
    # The user may edit an agent note; the author badge stays
    edited = (await api.put(f"/api/people/{PERSON}/notes/{note['id']}", json={"body": "corrected"})).json()
    assert edited["author"] == "agent" and edited["body"] == "corrected"
    # Wrong person in the path = not found
    assert (await api.put(f"/api/people/1/notes/{note['id']}", json={"body": "x"})).status_code == 404
    assert (await api.delete(f"/api/people/1/notes/{note['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_notes_require_session(db_engine):
    from httpx import ASGITransport, AsyncClient
    from backend.api.router import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1") as client:
        assert (await client.get(f"/api/people/{PERSON}/notes")).status_code == 401
