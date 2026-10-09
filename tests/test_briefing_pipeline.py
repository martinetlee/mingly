"""Briefing pipeline: transcript budgeting, citations, latest-N windows,
end-to-end runs, failure handling, regeneration window and single-flight."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from backend.briefing import pipeline
from backend.briefing.events import get_event_job, start_event_job
from backend.briefing.pipeline import Line, build_transcript, finalize_summary
from backend.db import crud
from backend.db.models import BriefingEntry, BriefingRun
from backend.llm.base import LLMModelMissing, LLMTimeout, LLMUnavailable, StreamDelta
from backend.llm.budget import estimate_tokens
from tests import factories
from tests.llm_fakes import FakeProvider, patch_briefing_env, seed_chat, seed_msg


class _Job:
    """Minimal job sink collecting events."""

    def __init__(self):
        self.events = []
        self.progress = {}

    def emit(self, event, data):
        self.events.append((event, data))

    def names(self):
        return [e for e, _ in self.events]


def _lines(n, size=50):
    return [Line(-1, i, "A", f"A: {'x' * size} {i}") for i in range(1, n + 1)]


def test_build_transcript_keeps_newest_within_budget():
    text, refs = build_transcript(_lines(100), 200)
    kept_ids = [ln.message_id for ln in refs.values()]
    assert kept_ids == sorted(kept_ids)
    assert kept_ids[-1] == 100  # newest always kept
    assert kept_ids[0] > 1  # oldest dropped
    assert estimate_tokens(text) <= 210
    assert text.startswith("[1] ")


def test_transcript_budget_counts_cjk_bytes_and_respects_num_ctx():
    cjk = [Line(-1, i, "A", "甲" * 100) for i in range(1, 200)]
    text, refs = build_transcript(cjk, 1000)
    assert estimate_tokens(text) <= 1050  # ~1 token per CJK char, not 1/3
    small = FakeProvider(context_tokens=8192)
    big = FakeProvider(context_tokens=None)
    assert pipeline.transcript_budget(small, 4000) < 8192 * 0.85 - 1500
    assert pipeline.transcript_budget(big, 4000) > pipeline.transcript_budget(small, 4000)


def test_finalize_summary_maps_and_renumbers_citations():
    refs = {1: Line(-1, 501, "A", "a"), 2: Line(-1, 502, "B", "b"), 3: Line(-1, 503, "C", "c")}
    raw = "<think>hidden</think>**Funding**\n- Round closed [3][1]\n- @foo_bar joined [1, 2] [99]"
    text, sources = finalize_summary(raw, refs)
    assert "hidden" not in text
    assert "**Funding**" in text  # markdown preserved, not stripped
    assert "@foo_bar" in text
    assert "Round closed [1][2]" in text
    assert "joined [2][3]" in text and "[99]" not in text
    assert sources == [
        {"conversation_id": -1, "message_id": 503},
        {"conversation_id": -1, "message_id": 501},
        {"conversation_id": -1, "message_id": 502},
    ]


def test_finalize_summary_accepts_message_ids_and_json_wrapping():
    refs = {1: Line(-1, 777, "A", "a")}
    text, sources = finalize_summary('{"summary": "Deal done [id:777]"}', refs)
    assert text == "Deal done [1]"
    assert sources == [{"conversation_id": -1, "message_id": 777}]


@pytest.mark.asyncio
async def test_messages_since_returns_newest_window_ascending(db):
    conv = await seed_chat(db, -1001, "Busy")
    for i in range(1, 11):
        await seed_msg(db, conv, i, f"m{i}", minutes_ago=100 - i)
    rows = await crud.get_messages_since(db, conv.id, datetime.utcnow() - timedelta(days=1), limit=3)
    assert [r[0].seq for r in rows] == [8, 9, 10]


@pytest.mark.asyncio
async def test_messages_from_sender_since_returns_newest_window(db):
    conv = await seed_chat(db, -1001, "G")
    bob = await factories.identity(db, 42, display_name="Bob")
    for i in range(1, 6):
        await seed_msg(db, conv, i, f"m{i}", minutes_ago=50 - i, sender=bob)
    rows = await crud.get_messages_from_sender_since(db, [bob.id], datetime.utcnow() - timedelta(days=1), limit=2)
    assert [r[0].seq for r in rows] == [4, 5]


async def _seed_tracked_and_briefing(db):
    tracked = await seed_chat(db, -1001, "Tracked", is_tracked=True)
    briefing = await seed_chat(db, -1002, "Briefing", is_briefing=True)
    msg = await seed_msg(db, tracked, 1, "hello tracked")
    await seed_msg(db, briefing, 2, "hello briefing")
    return tracked, msg


@pytest.mark.asyncio
async def test_run_briefing_persists_entries_with_sources(db):
    tracked, msg = await _seed_tracked_and_briefing(db)
    provider = FakeProvider(summary="- Something happened [1]")
    job = _Job()
    with patch_briefing_env(provider) as env:
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "done" and outcome.ok == 2
    assert "briefing_last_run" in env.prefs
    run = (await db.execute(select(BriefingRun))).scalar_one()
    assert run.status == "done" and run.finished_at is not None
    entries = (await db.execute(select(BriefingEntry).order_by(BriefingEntry.id))).scalars().all()
    assert sorted(e.conversation_title for e in entries) == ["Briefing", "Tracked"]
    assert all(e.summary == "- Something happened [1]" for e in entries)
    srcs = {e.conversation_title: json.loads(e.sources_json) for e in entries}
    assert srcs["Tracked"] == [{"conversation_id": tracked.id, "message_id": msg.id}]
    done = [d for e, d in job.events if e == "chat_done"]
    assert {d["conversation_id"] for d in done} == {e.conversation_id for e in entries}
    assert all(d["person_id"] is None for d in done)
    names = job.names()
    assert names[0] == "progress" and "start" in names and names[-1] == "done"
    assert names.count("chat_done") == 2
    assert any(e == "thinking" and "reasoning" in d for e, d in job.events)


@pytest.mark.asyncio
async def test_important_person_is_briefed_across_channels(db):
    from sqlalchemy import update
    from backend.db.models import Identity, Person

    tg = await factories.identity(db, 42, display_name="Bob")
    mail = await factories.identity(db, "bob@example.com", channel="gmail", display_name="Bob M")
    # One human, two channels: link the gmail identity to Bob's person.
    await db.execute(update(Identity).where(Identity.id == mail.id).values(person_id=tg.person_id))
    await db.execute(update(Person).where(Person.id == tg.person_id).values(is_important=True))
    await db.commit()
    group = await seed_chat(db, -1001, "Builders")
    thread = await seed_chat(db, "t-1", "Term sheet", kind="email_thread", channel="gmail")
    await seed_msg(db, group, 1, "shipping friday", sender=tg)
    await seed_msg(db, thread, 1700000000000, "see attached terms", sender=mail)

    provider = FakeProvider(summary="- Bob is busy [1][2]")
    job = _Job()
    with patch_briefing_env(provider):
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "done"
    entry = (await db.execute(select(BriefingEntry))).scalar_one()
    assert entry.category == "important_contact"
    assert entry.person_id == tg.person_id and entry.conversation_id is None
    prompt = provider.stream_calls[0][-1].content
    assert "(Builders · telegram) shipping friday" in prompt
    assert "(Term sheet · gmail) see attached terms" in prompt
    assert {src["conversation_id"] for src in json.loads(entry.sources_json)} == {group.id, thread.id}

    regen = _Job()
    with patch_briefing_env(FakeProvider(summary="- again [1]")):
        payload = await pipeline.regenerate_entry(regen, entry.run_id, entry.id)
    assert payload["person_id"] == tg.person_id and payload["conversation_id"] is None


@pytest.mark.asyncio
async def test_quiet_chats_are_reported_not_summarized(db):
    await seed_chat(db, -1001, "Quiet", is_tracked=True)
    provider = FakeProvider()
    job = _Job()
    with patch_briefing_env(provider):
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "done"
    assert provider.stream_calls == []
    entry = (await db.execute(select(BriefingEntry))).scalar_one()
    assert entry.summary == pipeline.NO_MESSAGES


@pytest.mark.asyncio
async def test_unavailable_llm_aborts_without_placeholder_run(db):
    await _seed_tracked_and_briefing(db)
    provider = FakeProvider(fail_with=LLMUnavailable("connection refused"))
    job = _Job()
    with patch_briefing_env(provider) as env:
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "failed" and outcome.error_kind == "llm_unavailable"
    assert "briefing_last_run" not in env.prefs
    assert (await db.execute(select(BriefingRun))).scalars().all() == []
    assert (await db.execute(select(BriefingEntry))).scalars().all() == []
    err = [d for e, d in job.events if e == "error"][-1]
    assert err["kind"] == "llm_unavailable" and err["run_id"] is None
    # aborted after the first chat — the second was never attempted
    assert len(provider.stream_calls) == 1


@pytest.mark.asyncio
async def test_model_missing_after_success_keeps_partial_run(db):
    await _seed_tracked_and_briefing(db)
    provider = FakeProvider(fail_with=LLMModelMissing("gone"), fail_on_call=1)
    job = _Job()
    with patch_briefing_env(provider):
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "failed" and outcome.run_id is not None
    run = (await db.execute(select(BriefingRun))).scalar_one()
    assert run.status == "partial" and run.error
    assert len((await db.execute(select(BriefingEntry))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_timeout_skips_chat_and_marks_partial(db):
    await _seed_tracked_and_briefing(db)
    provider = FakeProvider(fail_with=LLMTimeout("slow"), fail_on_call=1)
    job = _Job()
    with patch_briefing_env(provider):
        outcome = await pipeline.run_briefing(job, 1)
    assert outcome.status == "partial" and outcome.ok == 1 and outcome.failed == 1
    assert "chat_error" in job.names()
    entries = (await db.execute(select(BriefingEntry))).scalars().all()
    assert len(entries) == 1 and "unavailable" not in entries[0].summary.lower()


@pytest.mark.asyncio
async def test_regenerate_uses_the_runs_window(db):
    conv = await seed_chat(db, -1001, "Tracked", is_tracked=True)
    old = await seed_msg(db, conv, 1, "old news", minutes_ago=60 * 30)  # 30h ago
    run = await crud.create_briefing_run(db, days=1)
    run.generated_at = datetime.utcnow() - timedelta(hours=24)
    await db.commit()
    entry = await crud.add_briefing_entry(
        db, run.id, conversation_id=conv.id, conversation_title="Tracked", category="tracked", summary="stale",
    )
    await seed_msg(db, conv, 2, "brand new today", minutes_ago=5)

    provider = FakeProvider(summary="- refreshed [1]")
    job = _Job()
    with patch_briefing_env(provider):
        payload = await pipeline.regenerate_entry(job, run.id, entry.id)
    assert payload["summary"] == "- refreshed [1]"
    prompt = provider.stream_calls[0][-1].content
    assert "old news" in prompt and "brand new today" not in prompt
    assert payload["sources"] == [{"conversation_id": conv.id, "message_id": old.id}]
    assert payload["conversation_id"] == conv.id and payload["conversation_title"] == "Tracked"


@pytest.mark.asyncio
async def test_regenerate_failure_leaves_entry_untouched(db):
    conv = await seed_chat(db, -1001, "Tracked", is_tracked=True)
    run = await crud.create_briefing_run(db, days=1)
    entry = await crud.add_briefing_entry(
        db, run.id, conversation_id=conv.id, conversation_title="Tracked", category="tracked", summary="keep me",
    )
    await seed_msg(db, conv, 1, "hi there")
    job = _Job()
    with patch_briefing_env(FakeProvider(fail_with=LLMUnavailable("down"))):
        assert await pipeline.regenerate_entry(job, run.id, entry.id) is None
    await db.refresh(entry)
    assert entry.summary == "keep me"
    assert job.names()[-1] == "error"


@pytest.mark.asyncio
async def test_event_job_single_flight_and_replay():
    gate = asyncio.Event()

    async def body(job):
        job.emit("start", {"total": 1})
        await gate.wait()
        job.emit("done", {"run_id": 1})
        return "ok"

    job1, started1 = start_event_job("test-sf", "t", body)
    job2, started2 = start_event_job("test-sf", "t", body)
    assert started1 and not started2 and job1 is job2
    await asyncio.sleep(0)

    async def collect():
        return [e async for e, _ in job1.subscribe()]

    late = asyncio.ensure_future(collect())
    await asyncio.sleep(0.01)
    gate.set()
    assert await late == ["start", "done"]
    assert await job1.task == "ok"
    assert get_event_job("test-sf") is None


@pytest.mark.asyncio
async def test_event_job_error_becomes_error_event():
    async def body(job):
        raise LLMUnavailable("no model")

    job, _ = start_event_job("test-err", "t", body)
    events = [(e, d) async for e, d in job.subscribe()]
    assert events[-1][0] == "error" and events[-1][1]["kind"] == "llm_unavailable"


@pytest.mark.asyncio
async def test_briefing_attach_only_never_starts_a_run(api, db):
    await _seed_tracked_and_briefing(db)
    provider = FakeProvider()
    with patch_briefing_env(provider):
        resp = await api.get("/api/intelligence/briefing", params={"attach": 1})
    assert resp.status_code == 200
    assert resp.text == "event: idle\ndata: {}\n\n"
    assert provider.stream_calls == []
    assert (await db.execute(select(BriefingRun))).scalars().all() == []


@pytest.mark.asyncio
async def test_briefing_with_different_days_gets_409_not_silent_join(api, db):
    gate = asyncio.Event()

    async def body(job):
        job.emit("start", {"total": 1, "days": 1, "run_id": None})
        await gate.wait()
        job.emit("done", {"run_id": None, "status": "empty", "entries": 0, "failed": 0})

    job, _ = start_event_job(pipeline.BRIEFING_JOB_KEY, "briefing", body, meta={"days": 1, "trigger": "scheduled"})
    try:
        resp = await api.get("/api/intelligence/briefing", params={"days": 7})
        assert resp.status_code == 409
        body_json = resp.json()
        assert body_json["code"] == "briefing_running"
        assert body_json["running_days"] == 1 and body_json["requested_days"] == 7
        assert body_json["job_id"] == job.id
    finally:
        gate.set()
        await job.task


class _GatedProvider(FakeProvider):
    def __init__(self, gate, **kw):
        super().__init__(**kw)
        self.gate = gate

    async def stream(self, messages, *, max_tokens=1024, temperature=0.3):
        self.stream_calls.append(list(messages))
        await self.gate.wait()
        yield StreamDelta(content="- ok [1]")


@pytest.mark.asyncio
async def test_replace_cancels_running_briefing_and_starts_requested_window(api, db):
    import json as _json
    await _seed_tracked_and_briefing(db)

    first_gate = asyncio.Event()
    with patch_briefing_env(_GatedProvider(first_gate)):
        daily, started = pipeline.start_briefing_job(1, trigger="scheduled")
        assert started
        for _ in range(50):  # wait until the daily run is blocked inside the LLM call
            if daily.progress.get("run_id"):
                break
            await asyncio.sleep(0.01)
        assert daily.progress["run_id"]

    with patch_briefing_env(FakeProvider(summary="- weekly [1]")):
        resp = await api.get("/api/intelligence/briefing", params={"days": 7, "replace": 1})
    assert resp.status_code == 200
    events = []
    for block in resp.text.strip().split("\n\n"):
        lines = block.split("\n")
        events.append((lines[0][7:], _json.loads(lines[1][6:])))
    start = next(d for e, d in events if e == "start")
    assert start["days"] == 7 and start["replaced_job_id"] == daily.id and start["attached"] is False
    assert events[-1][0] == "done"

    assert daily.task.cancelled() and daily.events[-1][0] == "cancelled"
    runs = (await db.execute(select(BriefingRun))).scalars().all()
    assert [r.days for r in runs] == [7]  # cancelled daily run had no entries → removed
    assert get_event_job(pipeline.BRIEFING_JOB_KEY) is None


@pytest.mark.asyncio
async def test_dashboard_latest_briefing_entries_carry_sources(api, db):
    conv = await seed_chat(db, -1, "G")
    run = await crud.create_briefing_run(db, days=1)
    await crud.add_briefing_entry(db, run.id, conversation_id=conv.id, conversation_title="G", category="tracked",
                                  summary="x [1]",
                                  sources_json=json.dumps([{"conversation_id": conv.id, "message_id": 5}]))
    data = (await api.get("/api/dashboard")).json()
    assert data["latest_briefing"]["entries"][0]["sources"] == [{"conversation_id": conv.id, "message_id": 5}]
    assert data["latest_briefing"]["generated_at"].endswith("Z")
