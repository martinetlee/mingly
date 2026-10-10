"""Regression tests for features that were fully broken by refactors
(see reviews/APP_REVIEW_2026-10-06.md §1)."""

from unittest.mock import patch

import pytest

from tests.llm_fakes import seed_chat as _seed_chat_v2, seed_msg as _seed_msg


async def _seed_chat(db, external_id=-1001, name="Group", **flags):
    return await _seed_chat_v2(db, external_id, name, **flags)


@pytest.mark.asyncio
async def test_global_search_with_message_hit(api, db):
    conv = await _seed_chat(db)
    msg = await _seed_msg(db, conv, 42, "the quarterly roadmap")
    resp = await api.get("/api/search", params={"q": "roadmap"})
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["messages"]
    assert msgs and msgs[0]["message_id"] == msg.id


def test_scam_api_has_spawn():
    import backend.api.scam as scam_api
    assert callable(getattr(scam_api, "spawn", None))


def test_on_new_message_does_not_shadow_chat_type():
    """A local assignment to _chat_type inside on_new_message made the module
    helper unreachable (UnboundLocalError) and dropped first messages."""
    import backend.telegram.sync as sync

    handler = next(
        c for c in sync.register_event_handlers.__code__.co_consts
        if getattr(c, "co_name", None) == "on_new_message"
    )
    assert "_chat_type" not in handler.co_varnames
    assert "_chat_type" not in handler.co_cellvars


@pytest.mark.asyncio
async def test_scheduled_briefing_runs_with_tracked_and_briefing_chats(db):
    """P0-4: the scheduler crashed on row shapes / msg.telegram_id. It now runs
    the shared pipeline end to end."""
    from datetime import datetime as _dt
    from sqlalchemy import select
    from backend.db.models import BriefingEntry
    from tests.llm_fakes import FakeProvider, patch_briefing_env
    import backend.briefing_scheduler as sched

    tracked = await _seed_chat(db, -1001, "Tracked", is_tracked=True)
    briefing = await _seed_chat(db, -1002, "Briefing", is_briefing=True)
    await _seed_msg(db, tracked, 1, "hello tracked")
    await _seed_msg(db, briefing, 2, "hello briefing")

    provider = FakeProvider(summary="- summary [1]")
    sched._note_success()
    with patch_briefing_env(provider) as env:
        assert await sched.check_and_run(_dt.now())
    entries = (await db.execute(select(BriefingEntry))).scalars().all()
    assert sorted(e.conversation_title for e in entries) == ["Briefing", "Tracked"]
    assert all(e.sources_json for e in entries)


@pytest.mark.asyncio
async def test_manual_briefing_endpoint_streams_tracked_and_briefing_chats(api, db):
    """P0-4: the manual stream died right after 'start'."""
    import json as _json
    from tests.llm_fakes import FakeProvider, patch_briefing_env

    tracked = await _seed_chat(db, -1001, "Tracked", is_tracked=True)
    briefing = await _seed_chat(db, -1002, "Briefing", is_briefing=True)
    await _seed_msg(db, tracked, 1, "hello tracked")
    await _seed_msg(db, briefing, 2, "hello briefing")

    with patch_briefing_env(FakeProvider(summary="- summary [1]")):
        resp = await api.get("/api/intelligence/briefing", params={"days": 1})
    assert resp.status_code == 200
    events = []
    for block in resp.text.strip().split("\n\n"):
        lines = block.split("\n")
        events.append((lines[0][7:], _json.loads(lines[1][6:])))
    names = [e for e, _ in events]
    assert "error" not in names, events
    done = sorted(d["conversation_title"] for e, d in events if e == "chat_done")
    assert done == ["Briefing", "Tracked"]
    assert names[-1] == "done"
    run_id = events[-1][1]["run_id"]

    history = (await api.get("/api/intelligence/briefing/history")).json()
    assert history[0]["id"] == run_id and history[0]["status"] == "done"
    assert history[0]["generated_at"].endswith("Z")
    assert len(history[0]["entries"]) == 2
    assert {e["conversation_id"] for e in history[0]["entries"]} == {tracked.id, briefing.id}


@pytest.mark.asyncio
async def test_forum_topics_use_messages_namespace():
    from telethon.tl.functions import messages
    import backend.api.conversations as conversations_api
    import inspect

    src = inspect.getsource(conversations_api)
    assert "functions.channels import GetForumTopicsRequest" not in src
    assert "functions.channels import CreateForumTopicRequest" not in src
    assert "functions.channels import EditForumTopicRequest" not in src
    for name in ("GetForumTopicsRequest", "CreateForumTopicRequest", "EditForumTopicRequest"):
        assert hasattr(messages, name)


@pytest.mark.asyncio
async def test_ollama_pull_inline_error_marks_job_failed():
    import asyncio
    import backend.api.llm as llm_api

    class FakeOllama:
        async def pull_model_stream(self, model):
            yield {"status": "pulling manifest"}
            yield {"error": "pull model manifest: file does not exist"}

    with patch.object(llm_api, "_get_ollama", return_value=FakeOllama()):
        resp = await llm_api.start_pull(llm_api.PullRequest(model="nope"))
        job_id = resp["job_id"]
        for _ in range(50):
            if llm_api._jobs[job_id]["status"] in ("error", "done"):
                break
            await asyncio.sleep(0.01)
    assert llm_api._jobs[job_id]["status"] == "error"
