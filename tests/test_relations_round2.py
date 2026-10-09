"""Stage-tag lockdown in the generic tag APIs, user-orgs prefs by id."""

import pytest

from backend import user_orgs
from backend.prefs import load_prefs, save_prefs
from tests import factories as f


async def _board_with_column(api, db):
    tag = (await api.post("/api/tags", json={"name": "Members"})).json()
    conv = await f.conversation(db, "-1", title="G", kind="group")
    await api.post(f"/api/conversations/{conv.id}/tags", json={"tag_id": tag["id"]})
    ws = (await api.post("/api/workspaces", json={"name": "W", "tag_ids": [tag["id"]], "view_mode": "kanban"})).json()
    col = (await api.post(f"/api/workspaces/{ws['id']}/boards/{ws['main_board_id']}/columns", json={"name": "Done"})).json()
    await api.post(f"/api/workspaces/{ws['id']}/move",
                   json={"board_id": ws["main_board_id"], "conversation_id": conv.id, "to_tag_id": col["tag_id"]})
    return tag, ws, col, conv.id


@pytest.mark.asyncio
async def test_stage_tags_hidden_from_tag_lists(api, db):
    tag, _ws, col, cid = await _board_with_column(api, db)
    ids = {t["id"] for t in (await api.get("/api/tags")).json()}
    assert tag["id"] in ids and col["tag_id"] not in ids
    with_stage = {t["id"] for t in (await api.get("/api/tags", params={"include_stage": True})).json()}
    assert col["tag_id"] in with_stage
    conv_tag_ids = {t["id"] for t in (await api.get(f"/api/conversations/{cid}/tags")).json()}
    assert conv_tag_ids == {tag["id"]}


@pytest.mark.asyncio
async def test_generic_tag_apis_refuse_stage_tags(api, db):
    _tag, ws, col, cid = await _board_with_column(api, db)
    other = (await f.conversation(db, "-2", title="H", kind="group")).id
    tid = col["tag_id"]
    for resp in (
        await api.put(f"/api/tags/{tid}", json={"name": "Hacked"}),
        await api.delete(f"/api/tags/{tid}"),
        await api.post(f"/api/conversations/{other}/tags", json={"tag_id": tid}),
        await api.delete(f"/api/conversations/{cid}/tags/{tid}"),
    ):
        assert resp.status_code == 409, resp.text
        assert resp.json()["code"] == "stage_tag_locked"
    board = (await api.get(f"/api/workspaces/{ws['id']}/board")).json()
    assert board["columns"][0]["name"] == "Done"
    assert [c["id"] for c in board["columns"][0]["conversations"]] == [cid]


@pytest.mark.asyncio
async def test_user_org_names_migrate_to_ids_and_follow_rename(api, db):
    org = (await api.post("/api/organizations", json={"name": "Zircuit"})).json()
    save_prefs({"user_organizations": ["zircuit", "Not Yet An Org"]})
    resolved = await user_orgs.resolve_user_orgs(db)
    assert resolved == [{"id": org["id"], "name": "Zircuit"}, {"id": None, "name": "Not Yet An Org"}]
    assert load_prefs()["user_organizations"] == ["Zircuit", "Not Yet An Org"]
    assert load_prefs()["user_organization_ids"] == [org["id"]]

    await api.put(f"/api/organizations/{org['id']}", json={"name": "Zircuit Labs"})
    prefs = load_prefs()
    assert prefs["user_organization_ids"] == [org["id"]]
    assert prefs["user_organizations"] == ["Zircuit Labs", "Not Yet An Org"]


@pytest.mark.asyncio
async def test_legacy_name_only_pref_follows_rename_without_resolve(api):
    org = (await api.post("/api/organizations", json={"name": "Acme"})).json()
    save_prefs({"user_organizations": ["acme"]})
    await api.put(f"/api/organizations/{org['id']}", json={"name": "Acme Inc"})
    prefs = load_prefs()
    assert prefs["user_organization_ids"] == [org["id"]]
    assert prefs["user_organizations"] == ["Acme Inc"]


@pytest.mark.asyncio
async def test_user_org_follows_merge_and_delete(api, db):
    a = (await api.post("/api/organizations", json={"name": "Alpha"})).json()
    b = (await api.post("/api/organizations", json={"name": "Beta"})).json()
    await user_orgs.set_user_orgs(db, [a["id"]])
    r = await api.put(f"/api/organizations/{a['id']}", json={"name": "beta", "merge": True})
    assert r.status_code == 200
    prefs = load_prefs()
    assert prefs["user_organization_ids"] == [b["id"]] and prefs["user_organizations"] == ["Beta"]

    await api.delete(f"/api/organizations/{b['id']}")
    prefs = load_prefs()
    assert prefs["user_organization_ids"] == [] and prefs["user_organizations"] == []


@pytest.mark.asyncio
async def test_unrelated_org_changes_leave_prefs_alone(api):
    org = (await api.post("/api/organizations", json={"name": "Other"})).json()
    save_prefs({"user_organizations": ["Mine"], "notif_pref": "all"})
    await api.put(f"/api/organizations/{org['id']}", json={"name": "Other2"})
    await api.delete(f"/api/organizations/{org['id']}")
    assert load_prefs() == {"user_organizations": ["Mine"], "notif_pref": "all"}


@pytest.mark.asyncio
async def test_user_orgs_keep_input_order(api, db):
    acme = (await api.post("/api/organizations", json={"name": "Acme"})).json()
    beta = (await api.post("/api/organizations", json={"name": "Beta Labs"})).json()
    expected = [{"id": None, "name": "Unknown Co"}, {"id": beta["id"], "name": "Beta Labs"},
                {"id": acme["id"], "name": "Acme"}]
    assert await user_orgs.set_user_orgs(db, ["Unknown Co", beta["id"], "acme ", "ACME", 999]) == expected
    assert await user_orgs.resolve_user_orgs(db) == expected
    assert load_prefs()["user_organization_ids"] == [beta["id"], acme["id"]]

    # legacy name-only prefs migrate in place
    save_prefs({"user_organizations": ["x", "beta labs", "acme"]})
    assert [e["id"] for e in await user_orgs.resolve_user_orgs(db)] == [None, beta["id"], acme["id"]]

    # rename keeps the slot; an id without a name entry is appended
    await api.put(f"/api/organizations/{beta['id']}", json={"name": "Beta"})
    assert [e["name"] for e in await user_orgs.resolve_user_orgs(db)] == ["x", "Beta", "Acme"]
    save_prefs({"user_organization_ids": [acme["id"], beta["id"]], "user_organizations": ["Beta"]})
    assert [e["name"] for e in await user_orgs.resolve_user_orgs(db)] == ["Beta", "Acme"]


@pytest.mark.asyncio
async def test_conversation_tag_routes_404_and_bulk_assign(api, db):
    from backend.db import crud
    tag = (await api.post("/api/tags", json={"name": "Bulk"})).json()
    assert (await api.post("/api/conversations/999/tags", json={"tag_id": tag["id"]})).status_code == 404
    assert (await api.get("/api/conversations/999/tags")).status_code == 404
    a = (await f.conversation(db, "-7", title="A", kind="group")).id
    b = (await f.conversation(db, "-8", title="B", kind="group")).id
    assert (await api.post(f"/api/conversations/{a}/tags", json={"tag_id": 999})).status_code == 404
    assert await crud.add_tag_to_conversations(db, [a, b, a], tag["id"]) == 2
    assert await crud.add_tag_to_conversations(db, [a, b], tag["id"]) == 0
    assert (await api.delete(f"/api/conversations/{a}/tags/{tag['id']}")).json() == {"success": True}
    assert (await api.delete(f"/api/conversations/{a}/tags/{tag['id']}")).status_code == 404


@pytest.mark.asyncio
async def test_profile_degrades_to_cached_copy_when_offline(api, db):
    from unittest.mock import patch

    import backend.api.telegram as telegram

    telegram.clear_profile_cache()
    nine = await f.identity(db, "9", display_name="Nine")
    ten = await f.identity(db, "10", display_name="Ten")

    async def online(identity_id, user_id):
        return {"identity_id": identity_id, "type": "user", "first_name": "Nine"}

    async def offline(identity_id, user_id):
        raise ConnectionError("offline")

    with patch.object(telegram, "_fetch_profile", online):
        r = await api.get(f"/api/telegram/identities/{nine.id}/profile")
    assert r.json()["stale"] is False
    with patch.object(telegram, "_fetch_profile", offline):
        r = await api.get(f"/api/telegram/identities/{nine.id}/profile", params={"refresh": True})
        assert r.status_code == 200 and r.json()["stale"] is True and r.json()["first_name"] == "Nine"
        assert (await api.get(f"/api/telegram/identities/{ten.id}/profile")).status_code == 503
    telegram.clear_profile_cache()


@pytest.mark.asyncio
async def test_profile_photo_uses_shared_atomic_helper(api, db):
    from unittest.mock import patch

    from telethon.tl.types import User

    import backend.api.telegram as telegram
    import backend.telegram.media as media

    telegram.clear_profile_cache()
    ident = await f.identity(db, "42", display_name="A")
    calls = []

    async def fake_helper(client, entity, identity_id):
        calls.append((entity.id, identity_id))
        return None

    class FakeClient:
        async def get_entity(self, _id):
            return User(id=42, is_self=False, bot=False, first_name="A")

        async def __call__(self, _req):
            raise RuntimeError("no full info in test")

    async def connected(require=True):
        return FakeClient()

    with patch.object(media, "download_avatar", fake_helper), patch.object(telegram.runtime, "client", connected):
        r = await api.get(f"/api/telegram/identities/{ident.id}/profile")
    assert r.status_code == 200, r.text
    assert calls == [(42, ident.id)] and r.json()["avatar_url"] is None
    assert r.json()["person_id"] == ident.person_id
    telegram.clear_profile_cache()
