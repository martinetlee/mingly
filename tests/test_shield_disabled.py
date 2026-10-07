"""Shield (scam detection) is disabled by default: nothing is exposed,
nothing scans, nothing starts at launch."""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_features_report_shield_off(api):
    r = await api.get("/api/features")
    assert r.status_code == 200
    assert r.json() == {"shield": False}


@pytest.mark.asyncio
async def test_shield_endpoints_absent(api):
    for path in ("/api/scam/flags", "/api/scam/scanned-messages",
                 "/api/settings/prefs/scam-detection", "/api/settings/prefs/shield-cloud"):
        assert (await api.get(path)).status_code == 404, path
    r = await api.post("/api/scam/check-messages", json={"chat_id": 1, "message_ids": [1]})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_queue_does_nothing(db):
    import backend.scam.queue as q

    with patch.object(q._scan_queue, "enqueue") as enqueue:
        await q.enqueue_targets(-1001, [1, 2], source="realtime")
        await q.enqueue_targets(-1001, [3], source="manual")
    enqueue.assert_not_called()
    assert await q.scan_unscanned_messages() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_startup_starts_shield_only_when_enabled(enabled, monkeypatch, db_engine):
    """Drive the real startup hook (side effects stubbed) both ways."""
    import backend.config as config
    import backend.main as main

    monkeypatch.setattr(config, "SHIELD_ENABLED", enabled)
    started = []

    async def noop():
        return None

    monkeypatch.setattr(main, "init_db", noop)
    monkeypatch.setattr(main, "start_briefing_scheduler", lambda: None)
    monkeypatch.setattr(main, "start_catchup_summary_scheduler", lambda: None)
    monkeypatch.setattr(main, "_start_shield", lambda: started.append(1))
    monkeypatch.setattr(main, "spawn", lambda coro, **kw: coro.close())
    monkeypatch.setattr("backend.secrets.vault_status", lambda: "locked")

    await main.on_startup()
    assert started == ([1] if enabled else [])


@pytest.mark.asyncio
@pytest.mark.shield
async def test_switch_on_exposes_shield(api):
    assert (await api.get("/api/features")).json() == {"shield": True}
    assert (await api.get("/api/scam/flags")).status_code == 200
