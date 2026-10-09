"""Briefing scheduling when no LLM is configured (found by live smoke test)."""

from datetime import datetime

import pytest

from tests import factories


@pytest.mark.asyncio
async def test_scheduler_waits_quietly_without_llm(db, monkeypatch):
    import backend.briefing_scheduler as sched

    await factories.conversation(db, -1001, title="Tracked", kind="group", is_tracked=True)
    started = []
    monkeypatch.setattr(sched, "llm_configured", lambda role="orchestrator": False)
    monkeypatch.setattr(sched, "start_briefing_job", lambda *a, **k: started.append(1))
    monkeypatch.setattr(sched.schedule, "is_due", lambda *a, **k: True)
    monkeypatch.setattr(sched, "_retry_after", None)
    monkeypatch.setattr(sched, "_failures", 0)

    assert await sched.check_and_run(datetime(2026, 10, 6, 9, 1)) is False
    assert started == []
    assert sched._failures == 0 and sched._retry_after is None


@pytest.mark.asyncio
async def test_empty_briefing_completes_without_llm(db, monkeypatch):
    import backend.briefing.pipeline as pipeline
    from backend.llm.resolve import LLMUnavailable

    def no_provider(role="orchestrator"):
        raise LLMUnavailable("No LLM is configured")

    monkeypatch.setattr(pipeline, "get_provider", no_provider)
    events = []

    class Job:
        def emit(self, kind, data):
            events.append(kind)

    outcome = await pipeline.run_briefing(Job(), 1, sync_first=False)
    assert outcome.status == "empty"
    assert events[-1] == "done"
