"""Kanban stage-tag ownership (review §2-D): a column owns a fresh stage tag,
mutations never strip membership/scope tags, deletes clean up after themselves."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, func

from backend.db import crud
from backend.db.models import ConversationTag, Tag
from tests import factories as f


async def _group(db, ext, title="Group") -> int:
    return (await f.conversation(db, str(ext), title=title, kind="group")).id


async def _tag_exists(db, tag_id):
    return bool((await db.execute(select(func.count()).select_from(Tag).where(Tag.id == tag_id))).scalar())


async def _tags_of(db, conversation_id):
    return set((await db.execute(
        select(ConversationTag.tag_id).where(ConversationTag.conversation_id == conversation_id)
    )).scalars().all())


async def _kanban(api, name, tag_ids):
    r = await api.post("/api/workspaces", json={"name": name, "tag_ids": tag_ids, "view_mode": "kanban"})
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["main_board_id"]


@pytest_asyncio.fixture
async def investors(api, db):
    """Workspace whose include tag is "Investors", with two member conversations."""
    tag = (await api.post("/api/tags", json={"name": "Investors"})).json()
    convs = []
    for ext in (-1001, -1002):
        cid = await _group(db, ext)
        convs.append(cid)
        assert (await api.post(f"/api/conversations/{cid}/tags", json={"tag_id": tag["id"]})).status_code == 200
    ws_id, board_id = await _kanban(api, "Fundraise", [tag["id"]])
    return SimpleNamespace(tag=tag, ws_id=ws_id, board_id=board_id, a=convs[0], b=convs[1])


@pytest.mark.asyncio
async def test_column_named_like_include_tag_gets_its_own_stage_tag(api, db, investors):
    tag, ws_id, board_id, a, b = investors.tag, investors.ws_id, investors.board_id, investors.a, investors.b
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "investors"})).json()
    assert col["tag_id"] != tag["id"]
    stage = await db.get(Tag, col["tag_id"])
    assert stage.category == "stage"

    # Drag a card in and back out: membership tag must survive
    r = await api.post(f"/api/workspaces/{ws_id}/move",
                       json={"board_id": board_id, "conversation_id": a, "to_tag_id": col["tag_id"]})
    assert r.status_code == 200
    board = r.json()  # move returns the updated board
    assert [c["id"] for c in board["columns"][0]["conversations"]] == [a]
    r = await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "conversation_id": a, "to_tag_id": None})
    assert r.status_code == 200
    assert tag["id"] in await _tags_of(db, a)

    # Deleting the column never touches the user's tag
    await api.post(f"/api/workspaces/{ws_id}/move",
                   json={"board_id": board_id, "conversation_id": b, "to_tag_id": col["tag_id"]})
    r = await api.delete(f"/api/workspaces/{ws_id}/boards/{board_id}/columns/{col['tag_id']}")
    assert r.status_code == 200
    for cid in (a, b):
        assert await _tags_of(db, cid) == {tag["id"]}
    assert not await _tag_exists(db, col["tag_id"])  # stage tag died with its column


@pytest.mark.asyncio
async def test_same_label_in_two_workspaces_is_independent(api, db, investors):
    tag, ws_a, board_a, a = investors.tag, investors.ws_id, investors.board_id, investors.a
    ws_b, board_b = await _kanban(api, "Other", [tag["id"]])
    done_a = (await api.post(f"/api/workspaces/{ws_a}/boards/{board_a}/columns", json={"name": "Done"})).json()
    done_b = (await api.post(f"/api/workspaces/{ws_b}/boards/{board_b}/columns", json={"name": "Done"})).json()
    assert done_a["tag_id"] != done_b["tag_id"]
    await api.post(f"/api/workspaces/{ws_a}/move", json={"board_id": board_a, "conversation_id": a, "to_tag_id": done_a["tag_id"]})
    board_b_data = (await api.get(f"/api/workspaces/{ws_b}/board")).json()
    assert board_b_data["columns"][0]["conversations"] == []


@pytest.mark.asyncio
async def test_delete_workspace_removes_stage_tags_so_new_column_starts_empty(api, db, investors):
    tag, ws_id, board_id, a = investors.tag, investors.ws_id, investors.board_id, investors.a
    done = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "Done"})).json()
    await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "conversation_id": a, "to_tag_id": done["tag_id"]})
    assert (await api.delete(f"/api/workspaces/{ws_id}")).status_code == 204
    assert not await _tag_exists(db, done["tag_id"])
    assert await _tags_of(db, a) == {tag["id"]}

    ws2, board2 = await _kanban(api, "Again", [tag["id"]])
    await api.post(f"/api/workspaces/{ws2}/boards/{board2}/columns", json={"name": "Done"})
    assert (await api.get(f"/api/workspaces/{ws2}/board")).json()["columns"][0]["conversations"] == []


@pytest.mark.asyncio
async def test_delete_board_removes_its_stage_tags(api, db, investors):
    ws_id, board_id, b = investors.ws_id, investors.board_id, investors.b
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "New"})).json()
    await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "conversation_id": b, "to_tag_id": col["tag_id"]})
    assert (await api.delete(f"/api/workspaces/{ws_id}/boards/{board_id}")).status_code == 200
    assert not await _tag_exists(db, col["tag_id"])
    n = (await db.execute(select(func.count()).select_from(ConversationTag).where(ConversationTag.tag_id == col["tag_id"]))).scalar()
    assert n == 0


@pytest.mark.asyncio
async def test_move_never_strips_protected_tag_from_shared_column(api, db, investors):
    """A column backed directly by a protected (include) tag."""
    from backend.db.models import WorkspaceColumn
    tag, ws_id, board_id, a, b = investors.tag, investors.ws_id, investors.board_id, investors.a, investors.b
    db.add(WorkspaceColumn(workspace_id=ws_id, board_id=board_id, tag_id=tag["id"], label="Investors", position=0))
    other = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "Later"})).json()
    await db.commit()
    await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "conversation_id": a, "to_tag_id": other["tag_id"]})
    assert tag["id"] in await _tags_of(db, a)
    await api.delete(f"/api/workspaces/{ws_id}/boards/{board_id}/columns/{tag['id']}")
    assert await _tag_exists(db, tag["id"])
    assert tag["id"] in await _tags_of(db, b)


@pytest.mark.asyncio
async def test_reorder_columns_atomically(api, investors):
    ws_id, board_id = investors.ws_id, investors.board_id
    ids = [(await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": n})).json()["tag_id"]
           for n in ("A", "B", "C")]
    r = await api.put(f"/api/workspaces/{ws_id}/boards/{board_id}/columns/order", json={"tag_ids": ids[::-1]})
    assert r.status_code == 200, r.text
    assert [c["tag_id"] for c in r.json()["columns"]] == ids[::-1]
    r = await api.put(f"/api/workspaces/{ws_id}/boards/{board_id}/columns/order", json={"tag_ids": ids[:2]})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_stream_tag_never_resolves_to_stage_tag_and_frees_name(api, db, investors):
    ws_id, board_id = investors.ws_id, investors.board_id
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "Active"})).json()
    assert (await db.get(Tag, col["tag_id"])).name == "Active"
    r = await api.post(f"/api/workspaces/{ws_id}/boards", json={"name": "Stream", "scope_tag_name": "active"})
    assert r.status_code == 200, r.text
    scope = await db.get(Tag, r.json()["scope_tag_id"])
    await db.refresh(scope)
    assert scope.id != col["tag_id"] and scope.category == "stream"
    stage = await db.get(Tag, col["tag_id"])
    await db.refresh(stage)
    assert stage.name != "Active"  # renamed out of the way; column label unchanged
    board = (await api.get(f"/api/workspaces/{ws_id}/board", params={"board_id": board_id})).json()
    assert board["columns"][0]["name"] == "Active"


@pytest.mark.asyncio
async def test_user_tag_create_conflicts_case_insensitively_but_not_with_stage(api, investors):
    ws_id, board_id = investors.ws_id, investors.board_id
    assert (await api.post("/api/tags", json={"name": " investors "})).status_code == 409
    await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "Done"})
    r = await api.post("/api/tags", json={"name": "done"})
    assert r.status_code == 200, r.text
    assert (await api.post("/api/tags", json={"name": "x", "category": "stage"})).status_code == 400


@pytest.mark.asyncio
async def test_workspace_rejects_stage_tags_and_emoji_clears(api, investors):
    ws_id, board_id = investors.ws_id, investors.board_id
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "S"})).json()
    r = await api.put(f"/api/workspaces/{ws_id}", json={"tag_ids": [col["tag_id"]]})
    assert r.status_code == 400
    await api.put(f"/api/workspaces/{ws_id}", json={"emoji": "🚀"})
    ws = next(w for w in (await api.get("/api/workspaces")).json() if w["id"] == ws_id)
    assert ws["emoji"] == "🚀"
    await api.put(f"/api/workspaces/{ws_id}", json={"name": "Renamed"})  # omitted = unchanged
    ws = next(w for w in (await api.get("/api/workspaces")).json() if w["id"] == ws_id)
    assert ws["emoji"] == "🚀"
    await api.put(f"/api/workspaces/{ws_id}", json={"emoji": None})
    ws = next(w for w in (await api.get("/api/workspaces")).json() if w["id"] == ws_id)
    assert ws["emoji"] is None


@pytest.mark.asyncio
async def test_workspace_counts_batched(api, db, investors):
    ws_id, board_id, a = investors.ws_id, investors.board_id, investors.a
    from backend.db.models import WorkspaceConversationOverride
    from datetime import datetime
    db.add(WorkspaceConversationOverride(workspace_id=ws_id, conversation_id=a, needs_triage_since=datetime.utcnow()))
    await db.commit()
    rows = {r["workspace"].id: r for r in await crud.get_workspaces_with_counts(db)}
    assert rows[ws_id]["member_count"] == 2
    assert rows[ws_id]["unallocated_count"] == 1
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "S"})).json()
    db.expire_all()
    db.add(ConversationTag(conversation_id=a, tag_id=col["tag_id"]))
    await db.commit()
    rows = {r["workspace"].id: r for r in await crud.get_workspaces_with_counts(db)}
    assert rows[ws_id]["unallocated_count"] == 0


@pytest.mark.asyncio
async def test_board_rename_conflict_is_409(api, investors):
    ws_id, board_id = investors.ws_id, investors.board_id
    await api.post(f"/api/workspaces/{ws_id}/boards", json={"name": "Second"})
    r = await api.put(f"/api/workspaces/{ws_id}/boards/{board_id}", json={"name": "second"})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_move_rejects_unknown_conversation_and_foreign_column(api, investors):
    ws_id, board_id = investors.ws_id, investors.board_id
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "S"})).json()
    r = await api.post(f"/api/workspaces/{ws_id}/move",
                       json={"board_id": board_id, "conversation_id": 99999, "to_tag_id": col["tag_id"]})
    assert r.status_code == 400
    r = await api.post(f"/api/workspaces/{ws_id}/move",
                       json={"board_id": board_id, "conversation_id": investors.a, "to_tag_id": investors.tag["id"]})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_triage_surfaces_incoming_activity_until_dismissed(api, db, investors):
    from datetime import timedelta
    ws_id, board_id, a = investors.ws_id, investors.board_id, investors.a
    ws = (await crud.get_workspaces_with_counts(db))[0]["workspace"]
    ws.board_watermark -= timedelta(days=1)  # so "new" activity predates the dismissal below
    await db.commit()
    old, new = ws.board_watermark - timedelta(minutes=1), ws.board_watermark + timedelta(minutes=1)

    await crud.mark_workspace_triage(db, a, old)  # before the board existed: ignored
    assert (await api.get(f"/api/workspaces/{ws_id}/board")).json()["unallocated"] == []
    await crud.mark_workspace_triage(db, a, new)
    board = (await api.get(f"/api/workspaces/{ws_id}/board")).json()
    assert [c["id"] for c in board["unallocated"]] == [a] and board["boards"][0]["unallocated_count"] == 1

    # dismissing (move to backlog) sticks until newer activity
    await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "conversation_id": a, "to_tag_id": None})
    db.expire_all()  # the API changed the override in its own session
    await crud.mark_workspace_triage(db, a, new)
    assert (await api.get(f"/api/workspaces/{ws_id}/board")).json()["unallocated"] == []
    await crud.mark_workspace_triage(db, a, new + timedelta(days=2))
    assert [c["id"] for c in (await api.get(f"/api/workspaces/{ws_id}/board")).json()["unallocated"]] == [a]

    # a staged card is not triage; a non-member never is
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "Doing"})).json()
    board = (await api.post(f"/api/workspaces/{ws_id}/move",
                            json={"board_id": board_id, "conversation_id": a, "to_tag_id": col["tag_id"]})).json()
    assert board["classified_conversation_ids"] == [a]
    db.expire_all()
    await crud.mark_workspace_triage(db, a, new + timedelta(days=3))
    outsider = await _group(db, -5)
    await crud.mark_workspace_triage(db, outsider, new + timedelta(days=3))
    assert (await api.get(f"/api/workspaces/{ws_id}/board")).json()["unallocated"] == []


@pytest.mark.asyncio
async def test_tag_scoped_queries(api, db, investors):
    a, b = investors.a, investors.b
    conv_a = await crud.get_conversation(db, a)
    await f.message(db, conv_a, 1, "term sheet draft")
    await f.message(db, conv_a, 2, "the TERM sheet v2")
    await f.message(db, await crud.get_conversation(db, b), 1, "unrelated")
    rows = await crud.search_messages_in_tagged_conversations(db, "invest", "term sheet")
    assert [(r.conversation_id, r.match_count, r.latest_snippet) for r in rows] == [(a, 2, "the TERM sheet v2")]
    assert [c.id for c in await crud.get_conversations_by_tag_name(db, "INVEST")] == [a, b]
    await crud.touch_synced(db, a)
    assert await crud.get_tag_sync_coverage(db, "investors") == (2, 2, [b])
    tags = await crud.get_tags_for_conversations(db, [a, 12345])
    assert [t.name for t in tags[a]] == ["Investors"] and tags[12345] == []
    counts = {t["name"]: t["conversation_count"] for t in (await api.get("/api/tags")).json()}
    assert counts["Investors"] == 2


@pytest.mark.asyncio
async def test_workspace_member_ids_filters_the_inbox(db, investors):
    await _group(db, -3, "Outsider")
    convs = await crud.list_conversations(db, member_ids=crud.workspace_member_ids(investors.ws_id))
    assert sorted(c.id for c in convs) == sorted([investors.a, investors.b])
