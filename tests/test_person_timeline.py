"""Person timeline (GET /api/people/{id}/timeline): one-to-one conversations in
both directions, group messages the person sent, emails they take part in —
newest first, each message once, paged by (date, id)."""

from datetime import timedelta

import pytest

from backend.db import crud
from backend.db.models import MessageParticipant
from tests import factories as f


async def _participate(db, msg, ident, role):
    db.add(MessageParticipant(message_id=msg.id, identity_id=ident.id, role=role))
    await db.commit()


async def _world(db):
    """Alice on Telegram + Gmail (one person), plus Bob as unrelated noise."""
    alice = await f.identity(db, "77", display_name="Alice")
    alice_mail = await f.identity(db, "alice@example.com", channel="gmail", display_name="Alice")
    alice_mail.person_id = alice.person_id  # the same human on both channels
    await db.commit()
    bob = await f.identity(db, "88", display_name="Bob")

    dm = await f.conversation(db, "77", title="Alice", peer=alice)
    group = await f.conversation(db, "-100", title="Team", kind="group")
    bob_dm = await f.conversation(db, "88", title="Bob", peer=bob)
    thread = await f.conversation(db, "t1", title="Quarterly plan", kind="email_thread", channel="gmail")

    m = {}
    m["dm_in"] = await f.message(db, dm, 1, "hi from alice", sender=alice)
    m["dm_out"] = await f.message(db, dm, 2, "hi alice", out=True)
    m["group_alice"] = await f.message(db, group, 3, "alice in group", sender=alice)
    m["group_bob"] = await f.message(db, group, 4, "bob in group", sender=bob)
    m["bob_dm"] = await f.message(db, bob_dm, 5, "bob only", sender=bob)
    m["mail_to_alice"] = await f.message(db, thread, 6, "body", out=True, subject="Plan")
    await _participate(db, m["mail_to_alice"], alice_mail, "to")
    await _participate(db, m["mail_to_alice"], alice_mail, "cc")  # two roles: still one item
    m["mail_unrelated"] = await f.message(db, thread, 7, "fwd", out=True, subject="Plan")
    return alice, m


def _ids(body):
    return [it["message"]["id"] for it in body["items"]]


@pytest.mark.asyncio
async def test_timeline_collects_direct_group_and_email(db, api):
    alice, m = await _world(db)
    resp = await api.get(f"/api/people/{alice.person_id}/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_more"] is False
    # Newest first; both DM directions, only Alice's group message, the email
    # she's addressed on (once), nothing of Bob's and not the email without her
    assert _ids(body) == [m["mail_to_alice"].id, m["group_alice"].id, m["dm_out"].id, m["dm_in"].id]

    email = body["items"][0]
    assert email["conversation"]["kind"] == "email_thread"
    assert email["conversation"]["channel"] == "gmail"
    assert email["conversation"]["title"] == "Quarterly plan"
    assert email["message"]["subject"] == "Plan"
    assert {p["role"] for p in email["message"]["participants"]} == {"to", "cc"}
    group = body["items"][1]
    assert group["conversation"]["kind"] == "group" and group["conversation"]["channel"] == "telegram"
    assert group["message"]["text"] == "alice in group"


@pytest.mark.asyncio
async def test_timeline_email_sender_counts_once(db, api):
    alice, _ = await _world(db)
    alice_mail = (await crud.identities_for_person(db, alice.person_id))[1]
    thread = await f.conversation(db, "t2", title="Re: intro", kind="email_thread", channel="gmail")
    # Sent by Alice and recorded as a 'from' participant: matches two ways, listed once
    msg = await f.message(db, thread, 50, "intro", sender=alice_mail, subject="Intro")
    await _participate(db, msg, alice_mail, "from")
    ids = _ids((await api.get(f"/api/people/{alice.person_id}/timeline")).json())
    assert ids.count(msg.id) == 1 and ids[0] == msg.id


@pytest.mark.asyncio
async def test_timeline_paging_with_before(db, api):
    alice, m = await _world(db)
    url = f"/api/people/{alice.person_id}/timeline"
    first = (await api.get(url, params={"limit": 2})).json()
    assert _ids(first) == [m["mail_to_alice"].id, m["group_alice"].id]
    assert first["has_more"] is True

    last = first["items"][-1]["message"]
    second = (await api.get(url, params={"limit": 2, "before": last["date"]})).json()
    assert _ids(second) == [m["dm_out"].id, m["dm_in"].id]
    assert second["has_more"] is False


@pytest.mark.asyncio
async def test_timeline_paging_breaks_date_ties_with_before_id(db, api):
    alice = await f.identity(db, "77", display_name="Alice")
    dm = await f.conversation(db, "77", title="Alice", peer=alice)
    same = f.BASE_DATE + timedelta(hours=1)
    a = await f.message(db, dm, 1, "a", sender=alice, date=same)
    b = await f.message(db, dm, 2, "b", sender=alice, date=same)
    url = f"/api/people/{alice.person_id}/timeline"
    first = (await api.get(url, params={"limit": 1})).json()
    assert _ids(first) == [b.id] and first["has_more"] is True
    oldest = first["items"][-1]["message"]
    second = (await api.get(url, params={"limit": 1, "before": oldest["date"], "before_id": oldest["id"]})).json()
    assert _ids(second) == [a.id] and second["has_more"] is False


@pytest.mark.asyncio
async def test_timeline_unknown_person_and_bad_date(db, api):
    alice = await f.identity(db, "77", display_name="Alice")
    assert (await api.get("/api/people/9999/timeline")).status_code == 404
    assert (await api.get(f"/api/people/{alice.person_id}/timeline", params={"before": "nope"})).status_code == 400
