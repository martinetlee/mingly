"""Shield analytics counters, cloud sync (URL/manifest/bundle safety,
single-flight, change tracking, Telegram ids for the current cloud contract)
and the ML ensemble's per-model thresholds."""

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import select

from backend.db import database
from backend.db.models import Message, ScamAnalytics, ScamFlag
from backend.scam import cloud_sync
from backend.scam.analytics import AnalyticsTracker
from tests import factories as f


@pytest.fixture(autouse=True)
def _v1_server(monkeypatch):
    """These tests cover cloud contract v1 (test_scam_cloud_v2 covers v2):
    the server never advertises v2, and nothing asks a real host."""
    async def v1(self):
        return 1

    monkeypatch.setattr(cloud_sync.CloudSyncManager, "contract_version", v1)


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_increments_are_not_lost(db_engine):
    tracker = AnalyticsTracker()
    await asyncio.gather(*(tracker.increment(total_scanned=1, total_pipeline_ms=10) for _ in range(40)))
    await asyncio.gather(*(tracker.record_tool_call("search_scam_scenarios") for _ in range(15)))
    async with database.AsyncSessionLocal() as s:
        rows = (await s.execute(select(ScamAnalytics))).scalars().all()
    assert len(rows) == 1
    assert rows[0].total_scanned == 40 and rows[0].total_pipeline_ms == 400
    assert json.loads(rows[0].tool_calls_json) == {"search_scam_scenarios": 15}


@pytest.mark.asyncio
async def test_only_closed_days_are_uploaded(db_engine):
    tracker = AnalyticsTracker()
    await tracker.increment(total_scanned=3)
    yesterday = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    async with database.AsyncSessionLocal() as s:
        s.add(ScamAnalytics(period_start=yesterday, total_scanned=5, scans_direct=2, scans_group=3))
        await s.commit()
    periods = await tracker.get_pending_periods()
    assert [p["detection"]["total_scanned"] for p in periods] == [5]
    # current cloud contract: "chat_types" with private = direct
    assert periods[0]["chat_types"] == {"private": 2, "group": 3, "channel": 0}
    await tracker.mark_synced([p["period_start"] for p in periods])
    assert await tracker.get_pending_periods() == []


# ---------------------------------------------------------------------------
# Cloud sync safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("https://shield.example.com", True), ("http://localhost:8000", True), ("http://127.0.0.1:9", True),
    ("http://shield.example.com", False), ("ftp://x", False), ("https://", False),
])
def test_cloud_url_must_be_https(url, ok):
    assert (cloud_sync.validate_cloud_url(url) is None) is ok


@pytest.mark.asyncio
async def test_http_url_refuses_to_sync():
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: "k")
    mgr.configure("http://evil.example.com")
    res = await mgr.sync()
    assert res.status == "failed" and "https" in res.error


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud_sync, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cloud_sync, "update_prefs", lambda fn: None)
    return tmp_path


def _manager(monkeypatch, files: dict):
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: "k")
    mgr.configure("https://cloud.test", "k")

    async def fake_bytes(path, timeout=30.0):
        return files[path]

    monkeypatch.setattr(mgr, "_download_bytes", fake_bytes)
    return mgr


@pytest.mark.asyncio
async def test_manifest_names_are_allowlisted_and_contained(data_dir, monkeypatch):
    payload = json.dumps({"version": "2"}).encode()
    mgr = _manager(monkeypatch, {"/f": payload})
    manifest = {"files": [
        {"name": "../escape.json", "endpoint": "/f", "version": "2"},
        {"name": "prefs.json", "endpoint": "/f", "version": "2"},
        {"name": "scenarios.json", "endpoint": "https://evil/x", "version": "2"},
        {"name": "scenarios.json", "endpoint": "/f", "version": "2", "sha256": "00"},
        {"name": "ml_models/../../x.zip", "endpoint": "/f", "version": "2", "type": "model_bundle"},
        {"name": "safe_patterns.json", "endpoint": "/f", "version": "2", "sha256": hashlib.sha256(payload).hexdigest()},
    ]}
    errors = []
    monkeypatch.setattr(mgr, "_after_download", _noop)
    updated = await mgr._download_files(manifest, errors)
    assert updated == ["safe_patterns.json"]
    assert len(errors) == 5
    assert not (data_dir.parent / "escape.json").exists() and not (data_dir / "prefs.json").exists()
    assert json.loads((data_dir / "safe_patterns.json").read_text())["version"] == "2"


async def _noop(*a, **k):
    return None


def _bundle(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_bundle_hash_mismatch_is_never_installed(data_dir, monkeypatch):
    good = _bundle({"lgbm.joblib": b"x", "thresholds.json": json.dumps({"low_threshold": 0.1, "high_threshold": 0.9})})
    live = data_dir / "ml_models" / "sms"
    live.mkdir(parents=True)
    (live / "lgbm.joblib").write_bytes(b"old")
    mgr = _manager(monkeypatch, {"/b": good})
    monkeypatch.setattr(mgr, "_after_download", _noop)

    errors = []
    await mgr._download_files({"files": [{"name": "ml_models/sms.zip", "endpoint": "/b", "version": "2",
                                          "type": "model_bundle", "sha256": "deadbeef"}]}, errors)
    assert errors and (live / "lgbm.joblib").read_bytes() == b"old"

    errors = []
    await mgr._download_files({"files": [{"name": "ml_models/sms.zip", "endpoint": "/b", "version": "2",
                                          "type": "model_bundle", "sha256": hashlib.sha256(good).hexdigest()}]}, errors)
    assert not errors
    assert (live / "lgbm.joblib").read_bytes() == b"x"
    assert json.loads((live / "thresholds.json").read_text())["version"] == "2"
    assert not [p for p in (data_dir / "ml_models").iterdir() if p.name.startswith(".")]  # temp dirs cleaned


@pytest.mark.asyncio
async def test_bundle_with_path_traversal_member_is_rejected(data_dir, monkeypatch):
    evil = _bundle({"../../pwn.txt": b"x", "lgbm.joblib": b"x"})
    mgr = _manager(monkeypatch, {"/b": evil})
    errors = []
    await mgr._download_files({"files": [{"name": "ml_models/sms.zip", "endpoint": "/b", "version": "2",
                                          "type": "model_bundle"}]}, errors)
    assert errors and not (data_dir.parent / "pwn.txt").exists()
    assert not (data_dir / "ml_models" / "sms").exists()


@pytest.mark.asyncio
async def test_sync_is_single_flight(data_dir, monkeypatch):
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: "k")
    mgr.configure("https://cloud.test", "k")
    runs = []

    async def slow_impl():
        runs.append(1)
        await asyncio.sleep(0.05)
        return cloud_sync.SyncResult(status="success")

    monkeypatch.setattr(mgr, "_sync_impl", slow_impl)
    results = await asyncio.gather(mgr.sync(), mgr.sync(), mgr.sync())
    assert len(runs) == 1 and all(r.status == "success" for r in results)


@pytest.mark.asyncio
async def test_scan_upload_change_tracking(db_engine, monkeypatch):
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: "k")
    mgr.configure("https://cloud.test", "k")
    posted = []
    t0 = datetime.utcnow() - timedelta(minutes=5)

    async def fake_post(path, payload):
        posted.append(payload)
        # A re-score lands while the POST is in flight
        if len(posted) == 1:
            async with database.AsyncSessionLocal() as s:
                m = (await s.execute(select(Message).where(Message.seq == 2))).scalars().one()
                m.scam_score, m.scam_updated_at = 0.95, datetime.utcnow()
                await s.commit()

    monkeypatch.setattr(mgr, "_post", fake_post)
    async with database.AsyncSessionLocal() as s:
        conv = await f.conversation(s, "-1001", title="G", kind="group")
        sender = await f.identity(s, 77)
        for seq, status, score, manual in [(1, "completed", 0.9, None), (2, "completed", 0.8, None),
                                           (3, "completed", 0.1, None), (4, "failed", None, None)]:
            await f.message(s, conv, seq, "t", sender=sender, date=t0, scam_status=status,
                            scam_score=score, scam_manual_verdict=manual, scam_updated_at=t0)
        first = (await s.execute(select(Message.id).where(Message.seq == 1))).scalar_one()

    # 1, 2 (scam) + 4 (failed); 3 is safe and never uploaded. Row 2 changed
    # mid-POST, so it is not marked synced and goes up again (new score).
    assert await mgr._upload_scan_results() == 4
    assert {r["tg_message_id"] for r in posted[0]["scan_results"]} == {1, 2, 4}
    assert {(r["chat_id"], r["sender_id"]) for r in posted[0]["scan_results"]} == {(-1001, 77)}  # Telegram ids
    assert [(r["tg_message_id"], r["scam_score"]) for r in posted[1]["scan_results"]] == [(2, 0.95)]
    assert await mgr._upload_scan_results() == 0

    # A manual correction of an uploaded row is uploaded again
    from backend.scam.verdict import set_verdict
    from backend.ws.manager import ws_manager

    async def _quiet(e):
        return None

    monkeypatch.setattr(ws_manager, "broadcast", _quiet)
    await set_verdict(scope="message", verdict="not_scam", messages=[first])
    assert await mgr._upload_scan_results() == 1
    sent = posted[-1]["scan_results"][0]
    assert sent["manual_verdict"] == "not_scam" and sent["effective_is_scam"] is False
    assert await mgr._upload_scan_results() == 0


@pytest.mark.asyncio
async def test_flag_status_changes_are_reuploaded(db_engine, monkeypatch):
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: "k")
    mgr.configure("https://cloud.test", "k")
    posted = []

    async def fake_post(path, payload):
        posted.append(payload)

    monkeypatch.setattr(mgr, "_post", fake_post)
    async with database.AsyncSessionLocal() as s:
        sender = await f.identity(s, 5)
        s.add(ScamFlag(identity_id=sender.id, source="local", flagged_at=datetime.utcnow(),
                       updated_at=datetime.utcnow()))
        await s.commit()
    assert await mgr._upload_unsent_flags() == 1
    assert posted[-1]["flags"][0]["telegram_id"] == 5
    assert await mgr._upload_unsent_flags() == 0

    from backend.scam.verdict import set_verdict
    from backend.ws.manager import ws_manager

    async def _quiet(e):
        return None

    monkeypatch.setattr(ws_manager, "broadcast", _quiet)
    await set_verdict(scope="sender", verdict="not_scam", identity_id=sender.id)
    assert await mgr._upload_unsent_flags() == 1
    assert posted[-1]["flags"][0]["status"] == "dismissed"


# ---------------------------------------------------------------------------
# ML ensemble
# ---------------------------------------------------------------------------

class _FakeModel:
    def __init__(self, domain, score, low, high, has=True):
        self.domain, self.score = domain, score
        self.low_threshold, self.high_threshold, self.has_thresholds = low, high, has

    def predict_proba(self, texts):
        return np.array([self.score])


def _ensemble(*models):
    from backend.scam.tools.ml_prefilter import MLEnsemble
    e = MLEnsemble.__new__(MLEnsemble)
    e._all_models = {m.domain: m for m in models}
    return e


def test_per_model_thresholds_then_aggregate():
    # Real trace values: sms 0.9779 vs its calibrated high 0.995 → borderline,
    # not auto_scam (the old global 0.95 cut said auto_scam).
    e = _ensemble(_FakeModel("sms", 0.9779, 0.246, 0.995), _FakeModel("phishing", 0.55, 0.82, 0.99),
                  _FakeModel("job_scams", 0.04, 0.01, 0.74))
    out = e.predict(["x"], low_threshold=0.15, high_threshold=0.95)
    assert out["verdict"] == "borderline"
    assert {m["domain"]: m["verdict"] for m in out["models"]} == {
        "sms": "borderline", "phishing": "safe", "job_scams": "borderline"}

    e = _ensemble(_FakeModel("a", 0.1, 0.3, 0.9), _FakeModel("b", 0.95, 0.3, 0.9))
    assert e.predict(["x"])["verdict"] == "auto_scam"
    e = _ensemble(_FakeModel("a", 0.1, 0.3, 0.9))
    assert e.predict(["x"])["verdict"] == "safe"
    # Model without calibration falls back to the pipeline thresholds
    e = _ensemble(_FakeModel("a", 0.5, 0, 0, has=False))
    assert e.predict(["x"], low_threshold=0.4, high_threshold=0.9)["verdict"] == "borderline"


def test_all_models_failing_is_unavailable_not_safe():
    class Broken(_FakeModel):
        def predict_proba(self, texts):
            raise RuntimeError("bad")

    e = _ensemble(Broken("a", 0, 0.3, 0.9))
    assert e.predict(["x"])["verdict"] == "unavailable"


@pytest.mark.asyncio
async def test_script_uses_target_text_and_runs_off_loop(monkeypatch):
    from backend.scam.tools import ml_prefilter
    seen = {}

    class Rec:
        def predict(self, texts, **kw):
            import threading
            seen["texts"], seen["thread"] = texts, threading.current_thread() is threading.main_thread()
            return {"verdict": "safe", "risk_score": 0.0, "models": []}

    monkeypatch.setattr(ml_prefilter, "_ensemble", Rec())
    await ml_prefilter.ml_ensemble_script({"$message": "context junk\nMessages to analyze:\n  [A]: other",
                                           "$target_text": "the target"})
    assert seen["texts"] == ["the target"] and seen["thread"] is False


@pytest.mark.asyncio
async def test_manual_verdict_on_never_scanned_row_is_uploaded(db_engine, monkeypatch):
    from backend.scam.verdict import set_verdict
    from backend.ws.manager import ws_manager

    async def _quiet(e):
        return None

    monkeypatch.setattr(ws_manager, "broadcast", _quiet)
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: "k")
    mgr.configure("https://cloud.test")
    posted = []

    async def fake_post(path, payload):
        posted.append(payload)

    monkeypatch.setattr(mgr, "_post", fake_post)
    async with database.AsyncSessionLocal() as s:
        conv = await f.conversation(s, "1", title="A")
        msg = await f.message(s, conv, 1, "t", date=datetime.utcnow())  # scam_status NULL
    await set_verdict(scope="message", verdict="scam", messages=[msg.id])
    assert await mgr._upload_scan_results() == 1
    sent = posted[0]["scan_results"][0]
    assert sent["scan_status"] == "unknown" and sent["manual_verdict"] == "scam" and sent["effective_is_scam"]


def test_api_keys_are_read_lazily_never_cached():
    key = {"v": "k1"}
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: key["v"])
    mgr.configure("https://cloud.test", "ignored-legacy-arg")
    assert mgr._headers["Authorization"] == "Bearer k1"
    key["v"] = ""  # vault locked / reset
    assert not mgr.is_configured

    from backend.scam.client import ExternalScamAPIClient
    c = ExternalScamAPIClient("https://x", "stale-key", key_provider=lambda: key["v"])
    assert c._api_key == ""


@pytest.mark.asyncio
async def test_vault_lock_stops_sync_loop_and_inflight_sync(monkeypatch):
    from backend import secrets as vault
    from backend.jobs import run_single

    monkeypatch.setattr(cloud_sync, "SYNC_INITIAL_DELAY", 3600)
    assert cloud_sync.start_sync_loop() is True
    assert cloud_sync.start_sync_loop() is False  # idempotent
    loop_task = cloud_sync._loop_task

    async def long_sync():
        await asyncio.sleep(3600)

    job, _ = run_single("cloud_sync", long_sync)
    monkeypatch.setattr(vault, "_lock_listeners", [cloud_sync.stop_sync_loop])
    vault.lock()
    await asyncio.sleep(0)
    assert loop_task.cancelled() or loop_task.done()
    await asyncio.sleep(0)
    assert job.cancelled()


def test_verify_artifact_is_the_single_gate():
    data = b"bundle"
    cloud_sync.verify_artifact(data, {}, "x")  # no hash given: allowed (https only)
    cloud_sync.verify_artifact(data, {"sha256": hashlib.sha256(data).hexdigest().upper()}, "x")
    with pytest.raises(ValueError):
        cloud_sync.verify_artifact(data, {"sha256": "00"}, "x")


# ---------------------------------------------------------------------------
# Current cloud contract: Telegram ids, other channels skipped
# ---------------------------------------------------------------------------

def _recording_manager(monkeypatch):
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: "k")
    mgr.configure("https://cloud.test", "k")
    posted = []

    async def fake_post(path, payload):
        posted.append((path, payload))

    monkeypatch.setattr(mgr, "_post", fake_post)
    return mgr, posted


@pytest.mark.asyncio
async def test_non_telegram_rows_are_not_uploaded(db, monkeypatch):
    mgr, posted = _recording_manager(monkeypatch)
    mail = await f.conversation(db, "thread-1", title="Mail", kind="email_thread", channel="gmail")
    eve = await f.identity(db, "eve@example.com", channel="gmail")
    await f.message(db, mail, 1, "wire money", sender=eve, scam_status="completed", scam_score=0.9,
                    scam_updated_at=datetime.utcnow())
    db.add(ScamFlag(identity_id=eve.id, source="local", flagged_at=datetime.utcnow()))
    await db.commit()

    assert await mgr._upload_scan_results() == 0
    assert await mgr._upload_unsent_flags() == 0
    assert posted == []
    async with database.AsyncSessionLocal() as s:
        flag = (await s.execute(select(ScamFlag))).scalars().one()
    assert flag.synced_to_cloud is False  # waits for cloud contract v2


@pytest.mark.asyncio
async def test_report_dismissal_and_feedback_payloads_carry_telegram_ids(api, db, monkeypatch):
    from backend.scam.verdict import set_verdict
    from backend.ws.manager import ws_manager
    import backend.scam.verdict as verdict_mod

    async def _quiet(e):
        return None

    monkeypatch.setattr(ws_manager, "broadcast", _quiet)
    monkeypatch.setattr(verdict_mod, "get_pref", lambda k, d=None: True if k == "shield_sync_dismissals" else d)
    mgr, posted = _recording_manager(monkeypatch)
    monkeypatch.setattr(cloud_sync, "get_cloud_sync_manager", lambda: mgr)

    conv = await f.conversation(db, "-100900", title="G", kind="group")
    scammer = await f.identity(db, 4242, display_name="Mal")
    m1 = await f.message(db, conv, 31, "send btc", sender=scammer, scam_status="completed", scam_score=0.9)
    m2 = await f.message(db, conv, 32, "now", sender=scammer)

    r = await api.post("/api/scam/reports", json={
        "reported_identity_id": scammer.id, "reported_in_conversation_id": conv.id,
        "included_message_ids": [m1.id, m2.id],
    })
    assert r.status_code == 200, r.text
    assert await mgr._upload_pending_reports() == 1
    report = posted[-1][1]["reports"][0]
    assert report["reported_telegram_id"] == 4242 and report["reported_in_chat_id"] == -100900
    assert report["chat_type"] == "group"
    assert {m["telegram_id"] for m in json.loads(report["messages_json"])} == {4242}
    assert [sc["tg_message_id"] for sc in json.loads(report["local_detection_json"])["scores"]] == [31]

    await set_verdict(scope="message", verdict="not_scam", messages=[m1.id])
    await set_verdict(scope="sender", verdict="scam", identity_id=scammer.id)
    await set_verdict(scope="sender", verdict="not_scam", identity_id=scammer.id)
    assert await mgr._upload_pending_dismissals() == 2
    by_path = {path: payload for path, payload in posted[-2:]}
    assert by_path["/sync/dismissals"]["dismissals"][0]["telegram_id"] == 4242
    assert "identity_id" not in by_path["/sync/dismissals"]["dismissals"][0]
    scan = by_path["/sync/scan-results"]["scan_results"][0]
    assert (scan["chat_id"], scan["tg_message_id"], scan["sender_id"]) == (-100900, 31, 4242)

    r = await api.post("/api/scam/feedback", json={
        "feedback_type": "false_positive", "message": "fine", "related_conversation_id": conv.id,
        "related_message_id": m1.id, "related_flag_identity_id": scammer.id,
    })
    assert r.json()["status"] == "synced"
    fb = posted[-1][1]["feedback"][0]
    assert (fb["related_chat_id"], fb["related_message_id"], fb["related_flag_telegram_id"]) == (-100900, 31, 4242)

    # Feedback about another channel's conversation stays local
    mail = await f.conversation(db, "thread-2", title="Mail", kind="email_thread", channel="gmail")
    r = await api.post("/api/scam/feedback", json={"feedback_type": "bug", "message": "x",
                                                   "related_conversation_id": mail.id})
    assert r.json()["status"] == "saved_locally"
