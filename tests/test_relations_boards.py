"""Kanban stage-tag ownership (review §2-D): a column owns a fresh stage tag,
mutations never strip membership/scope tags, deletes clean up after themselves."""

import pytest
import pytest_asyncio
from sqlalchemy import select, func

from backend.db import crud
from backend.db.models import Chat, ChatTag, Tag


async def _chat(db, tid, name="Chat"):
    db.add(Chat(telegram_id=tid, name=name, type="group"))
    await db.commit()


async def _tag_exists(db, tag_id):
    return bool((await db.execute(select(func.count()).select_from(Tag).where(Tag.id == tag_id))).scalar())


async def _tags_of(db, chat_id):
    return set((await db.execute(select(ChatTag.tag_id).where(ChatTag.chat_id == chat_id))).scalars().all())


async def _kanban(api, name, tag_ids):
    r = await api.post("/api/workspaces", json={"name": name, "tag_ids": tag_ids, "view_mode": "kanban"})
    assert r.status_code == 200, r.text
    return r.json()["id"], r.json()["main_board_id"]


@pytest_asyncio.fixture
async def investors(api, db):
    """Workspace whose include tag is "Investors", with two member chats."""
    tag = (await api.post("/api/tags", json={"name": "Investors"})).json()
    for tid in (-1001, -1002):
        await _chat(db, tid)
        assert (await api.post(f"/api/chats/{tid}/tags", json={"tag_id": tag["id"]})).status_code == 200
    ws_id, board_id = await _kanban(api, "Fundraise", [tag["id"]])
    return tag, ws_id, board_id


@pytest.mark.asyncio
async def test_column_named_like_include_tag_gets_its_own_stage_tag(api, db, investors):
    tag, ws_id, board_id = investors
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "investors"})).json()
    assert col["tag_id"] != tag["id"]
    stage = await db.get(Tag, col["tag_id"])
    assert stage.category == "stage"

    # Drag a card in and back out: membership tag must survive
    r = await api.post(f"/api/workspaces/{ws_id}/move",
                       json={"board_id": board_id, "chat_id": -1001, "to_tag_id": col["tag_id"]})
    assert r.status_code == 200
    board = r.json()  # move returns the updated board
    assert [c["telegram_id"] for c in board["columns"][0]["chats"]] == [-1001]
    r = await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "chat_id": -1001, "to_tag_id": None})
    assert r.status_code == 200
    assert tag["id"] in await _tags_of(db, -1001)

    # Deleting the column never touches the user's tag
    await api.post(f"/api/workspaces/{ws_id}/move",
                   json={"board_id": board_id, "chat_id": -1002, "to_tag_id": col["tag_id"]})
    r = await api.delete(f"/api/workspaces/{ws_id}/boards/{board_id}/columns/{col['tag_id']}")
    assert r.status_code == 200
    for tid in (-1001, -1002):
        assert await _tags_of(db, tid) == {tag["id"]}
    assert not await _tag_exists(db, col["tag_id"])  # stage tag died with its column


@pytest.mark.asyncio
async def test_same_label_in_two_workspaces_is_independent(api, db, investors):
    tag, ws_a, board_a = investors
    ws_b, board_b = await _kanban(api, "Other", [tag["id"]])
    done_a = (await api.post(f"/api/workspaces/{ws_a}/boards/{board_a}/columns", json={"name": "Done"})).json()
    done_b = (await api.post(f"/api/workspaces/{ws_b}/boards/{board_b}/columns", json={"name": "Done"})).json()
    assert done_a["tag_id"] != done_b["tag_id"]
    await api.post(f"/api/workspaces/{ws_a}/move", json={"board_id": board_a, "chat_id": -1001, "to_tag_id": done_a["tag_id"]})
    board_b_data = (await api.get(f"/api/workspaces/{ws_b}/board")).json()
    assert board_b_data["columns"][0]["chats"] == []


@pytest.mark.asyncio
async def test_delete_workspace_removes_stage_tags_so_new_column_starts_empty(api, db, investors):
    tag, ws_id, board_id = investors
    done = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "Done"})).json()
    await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "chat_id": -1001, "to_tag_id": done["tag_id"]})
    assert (await api.delete(f"/api/workspaces/{ws_id}")).status_code == 204
    assert not await _tag_exists(db, done["tag_id"])
    assert await _tags_of(db, -1001) == {tag["id"]}

    ws2, board2 = await _kanban(api, "Again", [tag["id"]])
    await api.post(f"/api/workspaces/{ws2}/boards/{board2}/columns", json={"name": "Done"})
    assert (await api.get(f"/api/workspaces/{ws2}/board")).json()["columns"][0]["chats"] == []


@pytest.mark.asyncio
async def test_delete_board_removes_its_stage_tags(api, db, investors):
    _tag, ws_id, board_id = investors
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "New"})).json()
    await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "chat_id": -1002, "to_tag_id": col["tag_id"]})
    assert (await api.delete(f"/api/workspaces/{ws_id}/boards/{board_id}")).status_code == 200
    assert not await _tag_exists(db, col["tag_id"])
    n = (await db.execute(select(func.count()).select_from(ChatTag).where(ChatTag.tag_id == col["tag_id"]))).scalar()
    assert n == 0


@pytest.mark.asyncio
async def test_move_never_strips_protected_tag_from_legacy_shared_column(api, db, investors):
    """Pre-migration state: a column backed directly by the include tag."""
    from backend.db.models import WorkspaceColumn
    tag, ws_id, board_id = investors
    db.add(WorkspaceColumn(workspace_id=ws_id, board_id=board_id, tag_id=tag["id"], label="Investors", position=0))
    other = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "Later"})).json()
    await db.commit()
    await api.post(f"/api/workspaces/{ws_id}/move", json={"board_id": board_id, "chat_id": -1001, "to_tag_id": other["tag_id"]})
    assert tag["id"] in await _tags_of(db, -1001)
    await api.delete(f"/api/workspaces/{ws_id}/boards/{board_id}/columns/{tag['id']}")
    assert await _tag_exists(db, tag["id"])
    assert tag["id"] in await _tags_of(db, -1002)


@pytest.mark.asyncio
async def test_reorder_columns_atomically(api, investors):
    _tag, ws_id, board_id = investors
    ids = [(await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": n})).json()["tag_id"]
           for n in ("A", "B", "C")]
    r = await api.put(f"/api/workspaces/{ws_id}/boards/{board_id}/columns/order", json={"tag_ids": ids[::-1]})
    assert r.status_code == 200, r.text
    assert [c["tag_id"] for c in r.json()["columns"]] == ids[::-1]
    r = await api.put(f"/api/workspaces/{ws_id}/boards/{board_id}/columns/order", json={"tag_ids": ids[:2]})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_stream_tag_never_resolves_to_stage_tag_and_frees_name(api, db, investors):
    _tag, ws_id, board_id = investors
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
    _tag, ws_id, board_id = investors
    assert (await api.post("/api/tags", json={"name": " investors "})).status_code == 409
    await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "Done"})
    r = await api.post("/api/tags", json={"name": "done"})
    assert r.status_code == 200, r.text
    assert (await api.post("/api/tags", json={"name": "x", "category": "stage"})).status_code == 400


@pytest.mark.asyncio
async def test_workspace_rejects_stage_tags_and_emoji_clears(api, investors):
    _tag, ws_id, board_id = investors
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
    _tag, ws_id, board_id = investors
    from backend.db.models import WorkspaceChatOverride
    from datetime import datetime
    db.add(WorkspaceChatOverride(workspace_id=ws_id, chat_id=-1001, needs_triage_since=datetime.utcnow()))
    await db.commit()
    rows = {r["workspace"].id: r for r in await crud.get_workspaces_with_counts(db)}
    assert rows[ws_id]["member_count"] == 2
    assert rows[ws_id]["unallocated_count"] == 1
    col = (await api.post(f"/api/workspaces/{ws_id}/boards/{board_id}/columns", json={"name": "S"})).json()
    db.expire_all()
    from backend.db.models import ChatTag as CT
    db.add(CT(chat_id=-1001, tag_id=col["tag_id"]))
    await db.commit()
    rows = {r["workspace"].id: r for r in await crud.get_workspaces_with_counts(db)}
    assert rows[ws_id]["unallocated_count"] == 0


@pytest.mark.asyncio
async def test_board_rename_conflict_is_409(api, investors):
    _tag, ws_id, board_id = investors
    await api.post(f"/api/workspaces/{ws_id}/boards", json={"name": "Second"})
    r = await api.put(f"/api/workspaces/{ws_id}/boards/{board_id}", json={"name": "second"})
    assert r.status_code == 409
