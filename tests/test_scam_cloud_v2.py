"""Shield cloud contract v2 (docs/multichannel/SHIELD_CLOUD_CONTRACT_V2.md):
capability discovery, v1 fallback byte-for-byte, channel-neutral v2 payloads
with hashed private handles, per-channel content opt-in and the install salt."""

import hashlib
import hmac
import json
from datetime import datetime

import httpx
import pytest
from sqlalchemy import select

from backend import secrets as vault
from backend.db import database
from backend.db.models import Message, ScamAnalytics, ScamDismissal, ScamFeedback, ScamFlag, ScamReport
from backend.prefs import update_prefs
from backend.scam import channel_ids, cloud_sync
from backend.scam.client import ExternalScamAPIClient
from backend.scam.scan import ScanRequest
from tests import factories as f

T = datetime(2026, 3, 1, 10, 0)

TRACE = {
    "status": "completed", "pipeline_version": "3.1.0", "duration_ms": 900, "input": "PIPELINE INPUT send btc",
    "steps": [{"node_id": "llm_judge", "type": "llm", "status": "completed", "model_name": "qwen2.5:7b",
               "provider_name": "ollama", "max_tokens": 200, "temperature": 0, "duration_ms": 800,
               "prompt_sent": "PROMPT send btc", "raw_response": "RAW scam", "input": {"$message": "send btc"}}],
}


async def _seed_channel(db, channel, *, conv_ext, kind, sender_ext, text):
    """One conversation with a flagged sender, a scam-scored message, a
    report, both dismissal kinds and feedback, all on ``channel``."""
    sender = await f.identity(db, sender_ext, channel=channel, display_name=f"{channel}-sender")
    conv = await f.conversation(db, conv_ext, title=f"{channel} conv", kind=kind, channel=channel,
                                peer=sender if kind == "direct" else None)
    msg = await f.message(db, conv, 31, text, sender=sender, date=T, scam_status="completed", scam_score=0.9,
                          scam_reason="crypto ask", scam_category="investment", scam_updated_at=T,
                          scam_trace_json=json.dumps(TRACE))
    db.add(ScamFlag(identity_id=sender.id, source="local", confidence=0.9, reason="crypto ask",
                    category="investment", flagged_message_text=text, flagged_at=T, updated_at=T))
    db.add(ScamReport(
        report_id=f"report-{channel}", reported_identity_id=sender.id, reported_in_conversation_id=conv.id,
        chat_type=kind, reporter_verdict="scam", scam_category="investment",
        messages_json=json.dumps([
            {"position": 0, "identity_id": sender.id, "role": "reported", "included": True,
             "sender_name": sender.display_name, "text": text, "date": "2026-03-01T10:00:00Z"},
            {"position": 1, "identity_id": None, "role": "reporter", "included": False},
        ]),
        local_detection_json=json.dumps({"scores": [
            {"message_id": msg.id, "scam_score": 0.9, "scam_reason": "crypto ask", "scam_category": "investment",
             "scam_status": "completed", "manual_verdict": None, "effective_is_scam": True}]}),
        status="submitted", created_at=T, updated_at=T,
    ))
    db.add(ScamDismissal(dismissal_type="scammer", created_at=T, payload_json=json.dumps({
        "identity_id": sender.id, "source": "local", "confidence": 0.9, "reason": "crypto ask",
        "category": "investment", "flagged_message_text": text, "flagged_at": "2026-03-01T10:00:00Z"})))
    db.add(ScamDismissal(dismissal_type="scan", created_at=T, payload_json=json.dumps({
        "conversation_id": conv.id, "message_id": msg.id, "identity_id": sender.id, "scam_score": 0.9,
        "scam_reason": "crypto ask", "scam_category": "investment", "llm_prompt": f"PROMPT {text}",
        "llm_response": "RAW scam", "trace": TRACE})))
    fb = ScamFeedback(feedback_type="false_positive", message=f"about {channel}", related_conversation_id=conv.id,
                      related_message_id=msg.id, related_flag_identity_id=sender.id, status="pending", created_at=T)
    db.add(fb)
    await db.commit()
    return {"conv": conv, "sender": sender, "msg": msg, "feedback_id": fb.id}


async def seed(db):
    rows = {
        "telegram": await _seed_channel(db, "telegram", conv_ext="-100777", kind="group", sender_ext="4242",
                                        text="send btc to my wallet"),
        "gmail": await _seed_channel(db, "gmail", conv_ext="thread-1", kind="email_thread",
                                     sender_ext="Eve@Example.com", text="wire the invoice today"),
        "signal": await _seed_channel(db, "signal", conv_ext="sig-conv", kind="direct",
                                      sender_ext="5f0c1d2e-aci", text="gift card please"),
        "sms": await _seed_channel(db, "sms", conv_ext="sms-conv", kind="direct", sender_ext="+15551234567",
                                   text="your parcel is held"),
    }
    db.add(ScamAnalytics(period_start=datetime(2026, 1, 1), total_scanned=7, scam_detected=2, safe_detected=5,
                         scans_direct=3, scans_group=4, total_pipeline_ms=700))
    await db.commit()
    return rows


# Captured from the pre-v2 builders (same seed): what a v1 server must keep
# receiving, byte for byte.
V1_GOLDEN = [
    ('/sync/reports',
     '{"reports":[{"report_id":"report-telegram","reported_telegram_id":4242,"reported_in_chat_id":-100777,"chat_type":"group","reporter_verdict":"scam","scam_category":"investment","custom_category":null,"messages_json":"[{\\"position\\": 0, \\"role\\": \\"reported\\", \\"included\\": true, \\"sender_name\\": \\"telegram-sender\\", \\"text\\": \\"send btc to my wallet\\", \\"date\\": \\"2026-03-01T10:00:00Z\\", \\"telegram_id\\": 4242}, {\\"position\\": 1, \\"role\\": \\"reporter\\", \\"included\\": false, \\"telegram_id\\": null}]","local_detection_json":"{\\"scores\\": [{\\"scam_score\\": 0.9, \\"scam_reason\\": \\"crypto ask\\", \\"scam_category\\": \\"investment\\", \\"scam_status\\": \\"completed\\", \\"manual_verdict\\": null, \\"effective_is_scam\\": true, \\"tg_message_id\\": 31}]}","created_at":"2026-03-01T10:00:00"}]}'),
    ('/sync/flags',
     '{"flags":[{"telegram_id":4242,"source":"local","status":"active","confidence":0.9,"reason":"crypto ask","category":"investment","flagged_message_text":"send btc to my wallet","flagged_at":"2026-03-01T10:00:00","dismissed_at":null}]}'),
    ('/sync/scan-results',
     '{"scan_results":[{"chat_id":-100777,"tg_message_id":31,"sender_id":4242,"scam_score":0.9,"scam_reason":"crypto ask","scam_category":"investment","scan_status":"completed","scan_error":null,"manual_verdict":null,"effective_is_scam":true,"date":"2026-03-01T10:00:00","updated_at":"2026-03-01T10:00:00","pipeline_version":"3.1.0","pipeline_input":"PIPELINE INPUT send btc","models_used":[{"node_id":"llm_judge","model_name":"qwen2.5:7b","provider":"ollama","max_tokens":200,"temperature":0}]}]}'),
    ('/sync/dismissals',
     '{"dismissals":[{"source":"local","confidence":0.9,"reason":"crypto ask","category":"investment","flagged_message_text":"send btc to my wallet","flagged_at":"2026-03-01T10:00:00Z","telegram_id":4242}]}'),
    ('/sync/scan-results',
     '{"scan_results":[{"chat_id":-100777,"tg_message_id":31,"sender_id":4242,"scam_score":0.9,"scam_reason":"crypto ask","scam_category":"investment","scan_status":"dismissed","dismissed":true,"pipeline_version":"3.1.0","pipeline_input":"PIPELINE INPUT send btc","models_used":[{"node_id":"llm_judge","model_name":"qwen2.5:7b","provider":"ollama","max_tokens":200,"temperature":0}],"llm_prompt":"PROMPT send btc to my wallet","llm_response":"RAW scam","trace":{"status":"completed","pipeline_version":"3.1.0","duration_ms":900,"input":"PIPELINE INPUT send btc","steps":[{"node_id":"llm_judge","type":"llm","status":"completed","model_name":"qwen2.5:7b","provider_name":"ollama","max_tokens":200,"temperature":0,"duration_ms":800,"prompt_sent":"PROMPT send btc","raw_response":"RAW scam","input":{"$message":"send btc"}}]}}]}'),
    ('/sync/feedback',
     '{"feedback":[{"id":1,"feedback_type":"false_positive","message":"about telegram","related_chat_id":-100777,"related_message_id":31,"related_flag_telegram_id":4242,"created_at":"2026-03-01T10:00:00"}]}'),
    ('/sync/analytics',
     '{"periods":[{"period_start":"2026-01-01T00:00:00","period_end":"2026-01-02T00:00:00","detection":{"total_scanned":7,"scam_detected":2,"safe_detected":5,"short_circuited_ml":0,"short_circuited_signatures":0,"pipeline_errors":0,"avg_pipeline_ms":100},"categories":{},"ml_prefilter":{"safe":0,"borderline":0,"auto_scam":0,"unavailable":0},"tool_calls":{},"scenarios_matched":{},"safe_patterns_matched":{},"user_actions":{"flags_dismissed":0,"messages_unflagged":0,"reports_submitted":0,"manual_checks":0,"rescans":0},"chat_types":{"private":3,"group":4,"channel":0},"system":{}}]}'),
]
# What fb15113 (pre-multichannel) sent for the seeds of
# test_external_v1_server_requests_unchanged, derived from its builders:
# scam/queue.py _msg_dict {message_id: tg id, sender_id, sender_name, text,
# date.isoformat()}, profiles {telegram_id, name, bio: hint or "",
# is_contact}, chat_metadata {chat_id: peer, name, type}; scam/client.py
# is_this_scam {request_id, messages (context then targets),
# target_message_ids, user_telegram_id: 0, profiles, chat_metadata}. A chat
# posting as itself (channel post) had sender_id = the chat's peer id.
EXTERNAL_V1_GOLDEN = [
    ('/isThisScam',
     '{"request_id":"req-1","messages":[{"message_id":30,"sender_id":4242,"sender_name":"Mal","text":"hi","date":"2026-03-01T09:59:00"},{"message_id":31,"sender_id":4242,"sender_name":"Mal","text":"send btc","date":"2026-03-01T10:00:00"}],"target_message_ids":[31],"user_telegram_id":0,"profiles":[{"telegram_id":4242,"name":"Mal","bio":"","is_contact":false}],"chat_metadata":{"chat_id":-1001234567890,"name":"G","type":"group"}}'),
    ('/isThisScam',
     '{"request_id":"req-2","messages":[{"message_id":6,"sender_id":-1009876543210,"sender_name":"News","text":"old promo","date":"2026-03-01T09:00:00"},{"message_id":7,"sender_id":-1009876543210,"sender_name":"News","text":"buy now","date":"2026-03-01T10:00:00"}],"target_message_ids":[7],"user_telegram_id":0,"profiles":[{"telegram_id":-1009876543210,"name":"News","bio":"","is_contact":false}],"chat_metadata":{"chat_id":-1009876543210,"name":"News","type":"channel"}}'),
    ('/isThisScam',
     '{"request_id":"req-3","messages":[{"message_id":5,"sender_id":4242,"sender_name":"Mal","text":"hello friend","date":"2026-03-01T10:00:00"}],"target_message_ids":[5],"user_telegram_id":0,"profiles":[{"telegram_id":4242,"name":"Mal","bio":"","is_contact":false}],"chat_metadata":{"chat_id":4242,"name":"Mal","type":"private"}}'),
    ('/areFlaggedScams',
     '{"telegram_ids":[4242]}'),
]
# fb15113 build_message_context for the first seed.
TELEGRAM_PROMPT_GOLDEN = (
    "Chat: G (group)\n"
    "Profile - ID:4242, Name:Mal, Bio:, Is contact:False\n"
    "\nEarlier messages from the same sender (context only, do not judge these):\n"
    "  [Mal]: hi\n"
    "\n"
    "Messages to analyze:\n"
    "  [Mal]: send btc"
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class FakeCloud:
    """httpx MockTransport server: records every request; ``versions`` is
    what GET /sync/capabilities advertises (None → 404, an int → that HTTP
    error)."""

    def __init__(self, versions=None, manifest=None):
        self.versions = versions
        self.manifest = manifest or {"files": []}
        self.requests: list = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.endswith("capabilities"):
            if self.versions is None:
                return httpx.Response(404, json={"error": "not_found"})
            if isinstance(self.versions, int):
                return httpx.Response(self.versions, json={"error": "boom"})
            return httpx.Response(200, json={"contract_versions": self.versions})
        if request.method == "GET" and path == "/sync/manifest":
            return httpx.Response(200, json=self.manifest)
        if path == "/isThisScam":
            return httpx.Response(200, json={"is_scam": True, "confidence": 0.8, "reason": "r"})
        if path == "/areFlaggedScams":
            return httpx.Response(200, json={"flagged": []})
        return httpx.Response(200, json={"received": 1})

    @property
    def posts(self) -> list:
        return [r for r in self.requests if r.method == "POST"]

    def capability_gets(self) -> int:
        return sum(1 for r in self.requests if r.method == "GET" and r.url.path.endswith("capabilities"))


def _manager(cloud: FakeCloud) -> cloud_sync.CloudSyncManager:
    mgr = cloud_sync.CloudSyncManager(key_provider=lambda: "k")
    mgr.configure("https://cloud.test")
    mgr._transport = cloud.transport
    return mgr


async def _upload_everything(mgr, rows) -> dict:
    return {
        "reports": await mgr._upload_pending_reports(),
        "flags": await mgr._upload_unsent_flags(),
        "scans": await mgr._upload_scan_results(),
        "dismissals": await mgr._upload_pending_dismissals(),
        "feedback": [await mgr.upload_feedback(rows[ch]["feedback_id"]) for ch in ("telegram", "gmail")],
        "analytics": await mgr._upload_analytics(),
    }


@pytest.fixture
def unlocked_vault(monkeypatch):
    monkeypatch.setattr(vault, "_unlock_listeners", [])
    monkeypatch.setattr(vault, "_lock_listeners", [])
    vault.initialize_vault("pw")
    yield
    vault.lock()


def _hash(salt: bytes, text: str) -> str:
    return hmac.new(salt, text.encode(), hashlib.sha256).hexdigest()


def _bodies(cloud, path) -> list:
    return [json.loads(r.content) for r in cloud.posts if r.url.path == path]


# ---------------------------------------------------------------------------
# v1 server: today's payloads, other channels stay local
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("versions", [None, [1], 500])
async def test_v1_server_gets_todays_payloads_byte_for_byte(db, versions):
    """No capabilities endpoint, an explicit [1], or an error: v1 exactly."""
    rows = await seed(db)
    cloud = FakeCloud(versions=versions)
    mgr = _manager(cloud)

    counts = await _upload_everything(mgr, rows)

    assert counts == {"reports": 1, "flags": 1, "scans": 1, "dismissals": 2, "feedback": [True, False],
                      "analytics": 1}
    assert [(r.url.path, r.content.decode()) for r in cloud.posts] == V1_GOLDEN
    assert all("x-mingly-contract" not in r.headers for r in cloud.requests)
    assert cloud.capability_gets() == 1  # asked once, then cached

    async with database.AsyncSessionLocal() as s:
        flags = {fl.identity_id: fl.synced_to_cloud for fl in (await s.execute(select(ScamFlag))).scalars()}
        reports = {r.report_id: r.synced_at for r in (await s.execute(select(ScamReport))).scalars()}
        pending_dismissals = (await s.execute(
            select(ScamDismissal).where(ScamDismissal.synced_at.is_(None)))).scalars().all()
        fb = (await s.get(ScamFeedback, rows["gmail"]["feedback_id"]))
    assert flags[rows["telegram"]["sender"].id] is True
    assert [flags[rows[ch]["sender"].id] for ch in ("gmail", "signal", "sms")] == [False, False, False]
    assert reports["report-telegram"] is not None
    assert all(reports[f"report-{ch}"] is None for ch in ("gmail", "signal", "sms"))
    assert len(pending_dismissals) == 6  # 2 per non-Telegram channel wait for v2
    assert fb.status == "pending"


# ---------------------------------------------------------------------------
# v2 server: every channel, hashed private handles, content opt-in
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v2_server_gets_channel_neutral_payloads_for_all_channels(db, unlocked_vault):
    rows = await seed(db)
    cloud = FakeCloud(versions=[1, 2])
    mgr = _manager(cloud)

    counts = await _upload_everything(mgr, rows)
    assert counts == {"reports": 4, "flags": 4, "scans": 4, "dismissals": 8, "feedback": [True, True],
                      "analytics": 1}
    assert all(r.headers.get("x-mingly-contract") == "2" for r in cloud.posts)
    assert all(json.loads(r.content)["contract"] == 2 for r in cloud.posts)

    salt = bytes.fromhex(vault.get_secret(channel_ids.HANDLE_SALT_KEY))
    expected_sender = {
        "telegram": {"id": "4242"},
        "gmail": {"handle_hash": _hash(salt, "gmail:eve@example.com")},
        "signal": {"handle_hash": _hash(salt, "signal:5f0c1d2e-aci")},
        "sms": {"handle_hash": _hash(salt, "sms:+15551234567")},
    }

    # Private handles never leave the install in clear
    everything = b"".join(r.content for r in cloud.posts)
    for raw in (b"Eve@Example.com", b"eve@example.com", b"5f0c1d2e-aci", b"+15551234567", b"thread-1", b"sig-conv"):
        assert raw not in everything

    flags = {fl["channel"]: fl for fl in _bodies(cloud, "/sync/flags")[0]["flags"]}
    assert {ch: fl["sender"] for ch, fl in flags.items()} == expected_sender
    assert flags["telegram"]["flagged_message_text"] == "send btc to my wallet"
    assert (flags["gmail"]["flagged_message_text"], flags["gmail"]["content_included"]) == (None, False)
    assert flags["sms"]["flagged_message_text"] == "your parcel is held" and flags["sms"]["content_included"]

    scans, dismissed = _bodies(cloud, "/sync/scan-results")
    scans = {sc["channel"]: sc for sc in scans["scan_results"]}
    assert scans["telegram"]["conversation"] == {"kind": "group", "id": "-100777"}
    assert scans["telegram"]["message"] == {"id": "31"}
    assert scans["telegram"]["pipeline_input"] == "PIPELINE INPUT send btc"
    gmail_scan = scans["gmail"]
    assert gmail_scan["conversation"] == {
        "kind": "email_thread", "ref": _hash(salt, f"ref/conversation/{rows['gmail']['conv'].id}")}
    assert gmail_scan["message"] == {"ref": _hash(salt, f"ref/message/{rows['gmail']['msg'].id}")}
    assert gmail_scan["sender"] == expected_sender["gmail"]
    assert gmail_scan["pipeline_input"] is None and gmail_scan["content_included"] is False
    assert gmail_scan["models_used"][0]["model_name"] == "qwen2.5:7b"  # verdict metadata still goes
    assert (gmail_scan["scam_score"], gmail_scan["scam_category"]) == (0.9, "investment")

    dismissed = {d["channel"]: d for d in dismissed["scan_results"]}
    assert dismissed["telegram"]["llm_prompt"] == "PROMPT send btc to my wallet"
    assert dismissed["telegram"]["trace"]["steps"][0]["prompt_sent"] == "PROMPT send btc"
    g = dismissed["gmail"]
    assert (g["llm_prompt"], g["llm_response"], g["pipeline_input"]) == (None, None, None)
    assert g["trace"] == {"status": "completed", "pipeline_version": "3.1.0", "duration_ms": 900, "steps": [
        {"node_id": "llm_judge", "type": "llm", "status": "completed", "duration_ms": 800, "model_name": "qwen2.5:7b",
         "provider_name": "ollama", "max_tokens": 200, "temperature": 0}]}

    sender_dismissals = {d["channel"]: d for d in _bodies(cloud, "/sync/dismissals")[0]["dismissals"]}
    assert {ch: d["sender"] for ch, d in sender_dismissals.items()} == expected_sender
    assert sender_dismissals["gmail"]["flagged_message_text"] is None

    reports = {r["channel"]: r for r in _bodies(cloud, "/sync/reports")[0]["reports"]}
    assert set(reports) == set(expected_sender)
    gr = reports["gmail"]
    assert gr["reported"] == expected_sender["gmail"]
    assert gr["conversation"]["kind"] == "email_thread"
    # A report is an explicit submission: its included messages go as written
    assert gr["messages"][0] == {"position": 0, "role": "reported", "included": True, "sender_name": "gmail-sender",
                                 "text": "wire the invoice today", "date": "2026-03-01T10:00:00Z",
                                 "sender": expected_sender["gmail"]}
    assert gr["local_detection"]["scores"][0]["message"] == gmail_scan["message"]
    assert "wire the invoice today" not in json.dumps(
        [b for r in cloud.posts if r.url.path != "/sync/reports" for b in [json.loads(r.content)]])

    feedback = [b["feedback"][0] for b in _bodies(cloud, "/sync/feedback")]
    assert [(fb["channel"], fb["related_sender"]) for fb in feedback] == [
        ("telegram", {"id": "4242"}), ("gmail", expected_sender["gmail"])]
    assert feedback[1]["related_message"] == gmail_scan["message"]

    period = _bodies(cloud, "/sync/analytics")[0]["periods"][0]
    assert period["chat_types"] == {"direct": 3, "group": 4, "channel": 0}

    # Marked synced exactly as in v1: a second pass sends nothing
    cloud.requests.clear()
    assert await mgr._upload_pending_reports() == 0
    assert await mgr._upload_unsent_flags() == 0
    assert await mgr._upload_scan_results() == 0
    assert await mgr._upload_pending_dismissals() == 0
    assert cloud.posts == []
    async with database.AsyncSessionLocal() as s:
        assert all(fl.synced_to_cloud for fl in (await s.execute(select(ScamFlag))).scalars())
        assert all(m.scam_synced_to_cloud for m in (await s.execute(
            select(Message).where(Message.scam_status.isnot(None)))).scalars())


@pytest.mark.asyncio
async def test_v2_content_upload_follows_per_channel_prefs(db, unlocked_vault):
    rows = await seed(db)
    update_prefs(lambda p: p.update({cloud_sync.CONTENT_UPLOAD_PREF: {"gmail": True, "telegram": False}}))
    cloud = FakeCloud(versions=[2])
    mgr = _manager(cloud)

    assert await mgr._upload_unsent_flags() == 4
    flags = {fl["channel"]: fl for fl in _bodies(cloud, "/sync/flags")[0]["flags"]}
    assert flags["gmail"]["flagged_message_text"] == "wire the invoice today"
    assert flags["telegram"]["flagged_message_text"] is None and not flags["telegram"]["content_included"]
    assert flags["signal"]["flagged_message_text"] == "gift card please"  # default for chats: on

    assert await mgr._upload_scan_results() == 4
    scans = {sc["channel"]: sc for sc in _bodies(cloud, "/sync/scan-results")[0]["scan_results"]}
    assert scans["gmail"]["pipeline_input"] == "PIPELINE INPUT send btc"
    assert scans["telegram"]["pipeline_input"] is None
    assert rows  # seeded


def test_content_defaults():
    assert [cloud_sync.content_upload_allowed(ch) for ch in ("telegram", "signal", "sms", "gmail", "other")] == [
        True, True, True, False, False]


# ---------------------------------------------------------------------------
# Capability discovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_capability_cache_ttl():
    now = {"t": 1000.0}
    cloud = FakeCloud(versions=[1, 2])
    disc = cloud_sync.ContractDiscovery("/sync/capabilities", clock=lambda: now["t"])
    args = ("https://cloud.test", {"Authorization": "Bearer k"}, cloud.transport)

    assert await disc.contract(*args) == 2
    assert await disc.contract(*args) == 2
    assert cloud.capability_gets() == 1
    now["t"] += cloud_sync.CAPABILITIES_TTL - 1
    cloud.versions = [1]  # server downgraded; not seen until the TTL runs out
    assert await disc.contract(*args) == 2 and cloud.capability_gets() == 1
    now["t"] += 2
    assert await disc.contract(*args) == 1 and cloud.capability_gets() == 2

    # An error means v1, asked again after the short retry TTL
    cloud.versions = 503
    now["t"] += cloud_sync.CAPABILITIES_TTL + 1
    assert await disc.contract(*args) == 1 and cloud.capability_gets() == 3
    cloud.versions = [1, 2]
    now["t"] += cloud_sync.CAPABILITIES_RETRY_TTL - 1
    assert await disc.contract(*args) == 1 and cloud.capability_gets() == 3
    now["t"] += 2
    assert await disc.contract(*args) == 2 and cloud.capability_gets() == 4


@pytest.mark.parametrize("value,expected", [
    ([1, 2], {1, 2}), ([2], {2}), ([], {1}), (None, {1}), ("2", {1}), ([True, "2", 2.0], {1}), ([3, 2], {2, 3}),
])
def test_contract_versions_parsing(value, expected):
    assert cloud_sync.parse_contract_versions(value) == frozenset(expected)


@pytest.mark.asyncio
async def test_new_cloud_url_forgets_the_cached_contract():
    cloud = FakeCloud(versions=[1, 2])
    mgr = _manager(cloud)
    assert await mgr.contract_version() == 2
    mgr.configure("https://cloud.test")  # same URL: cache kept
    assert await mgr.contract_version() == 2 and cloud.capability_gets() == 1
    cloud.versions = None
    mgr.configure("https://other.test")
    assert mgr.cached_contract_version() == 1
    assert await mgr.contract_version() == 1 and cloud.capability_gets() == 2


@pytest.mark.asyncio
async def test_manifest_contract_versions_select_v2_without_a_capabilities_call(db, unlocked_vault, monkeypatch):
    rows = await seed(db)
    monkeypatch.setattr(cloud_sync, "update_prefs", lambda fn: None)
    cloud = FakeCloud(versions=None, manifest={"files": [], "contract_versions": [1, 2]})
    mgr = _manager(cloud)

    result = await mgr.sync()
    assert result.status == "success", result.error
    assert result.flags_submitted == 4 and result.scan_results_submitted == 4 and result.reports_submitted == 4
    assert cloud.capability_gets() == 0
    assert {r.headers.get("x-mingly-contract") for r in cloud.posts} == {"2"}
    assert rows


# ---------------------------------------------------------------------------
# Install salt
# ---------------------------------------------------------------------------

def test_salt_is_created_once_in_the_vault_and_survives_relock(unlocked_vault, monkeypatch):
    assert vault.get_secret(channel_ids.HANDLE_SALT_KEY) is None
    salt = channel_ids.handle_salt()
    assert len(salt) == 32
    assert channel_ids.handle_salt() == salt
    h = channel_ids.handle_hash(salt, "gmail", " Eve@Example.com ")
    assert h == channel_ids.handle_hash(salt, "gmail", "eve@example.com") == _hash(salt, "gmail:eve@example.com")

    vault.lock()
    with pytest.raises(RuntimeError):
        channel_ids.handle_salt()  # locked: no salt, and no new one is made
    assert vault.unlock("pw")
    assert channel_ids.handle_salt() == salt


@pytest.mark.asyncio
async def test_hashes_are_stable_across_uploads(db, unlocked_vault):
    rows = await seed(db)
    cloud = FakeCloud(versions=[2])
    mgr = _manager(cloud)
    assert await mgr._upload_unsent_flags() == 4
    first = {fl["channel"]: fl["sender"] for fl in _bodies(cloud, "/sync/flags")[0]["flags"]}

    async with database.AsyncSessionLocal() as s:  # the gmail flag changes and goes up again
        flag = (await s.execute(select(ScamFlag).where(
            ScamFlag.identity_id == rows["gmail"]["sender"].id))).scalars().one()
        flag.synced_to_cloud, flag.updated_at = False, datetime.utcnow()
        await s.commit()
    assert await mgr._upload_unsent_flags() == 1
    assert _bodies(cloud, "/sync/flags")[1]["flags"][0]["sender"] == first["gmail"]


# ---------------------------------------------------------------------------
# External detection API (isThisScam)
# ---------------------------------------------------------------------------

def _scan_request(conversation_id=5, channel="telegram", kind="direct", identity_id=3):
    return ScanRequest(
        conversation_id=conversation_id, channel=channel,
        targets=[{"message_id": 11, "identity_id": identity_id, "telegram_user_id": 4242, "sender_name": "Mal",
                  "text": "send btc", "date": "2026-03-01T10:00:00Z"}],
        context=[{"message_id": 10, "identity_id": identity_id, "telegram_user_id": 4242, "sender_name": "Mal",
                  "text": "hi", "date": "2026-03-01T09:59:00Z"}],
        profiles=[{"telegram_user_id": 4242, "identity_id": identity_id, "name": "Mal", "bio": "trader",
                   "is_contact": False}],
        metadata={"conversation_id": conversation_id, "title": "G", "kind": kind, "channel": channel},
    )


def _external(cloud):
    c = ExternalScamAPIClient("https://detect.test", key_provider=lambda: "k")
    c._transport = cloud.transport
    return c


async def _seed_telegram_scans(db) -> list:
    """Three Telegram scans: a group message with context from the same
    user, a broadcast channel post (the chat is the sender) and a DM."""
    mal = await f.identity(db, "4242", display_name="Mal")
    group = await f.conversation(db, "-1001234567890", title="G", kind="group")
    await f.message(db, group, 30, "hi", sender=mal, date=datetime(2026, 3, 1, 9, 59))
    in_group = await f.message(db, group, 31, "send btc", sender=mal, date=T)
    news = await f.conversation(db, "-1009876543210", title="News", kind="channel")
    old = await f.message(db, news, 6, "old promo", date=datetime(2026, 3, 1, 9, 0))
    post = await f.message(db, news, 7, "buy now", date=T)
    old.sender_name = post.sender_name = "News"  # stored like Telethon's channel post: no user sender
    await db.commit()
    dm = await f.conversation(db, "4242", title="Mal", peer=mal)
    in_dm = await f.message(db, dm, 5, "hello friend", sender=mal, date=T)
    return [(group, in_group), (news, post), (dm, in_dm)]


@pytest.mark.asyncio
async def test_external_v1_server_requests_unchanged(db):
    """Telegram scans reach a v1 detection server exactly as before the
    multi-channel refactor (Telegram message / sender / chat ids)."""
    from backend.scam.requests import build_scan_request

    scans = await _seed_telegram_scans(db)
    cloud = FakeCloud(versions=None)
    c = _external(cloud)
    for n, (conv, msg) in enumerate(scans, 1):
        await c.is_this_scam(f"req-{n}", await build_scan_request(conv.id, [msg.id]))
    await c.are_flagged_scams([4242])
    assert [(r.url.path, r.content.decode()) for r in cloud.posts] == EXTERNAL_V1_GOLDEN
    assert all("x-mingly-contract" not in r.headers for r in cloud.requests)


@pytest.mark.asyncio
async def test_telegram_prompt_keeps_the_pre_multichannel_header(db):
    from backend.scam.scan import build_message_context
    from backend.scam.requests import build_scan_request

    (group, msg), _, _ = await _seed_telegram_scans(db)
    assert build_message_context(await build_scan_request(group.id, [msg.id])) == TELEGRAM_PROMPT_GOLDEN


@pytest.mark.asyncio
async def test_known_scammer_signature_matches_a_channel_posting_as_itself(db):
    from backend.scam.engine import _signature_match
    from backend.scam.pipeline import Signatures
    from backend.scam.requests import build_scan_request

    _, (news, post), _ = await _seed_telegram_scans(db)
    req = await build_scan_request(news.id, [post.id])
    hit = _signature_match(Signatures(telegram_ids=[-1009876543210]), req.targets, req.channel)
    assert hit is not None and hit["matched_value"] == "-1009876543210"


@pytest.mark.asyncio
async def test_chat_history_followup_sends_telegram_ids(db):
    from unittest.mock import AsyncMock

    from backend.db.models import ScamCheckRequest
    from backend.scam.service import ScamDetectionService

    (group, _), _, _ = await _seed_telegram_scans(db)
    client = _external(FakeCloud(versions=None))
    client.respond_to_followup = AsyncMock(return_value=None)
    row = ScamCheckRequest(request_id="r", request_type="is_this_scam", status="pending",
                           related_conversation_id=group.id, created_at=T, updated_at=T)
    await ScamDetectionService(client)._handle_followup(row, {"data_type": "chat_history", "params": {}})
    data = client.respond_to_followup.call_args.args[1]
    assert [(d["message_id"], d["sender_id"], d["text"]) for d in data] == [(31, 4242, "send btc"), (30, 4242, "hi")]


@pytest.mark.asyncio
async def test_external_v2_server_gets_hashed_senders(db, unlocked_vault):
    eve = await f.identity(db, "eve@example.com", channel="gmail", display_name="Eve")
    cloud = FakeCloud(versions=[1, 2])
    c = _external(cloud)
    req = _scan_request(channel="gmail", kind="email_thread", identity_id=eve.id)
    for m in req.all_messages() + req.profiles:
        m.pop("telegram_user_id")
    result = await c.is_this_scam("req-2", req)
    assert result.is_scam

    (post,) = cloud.posts
    assert post.headers["x-mingly-contract"] == "2"
    body = json.loads(post.content)
    salt = channel_ids.handle_salt()
    sender = {"handle_hash": _hash(salt, "gmail:eve@example.com")}
    assert body["contract"] == 2 and body["channel"] == "gmail"
    assert body["conversation"] == {"kind": "email_thread", "name": "G"}
    assert [m["sender"] for m in body["messages"]] == [sender, sender]
    assert body["profiles"] == [{"sender": sender, "name": "Mal", "bio": "trader", "is_contact": False}]
    assert body["target_message_ids"] == [11]
    assert b"eve@example.com" not in post.content


# ---------------------------------------------------------------------------
# Dismissals whose rows are deleted before the upload
# ---------------------------------------------------------------------------

@pytest.fixture
def sync_dismissals(monkeypatch):
    from backend.scam import verdict as verdict_mod
    monkeypatch.setattr(verdict_mod, "get_pref", lambda k, d=None: True if k == "shield_sync_dismissals" else d)


async def _dismissed_telegram_scan(db):
    """A scam-scored Telegram message from user 4242 whose sender flag and
    scan the user dismissed (one scan and one scammer dismissal queued)."""
    from backend.scam.verdict import set_verdict

    sender = await f.identity(db, "4242", display_name="Mal")
    conv = await f.conversation(db, "-100777", title="G", kind="group")
    msg = await f.message(db, conv, 31, "send btc", sender=sender, date=T, scam_status="completed",
                          scam_score=0.9, scam_updated_at=T, scam_trace_json=json.dumps(TRACE))
    db.add(ScamFlag(identity_id=sender.id, source="local", confidence=0.9, reason="crypto ask",
                    category="investment", flagged_at=T, updated_at=T))
    await db.commit()
    await set_verdict(scope="message", verdict="not_scam", messages=[msg.id])
    return conv, msg, sender


async def _delete(model, row_id):
    from sqlalchemy import delete
    async with database.AsyncSessionLocal() as s:
        await s.execute(delete(model).where(model.id == row_id))
        await s.commit()


@pytest.mark.asyncio
async def test_v1_dismissals_keep_telegram_ids_after_the_rows_are_deleted(db, sync_dismissals):
    from backend.db.models import Identity
    conv, msg, sender = await _dismissed_telegram_scan(db)
    await _delete(Message, msg.id)  # the scammer deleted it on Telegram
    await _delete(Identity, sender.id)
    cloud = FakeCloud(versions=None)
    assert await _manager(cloud)._upload_pending_dismissals() == 2

    (scan,) = _bodies(cloud, "/sync/scan-results")[0]["scan_results"]
    assert (scan["chat_id"], scan["tg_message_id"], scan["sender_id"]) == (-100777, 31, 4242)
    (dismissal,) = _bodies(cloud, "/sync/dismissals")[0]["dismissals"]
    assert dismissal["telegram_id"] == 4242 and "cloud_v1" not in dismissal and "identity_id" not in dismissal
    async with database.AsyncSessionLocal() as s:
        statuses = (await s.execute(select(ScamDismissal.cloud_status))).scalars().all()
    assert statuses == ["synced", "synced"]


@pytest.mark.asyncio
async def test_v2_dismissed_scan_falls_back_to_the_telegram_snapshot(db, sync_dismissals, unlocked_vault):
    conv, msg, _ = await _dismissed_telegram_scan(db)
    await _delete(Message, msg.id)
    cloud = FakeCloud(versions=[1, 2])
    await _manager(cloud)._upload_pending_dismissals()
    (scan,) = _bodies(cloud, "/sync/scan-results")[0]["scan_results"]
    assert scan["conversation"] == {"kind": "group", "id": "-100777"}
    assert scan["message"] == {"id": "31"} and scan["sender"] == {"id": "4242"}


@pytest.mark.asyncio
async def test_dismissals_that_can_never_upload_are_skipped(db):
    from backend.db.models import Conversation
    rows = await seed(db)
    for ch in ("gmail", "telegram"):  # gone, and no snapshot (recorded before snapshots existed)
        await _delete(Conversation, rows[ch]["conv"].id)
    from backend.db.models import Identity
    await _delete(Identity, rows["gmail"]["sender"].id)
    cloud = FakeCloud(versions=None)
    await _manager(cloud)._upload_pending_dismissals()

    async with database.AsyncSessionLocal() as s:
        dismissals = (await s.execute(select(ScamDismissal))).scalars().all()
    by_ref = {}
    for d in dismissals:
        data = json.loads(d.payload_json)
        by_ref[(d.dismissal_type, data.get("conversation_id") or data.get("identity_id"))] = d
    for key in (("scan", rows["gmail"]["conv"].id), ("scammer", rows["gmail"]["sender"].id)):
        d = by_ref[key]
        assert d.synced_at is not None and d.cloud_status == "skipped"
        assert d.cloud_skipped_reason == "source rows deleted" and "cloud_skipped" not in d.payload_json
    # Signal / SMS rows still exist: they wait for a v2 server
    for ch in ("signal", "sms"):
        for key in (("scan", rows[ch]["conv"].id), ("scammer", rows[ch]["sender"].id)):
            assert by_ref[key].synced_at is None and by_ref[key].cloud_status == "pending"


# ---------------------------------------------------------------------------
# Messages posted "as" another channel (meta.sender_peer)
# ---------------------------------------------------------------------------

async def _posted_as_channel(db):
    """A group where channel -1000000000777 posted twice and another channel
    once, all without a user sender."""
    group = await f.conversation(db, "-1001234567890", title="G", kind="group")
    rows = []
    for seq, text, peer, name, minute in ((40, "promo one", -1000000000777, "Promo", 1),
                                          (41, "other channel", -1000000000888, "Other", 2),
                                          (42, "buy my coin", -1000000000777, "Promo", 3)):
        m = await f.message(db, group, seq, text, date=datetime(2026, 3, 1, 10, minute),
                            meta_json=json.dumps({"sender_peer": peer}), scam_status="completed",
                            scam_score=0.9, scam_updated_at=T, scam_trace_json=json.dumps(TRACE))
        m.sender_name = name
        rows.append(m)
    await db.commit()
    return group, rows


@pytest.mark.asyncio
async def test_v1_requests_send_the_channel_a_message_was_posted_as(db):
    from backend.scam.scan import build_message_context
    from backend.scam.requests import build_scan_request

    group, (first, _, target) = await _posted_as_channel(db)
    req = await build_scan_request(group.id, [target.id])
    cloud = FakeCloud(versions=None)
    await _external(cloud).is_this_scam("req-p", req)
    body = json.loads(cloud.posts[0].content)
    # fb15113 sent Telethon's msg.sender_id: the marked peer id of the channel
    assert [(m["message_id"], m["sender_id"]) for m in body["messages"]] == [(40, -1000000000777), (42, -1000000000777)]
    assert body["profiles"] == [{"telegram_id": -1000000000777, "name": "Promo", "bio": "", "is_contact": False}]
    assert "Profile - ID:-1000000000777, Name:Promo" in build_message_context(req)


@pytest.mark.asyncio
async def test_v1_scan_results_send_the_channel_a_message_was_posted_as(db):
    group, rows = await _posted_as_channel(db)
    cloud = FakeCloud(versions=None)
    await _manager(cloud)._upload_scan_results()
    results = [r for b in _bodies(cloud, "/sync/scan-results") for r in b["scan_results"]]
    assert [(r["tg_message_id"], r["sender_id"]) for r in results] == [
        (40, -1000000000777), (41, -1000000000888), (42, -1000000000777)]


@pytest.mark.asyncio
@pytest.mark.parametrize("posted_as", [-1000000000777, None])  # another channel / the group itself (anonymous admin)
async def test_identity_less_context_is_exact_among_many_other_channel_posts(db, posted_as):
    """Context for a sender without an identity is matched exactly in SQL:
    over 10x as many unrelated identity-less posts in between can't crowd
    out the sender's own earlier messages."""
    from backend.scam.requests import _DEFAULT_CONTEXT, build_scan_request

    group = await f.conversation(db, "-1001234567890", title="G", kind="group")
    name = "Promo" if posted_as else "G"
    meta = json.dumps({"sender_peer": posted_as}) if posted_as else None
    seq, own = 100, []
    for i in range(_DEFAULT_CONTEXT + 2):
        for _ in range(12):  # unrelated: another channel posting
            seq += 1
            m = await f.message(db, group, seq, f"noise {seq}", meta_json=json.dumps({"sender_peer": -1000000000999}))
            m.sender_name = "Noise"
        seq += 1
        m = await f.message(db, group, seq, f"own {i}", meta_json=meta)
        m.sender_name = name
        own.append(m)
    await db.commit()
    target = own[-1]
    req = await build_scan_request(group.id, [target.id])
    assert [m["text"] for m in req.context] == [m.text for m in own[-1 - _DEFAULT_CONTEXT:-1]]
    assert {m["telegram_sender_id"] for m in req.context} == {posted_as or -1001234567890}
