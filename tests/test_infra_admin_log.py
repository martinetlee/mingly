"""Admin action log: raw-SQL datetimes come back as strings from SQLite."""

import pytest

from backend.api.admin import _db_datetime, _log_admin_action
from backend.db.models import Chat


@pytest.mark.asyncio
async def test_action_log_returns_utc_iso(api, db):
    db.add(Chat(telegram_id=-100777, name="Admins", type="group"))
    await db.commit()
    await _log_admin_action(db, -100777, "ban", target_user_id=42, details="spam")

    r = await api.get("/api/admin/-100777/action-log")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["action"] == "ban" and rows[0]["target_user_id"] == 42
    assert rows[0]["created_at"].endswith("Z")
    assert "T" in rows[0]["created_at"]


def test_db_datetime_parses_sqlite_strings():
    assert _db_datetime(None) is None
    assert _db_datetime("2026-10-06 12:30:00").hour == 12
    assert _db_datetime("2026-10-06 12:30:00.123456").microsecond == 123456
    assert _db_datetime("garbage") is None


@pytest.mark.asyncio
async def test_admin_action_without_telegram_is_503(api, db):
    from unittest.mock import patch

    db.add(Chat(telegram_id=-100778, name="Admins2", type="group"))
    await db.commit()
    with patch("backend.telegram.client.get_client", return_value=None):
        r = await api.get("/api/admin/-100778/invite-links")
    assert r.status_code == 503
    assert isinstance(r.json()["detail"], str)
