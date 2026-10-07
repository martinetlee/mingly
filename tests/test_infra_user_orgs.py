"""GET/PUT /api/settings/prefs/user-organizations go through backend/user_orgs."""

import json

import pytest

from backend import prefs as prefs_mod
from backend.db.models import Organization


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(prefs_mod, "PREFS_FILE", tmp_path / "prefs.json")
    prefs_mod.invalidate_cache()
    yield tmp_path
    prefs_mod.invalidate_cache()


@pytest.mark.asyncio
async def test_put_resolves_names_and_ids(api, db, isolated_prefs):
    acme, beta = Organization(name="Acme"), Organization(name="Beta Labs")
    db.add_all([acme, beta])
    await db.commit()

    r = await api.put("/api/settings/prefs/user-organizations",
                      json={"organizations": ["acme ", beta.id, "Unknown Co"]})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    # The user's order is preserved (Settings adopts this echo verbatim).
    expected_entries = [
        {"id": acme.id, "name": "Acme"},
        {"id": beta.id, "name": "Beta Labs"},
        {"id": None, "name": "Unknown Co"},
    ]
    assert body["organizations"] == ["Acme", "Beta Labs", "Unknown Co"]
    assert body["entries"] == expected_entries

    r = await api.get("/api/settings/prefs/user-organizations")
    assert r.json() == {"organizations": ["Acme", "Beta Labs", "Unknown Co"], "entries": expected_entries}

    stored = json.loads((isolated_prefs / "prefs.json").read_text())
    assert stored["user_organization_ids"] == [acme.id, beta.id]
    assert stored["user_organizations"] == ["Acme", "Beta Labs", "Unknown Co"]


@pytest.mark.asyncio
async def test_reordering_is_kept(api, db):
    a, b = Organization(name="Alpha"), Organization(name="Bravo")
    db.add_all([a, b])
    await db.commit()
    r = await api.put("/api/settings/prefs/user-organizations", json={"organizations": ["Solo", b.id, a.id]})
    assert r.json()["organizations"] == ["Solo", "Bravo", "Alpha"]
    r = await api.get("/api/settings/prefs/user-organizations")
    assert r.json()["organizations"] == ["Solo", "Bravo", "Alpha"]


@pytest.mark.asyncio
async def test_get_migrates_legacy_names(api, db, isolated_prefs):
    org = Organization(name="Zircuit")
    db.add(org)
    await db.commit()
    prefs_mod.save_prefs({"user_organizations": ["zircuit", "Solo"]})

    r = await api.get("/api/settings/prefs/user-organizations")
    assert r.status_code == 200
    assert r.json() == {
        "organizations": ["Zircuit", "Solo"],
        "entries": [{"id": org.id, "name": "Zircuit"}, {"id": None, "name": "Solo"}],
    }
    assert prefs_mod.get_pref("user_organization_ids") == [org.id]


@pytest.mark.asyncio
async def test_user_org_keys_not_in_public_prefs(api, isolated_prefs):
    prefs_mod.save_prefs({"user_organization_ids": [1], "notif_pref": "all"})
    assert (await api.get("/api/settings/prefs")).json() == {"notif_pref": "all"}
