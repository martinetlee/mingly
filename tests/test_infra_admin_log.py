"""Admin action log: recorded per conversation, targets as identities."""

from unittest.mock import patch

import pytest

from backend.db.crud import log_admin_action
from tests import factories as f


@pytest.mark.asyncio
async def test_action_log_returns_utc_iso_and_target_person(api, db):
    group = await f.conversation(db, "-100777", title="Admins", kind="group")
    spammer = await f.identity(db, "42", display_name="Spammer")
    await log_admin_action(db, group.id, "ban", target_identity_id=spammer.id, details="spam")

    r = await api.get(f"/api/admin/{group.id}/action-log")
    assert r.status_code == 200
    [row] = r.json()
    assert row["action"] == "ban" and row["conversation_id"] == group.id
    assert row["target_identity_id"] == spammer.id and row["target_person_id"] == spammer.person_id
    assert row["created_at"].endswith("Z") and "T" in row["created_at"]


@pytest.mark.asyncio
async def test_admin_action_without_telegram_is_503(api, db):
    group = await f.conversation(db, "-100778", title="Admins2", kind="group")
    with patch("backend.telegram.client.get_client", return_value=None):
        r = await api.get(f"/api/admin/{group.id}/invite-links")
    assert r.status_code == 503
    assert isinstance(r.json()["detail"], str)


@pytest.mark.asyncio
async def test_admin_actions_need_a_telegram_group(api, db):
    dm = await f.conversation(db, "77", title="Ann")
    assert (await api.get(f"/api/admin/{dm.id}/action-log")).status_code == 400
    mail = await f.conversation(db, "thread-1", title="Hi", kind="email_thread", channel="gmail")
    r = await api.get(f"/api/admin/{mail.id}/action-log")
    assert r.status_code == 409 and r.json()["code"] == "unsupported_channel"
    assert (await api.get("/api/admin/999999/action-log")).status_code == 404
