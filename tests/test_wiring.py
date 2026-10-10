"""The composition root connects Shield to ingest's inspector port, and the
port delegates every hook to the scan queue."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.ingest import fanout
from backend.scam.inspector import shield_inspector


def test_create_app_wires_shield_into_ingest():
    from backend.api.router import create_app

    create_app()
    assert shield_inspector in fanout._inspectors


@pytest.mark.asyncio
async def test_new_message_reaches_the_scan_queue():
    with patch("backend.scam.queue.enqueue_targets", AsyncMock()) as enqueue:
        await fanout.inspect_messages(7, [1, 2], "realtime")
    enqueue.assert_awaited_once_with(7, [1, 2], source="realtime")


@pytest.mark.asyncio
async def test_backlog_and_sender_hints_reach_the_scan_queue():
    with patch("backend.scam.queue.scan_unscanned_messages", AsyncMock(return_value=0)) as scan, \
         patch("backend.scam.queue.note_sender_profile") as note:
        await fanout.inspect_backlog()
        fanout.note_sender_profile(5, {"bio": "hi", "is_contact": False})
    scan.assert_awaited_once_with(max_messages=200, max_age_hours=48)
    note.assert_called_once_with(5, bio="hi", is_contact=False)


@pytest.mark.asyncio
async def test_a_failing_inspector_does_not_break_ingest():
    class Boom:
        async def inspect(self, *a):
            raise RuntimeError("boom")

    boom = Boom()
    fanout.add_message_inspector(boom)
    try:
        with patch("backend.scam.queue.enqueue_targets", AsyncMock()) as enqueue:
            await fanout.inspect_messages(1, [1], "edit")
        enqueue.assert_awaited_once()  # the failure is contained; other inspectors ran
    finally:
        fanout.remove_message_inspector(boom)
