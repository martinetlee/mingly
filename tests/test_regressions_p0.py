"""Regression tests for features that were fully broken by refactors
(see reviews/APP_REVIEW_2026-10-06.md §1)."""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from backend.db.models import Chat, Message


async def _seed_chat(db, telegram_id=-1001, name="Group", **flags):
    chat = Chat(telegram_id=telegram_id, name=name, type="group", **flags)
    db.add(chat)
    await db.commit()
    return chat


async def _seed_msg(db, chat_id, tg_id, text, minutes_ago=5):
    msg = Message(
        tg_message_id=tg_id, chat_id=chat_id, sender_name="Alice", text=text,
        date=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )
    db.add(msg)
    await db.commit()
    return msg


@pytest.mark.asyncio
async def test_global_search_with_message_hit(api, db):
    await _seed_chat(db)
    await _seed_msg(db, -1001, 42, "the quarterly roadmap")
    resp = await api.get("/api/search", params={"q": "roadmap"})
    assert resp.status_code == 200, resp.text
    msgs = resp.json()["messages"]
    assert msgs and msgs[0]["tg_message_id"] == 42


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

    await _seed_chat(db, -1001, "Tracked", is_tracked=True)
    await _seed_chat(db, -1002, "Briefing", is_briefing=True)
    await _seed_msg(db, -1001, 1, "hello tracked")
    await _seed_msg(db, -1002, 2, "hello briefing")

    provider = FakeProvider(summary="- summary [1]")
    sched._note_success()
    with patch_briefing_env(provider) as env, \
         patch.object(sched, "_load_prefs", side_effect=lambda: dict(env.prefs)):
        assert await sched.check_and_run(_dt.now())
    entries = (await db.execute(select(BriefingEntry))).scalars().all()
    assert sorted(e.chat_name for e in entries) == ["Briefing", "Tracked"]
    assert all(e.sources_json for e in entries)


@pytest.mark.asyncio
async def test_manual_briefing_endpoint_streams_tracked_and_briefing_chats(api, db):
    """P0-4: the manual stream died right after 'start'."""
    import json as _json
    from tests.llm_fakes import FakeProvider, patch_briefing_env

    await _seed_chat(db, -1001, "Tracked", is_tracked=True)
    await _seed_chat(db, -1002, "Briefing", is_briefing=True)
    await _seed_msg(db, -1001, 1, "hello tracked")
    await _seed_msg(db, -1002, 2, "hello briefing")

    with patch_briefing_env(FakeProvider(summary="- summary [1]")):
        resp = await api.get("/api/intelligence/briefing", params={"days": 1})
    assert resp.status_code == 200
    events = []
    for block in resp.text.strip().split("\n\n"):
        lines = block.split("\n")
        events.append((lines[0][7:], _json.loads(lines[1][6:])))
    names = [e for e, _ in events]
    assert "error" not in names, events
    done = sorted(d["chat_name"] for e, d in events if e == "chat_done")
    assert done == ["Briefing", "Tracked"]
    assert names[-1] == "done"
    run_id = events[-1][1]["run_id"]

    history = (await api.get("/api/intelligence/briefing/history")).json()
    assert history[0]["id"] == run_id and history[0]["status"] == "done"
    assert history[0]["generated_at"].endswith("Z")
    assert len(history[0]["entries"]) == 2


@pytest.mark.asyncio
async def test_forum_topics_use_messages_namespace():
    from telethon.tl.functions import messages
    import backend.api.chats as chats_api
    import inspect

    src = inspect.getsource(chats_api)
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
