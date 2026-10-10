"""Scheduled briefing: next-due logic and the scheduler tick."""

from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from backend.briefing import schedule
from backend.db.models import BriefingEntry, BriefingRun
from backend.llm.base import LLMUnavailable
from tests.llm_fakes import FakeProvider, patch_briefing_env, seed_chat, seed_msg

# 2026-10-07 is a Wednesday.
WED_1000 = datetime(2026, 10, 7, 10, 0)
WED_0800 = datetime(2026, 10, 7, 8, 0)


def test_daily_slot_today_or_yesterday():
    assert schedule.latest_slot(WED_1000, "daily", "09:00", None) == datetime(2026, 10, 7, 9, 0)
    assert schedule.latest_slot(WED_0800, "daily", "09:00", None) == datetime(2026, 10, 6, 9, 0)


def test_weekly_slot_honors_day_and_time():
    assert schedule.latest_slot(WED_1000, "weekly", "09:30", "wednesday") == datetime(2026, 10, 7, 9, 30)
    assert schedule.latest_slot(WED_0800, "weekly", "09:30", "wednesday") == datetime(2026, 9, 30, 9, 30)
    assert schedule.latest_slot(WED_1000, "weekly", "18:00", "friday") == datetime(2026, 10, 2, 18, 0)


def test_is_due():
    assert schedule.is_due(WED_1000, "daily", "09:00", None, None)
    assert not schedule.is_due(WED_1000, "daily", "09:00", None, "2026-10-07T09:05:00")
    assert schedule.is_due(WED_1000, "daily", "09:00", None, "2026-10-06T23:00:00")
    # weekly on Friday: a run last Saturday covers this week's slot (Fri 2nd)
    assert not schedule.is_due(WED_1000, "weekly", "09:00", "friday", "2026-10-03T12:00:00")
    assert schedule.is_due(WED_1000, "weekly", "09:00", "friday", "2026-10-01T12:00:00")


@pytest.mark.parametrize("bad", ["99:99", "9", "", None, "ab:cd", "25:00"])
def test_invalid_time_never_raises(bad):
    slot = schedule.latest_slot(WED_1000, "daily", bad, None)
    assert slot == datetime(2026, 10, 7, 9, 0)
    assert schedule.parse_day("someday") == 0
    assert schedule.parse_last_run("garbage") is None


async def _seed(db):
    tracked = await seed_chat(db, -1001, "Tracked", is_tracked=True)
    briefing = await seed_chat(db, -1002, "Briefing", is_briefing=True)
    await seed_msg(db, tracked, 1, "hello tracked")
    await seed_msg(db, briefing, 2, "hello briefing")


@pytest.mark.asyncio
async def test_scheduler_tick_runs_and_records_success(db):
    import backend.briefing_scheduler as sched
    await _seed(db)
    sched._note_success()
    with patch_briefing_env(FakeProvider()) as env:
        assert await sched.check_and_run(WED_1000)
        assert "briefing_last_run" in env.prefs
        assert sched._failures == 0
    entries = (await db.execute(select(BriefingEntry))).scalars().all()
    assert sorted(e.conversation_title for e in entries) == ["Briefing", "Tracked"]


@pytest.mark.asyncio
async def test_scheduler_failure_backs_off_and_keeps_last_run(db):
    import backend.briefing_scheduler as sched
    await _seed(db)
    sched._note_success()
    try:
        with patch_briefing_env(FakeProvider(fail_with=LLMUnavailable("down"))) as env:
            assert await sched.check_and_run(WED_1000)
            assert "briefing_last_run" not in env.prefs
            assert sched._failures == 1 and sched._retry_after is not None
            # Within the backoff window nothing is attempted.
            assert not await sched.check_and_run(WED_1000)
        assert (await db.execute(select(BriefingRun))).scalars().all() == []
    finally:
        sched._note_success()
