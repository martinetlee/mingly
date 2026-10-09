"""Person notes REST API (/api/people/{person_id}/notes) and its WS event."""

import pytest

from backend.db.models import PersonNote
from tests import factories as f


@pytest.fixture
def sent(monkeypatch):
    events = []

    async def _capture(data):
        events.append(data)

    from backend.ws.manager import ws_manager
    monkeypatch.setattr(ws_manager, "broadcast", _capture)
    return events


async def _seed(db):
    """A person with one stored message in their direct conversation.
    Returns (person_id, conversation_id, message_id)."""
    conv = await f.conversation(db, "4242", title="Pat")
    msg = await f.message(db, conv, 7, "hi", sender=conv.peer_identity)
    return conv.peer_identity.person_id, conv.id, msg.id


@pytest.mark.asyncio
async def test_notes_crud_and_broadcast(api, db, sent):
    pid, cid, mid = await _seed(db)
    assert (await api.get(f"/api/people/{pid}/notes")).json() == []

    resp = await api.post(f"/api/people/{pid}/notes", json={
        "kind": "context", "title": "  Met at ETHDenver ", "body": "**Founder** of X",
        "citations": [{"conversation_id": cid, "message_id": mid}, {"conversation_id": cid, "message_id": mid}],
    })
    assert resp.status_code == 200, resp.text
    first = resp.json()
    assert first["title"] == "Met at ETHDenver" and first["person_id"] == pid
    assert first["author"] == "user" and first["agent_label"] is None
    assert first["citations"] == [{"conversation_id": cid, "message_id": mid}]  # deduplicated
    assert first["created_at"].endswith("Z") and first["updated_at"].endswith("Z")
    assert sent == [{"type": "person_notes_changed", "person_id": pid}]

    second = (await api.post(f"/api/people/{pid}/notes", json={
        "kind": "todo", "title": "Follow up", "body": "",
    })).json()
    listed = (await api.get(f"/api/people/{pid}/notes")).json()
    assert [n["id"] for n in listed] == [second["id"], first["id"]]  # newest first
    assert set(listed[0]) == {
        "id", "person_id", "kind", "title", "body", "citations", "author",
        "agent_label", "created_at", "updated_at",
    }
    assert (await api.get(f"/api/people/{pid}")).json()["note_count"] == 2

    upd = await api.put(f"/api/people/{pid}/notes/{first['id']}", json={"kind": "risk", "citations": []})
    assert upd.status_code == 200
    assert upd.json()["kind"] == "risk" and upd.json()["citations"] == []
    assert upd.json()["title"] == "Met at ETHDenver"

    assert (await api.delete(f"/api/people/{pid}/notes/{second['id']}")).json() == {"success": True}
    assert [n["id"] for n in (await api.get(f"/api/people/{pid}/notes")).json()] == [first["id"]]
    assert len(sent) == 4 and all(e == {"type": "person_notes_changed", "person_id": pid} for e in sent)


@pytest.mark.asyncio
async def test_notes_validation(api, db, sent):
    pid, cid, mid = await _seed(db)
    other = await f.conversation(db, "-9", title="Elsewhere", kind="group")
    bad_kind = await api.post(f"/api/people/{pid}/notes", json={"kind": "gossip", "title": "t"})
    assert bad_kind.status_code == 400 and "kind" in bad_kind.json()["detail"]
    empty = await api.post(f"/api/people/{pid}/notes", json={"kind": "context", "title": "  "})
    assert empty.status_code == 400
    for citation in ({"conversation_id": cid, "message_id": mid + 1},  # not stored
                     {"conversation_id": other.id, "message_id": mid}):  # stored, but in another conversation
        missing = await api.post(f"/api/people/{pid}/notes", json={
            "kind": "context", "title": "t", "citations": [citation],
        })
        assert missing.status_code == 400 and "not found locally" in missing.json()["detail"]
    assert (await api.post("/api/people/999/notes", json={"kind": "context", "title": "t"})).status_code == 404
    assert sent == []


@pytest.mark.asyncio
async def test_note_scoped_to_person(api, db, sent):
    pid, _cid, _mid = await _seed(db)
    other = (await f.identity(db, "1", display_name="Someone")).person_id
    db.add(PersonNote(person_id=pid, kind="context", title="agent says", body="", author="agent",
                      agent_label="claude"))
    await db.commit()
    note = (await api.get(f"/api/people/{pid}/notes")).json()[0]
    assert note["author"] == "agent" and note["agent_label"] == "claude"
    # The user may edit an agent note; the author badge stays
    edited = (await api.put(f"/api/people/{pid}/notes/{note['id']}", json={"body": "corrected"})).json()
    assert edited["author"] == "agent" and edited["body"] == "corrected"
    # Wrong person in the path = not found
    assert (await api.put(f"/api/people/{other}/notes/{note['id']}", json={"body": "x"})).status_code == 404
    assert (await api.delete(f"/api/people/{other}/notes/{note['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_notes_require_session(db_engine):
    from httpx import ASGITransport, AsyncClient
    from backend.api.router import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://127.0.0.1") as client:
        assert (await client.get("/api/people/1/notes")).status_code == 401
