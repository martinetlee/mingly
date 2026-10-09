"""GET /api/scam/sync/history: dismissals count by cloud_status — a skipped
dismissal (no cloud contract carries it) is neither synced nor pending."""

import json
from datetime import datetime

import pytest

from backend.db.models import ScamDismissal


@pytest.mark.asyncio
async def test_dismissal_counts_split_synced_pending_skipped(api, db):
    now = datetime.utcnow()
    rows = [
        ("pending", None), ("pending", None),
        ("synced", now),
        ("skipped", now), ("skipped", now), ("skipped", now),  # left the queue without an upload
    ]
    for status, synced_at in rows:
        db.add(ScamDismissal(dismissal_type="scan", payload_json=json.dumps({}), cloud_status=status, synced_at=synced_at))
    await db.commit()

    r = await api.get("/api/scam/sync/history")
    assert r.status_code == 200
    assert r.json()["dismissals"] == {"total": 6, "synced": 1, "pending": 2, "skipped": 3}


@pytest.mark.asyncio
async def test_dismissal_counts_empty(api):
    r = await api.get("/api/scam/sync/history")
    assert r.json()["dismissals"] == {"total": 0, "synced": 0, "pending": 0, "skipped": 0}
