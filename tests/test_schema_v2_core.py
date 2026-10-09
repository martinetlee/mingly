"""Schema v2 core: identities/people, conversations, message upsert with
attachments, keyset windows, reply previews, database generation handling."""

import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import select

from backend.db import crud
from backend.db.models import ContactPoint, Message, MessageAttachment, Person
from backend.serializers import conversation_out, message_out
from tests import factories as f


@pytest.mark.asyncio
async def test_identity_upsert_creates_person_and_keeps_unknown_fields(db):
    a = await crud.upsert_identity(db, channel="telegram", external_id="5", display_name="Ann", phone="15551234567")
    b = await crud.upsert_identity(db, channel="telegram", external_id="5", username="ann", phone=None)
    assert a.id == b.id and b.display_name == "Ann" and b.username == "ann" and b.phone == "15551234567"
    person = await crud.get_person(db, a.person_id)
    assert person.name == "Ann" and person.is_contact is False
    points = (await db.execute(select(ContactPoint.kind, ContactPoint.value))).all()
    assert points == [("phone", "+15551234567")]
    await crud.upsert_identity(db, channel="telegram", external_id="5", is_contact=True, phone="")
    await db.refresh(person)
    assert person.is_contact is True
    assert (await db.execute(select(ContactPoint))).scalars().all() == []  # phone cleared
    assert (await db.execute(select(Person))).scalars().all() == [person]


@pytest.mark.asyncio
async def test_self_identity_marks_person(db):
    me = await crud.upsert_identity(db, channel="telegram", external_id="1", display_name="Me", is_self=True)
    assert (await crud.self_person(db)).id == me.person_id


@pytest.mark.asyncio
async def test_conversation_upsert_is_keyed_by_account_and_external_id(db):
    c1 = await f.conversation(db, "77", title="Alice")
    c2 = await f.conversation(db, "77", title="Alice B")
    other = await f.conversation(db, "77", title="Alice mail", channel="gmail", kind="email_thread")
    assert c1.id == c2.id != other.id
    out = conversation_out(c2)
    assert out["title"] == "Alice B" and out["channel"] == "telegram" and out["kind"] == "direct"
    assert out["peer"]["identity_id"] == c2.peer_identity_id
    assert "send" in out["capabilities"] and "forum_topics" not in out["capabilities"]
    assert out["history_complete"] is False


@pytest.mark.asyncio
async def test_last_activity_never_moves_backwards(db):
    acc = await f.account(db)
    newer = f.BASE_DATE + timedelta(days=1)
    await crud.upsert_conversation(db, account_id=acc.id, external_id="9", title="G", kind="group", last_message_at=newer)
    conv = await crud.upsert_conversation(
        db, account_id=acc.id, external_id="9", title="G", kind="group", last_message_at=f.BASE_DATE,
    )
    assert conv.last_activity_at == newer


@pytest.mark.asyncio
async def test_save_messages_reports_created_updated_and_syncs_attachments(db):
    conv = await f.conversation(db)
    m = await f.message(db, conv, 1, "hello", attachments=[f.photo()])
    assert m._change == "created" and [a.kind for a in m.attachments] == ["photo"]
    att = m.attachments[0]
    await crud.set_attachment_local_path(db, att.id, "media/1/1.jpg")

    same = await f.message(db, conv, 1, "hello", attachments=[f.photo()])
    assert same._change is None
    await db.refresh(same)
    assert same.attachments[0].local_path == "media/1/1.jpg"  # kept once downloaded

    edited = await f.message(db, conv, 1, "hello!", attachments=[])
    assert edited._change == "updated"
    assert (await db.execute(select(MessageAttachment))).scalars().all() == []

    res = await crud.save_messages(db, [
        dict(conversation_id=conv.id, external_id="2", seq=2, text="a", date=f.BASE_DATE),
        dict(conversation_id=conv.id, external_id="2", seq=2, text="b", date=f.BASE_DATE),
    ])
    assert len(res["created"]) == 1 and res["updated"] == res["created"]


@pytest.mark.asyncio
async def test_local_columns_survive_upsert_and_delivery_only_moves_forward(db):
    conv = await f.conversation(db)
    m = await f.message(db, conv, 1, delivery_state="read")
    m.scam_score, m.my_reaction = 0.9, "👍"
    await db.commit()
    again = await f.message(db, conv, 1, delivery_state="sent")
    assert again.scam_score == 0.9 and again.my_reaction == "👍" and again.delivery_state == "read"


@pytest.mark.asyncio
async def test_inbox_preview_tracks_newest_message(db):
    conv = await f.conversation(db)
    await f.message(db, conv, 5, "newest")
    await f.message(db, conv, 3, "older")
    await db.refresh(conv)
    assert conv.last_message_text == "newest"
    await f.message(db, conv, 5, "newest, edited")
    await db.refresh(conv)
    assert conv.last_message_text == "newest, edited"


@pytest.mark.asyncio
async def test_keyset_window_paging_by_message_id(db):
    conv = await f.conversation(db)
    ids = [(await f.message(db, conv, s, f"m{s}")).id for s in range(1, 11)]
    newest = await crud.get_message_window(db, conv.id, limit=4)
    assert [r[0].seq for r in newest["rows"]] == [7, 8, 9, 10]
    assert newest["has_older"] and not newest["has_newer"]
    older = await crud.get_message_window(db, conv.id, limit=4, before=newest["rows"][0][0].id)
    assert [r[0].seq for r in older["rows"]] == [3, 4, 5, 6]
    around = await crud.get_message_window(db, conv.id, limit=4, anchor=ids[4])
    assert [r[0].seq for r in around["rows"]] == [3, 4, 5, 6]
    other = await f.conversation(db, "88", title="Bob")
    foreign = await crud.get_message_window(db, other.id, anchor=ids[0])
    assert foreign["rows"] == [] and foreign["anchor_found"] is False


@pytest.mark.asyncio
async def test_reply_preview_and_serialized_message(db):
    conv = await f.conversation(db)
    ann = await f.identity(db, "77", display_name="Ann")
    await f.message(db, conv, 1, "original", sender=ann, attachments=[f.photo()])
    reply = await f.message(db, conv, 2, "answer", reply_to_external_id="1")
    orphan = await f.message(db, conv, 3, "to nothing", reply_to_external_id="999")
    rows = {r[0].id: r for r in await crud.get_reply_previews(db, [reply.id, orphan.id])}
    out = message_out(rows[reply.id])
    assert out["reply_to_preview"] == {"sender_name": "Ann", "text": "original", "media_type": "photo"}
    assert out["reply_to_message_id"] is not None
    assert message_out(rows[orphan.id])["reply_to_preview"] is None
    first = message_out((await crud.get_message_window(db, conv.id))["rows"][0])
    assert first["sender"]["person_id"] == ann.person_id and first["attachments"][0]["kind"] == "photo"
    assert first["attachments"][0]["url"] is None


@pytest.mark.asyncio
async def test_deleting_conversation_cascades(db):
    conv = await f.conversation(db)
    await f.message(db, conv, 1, attachments=[f.photo()])
    await db.delete(conv)
    await db.commit()
    assert (await db.execute(select(Message))).scalars().all() == []
    assert (await db.execute(select(MessageAttachment))).scalars().all() == []


@pytest.mark.asyncio
async def test_pre_v2_database_is_backed_up_and_replaced(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine
    from backend.db import database

    path = tmp_path / "mingly.db"
    with sqlite3.connect(str(path)) as old:
        old.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, telegram_id INTEGER)")
        old.execute("INSERT INTO chats (telegram_id) VALUES (77)")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine)
    await database.init_db()
    [backup] = list((tmp_path / "backups").glob("mingly-pre-v2-*.db"))
    with sqlite3.connect(str(backup)) as b:
        assert b.execute("SELECT telegram_id FROM chats").fetchone() == (77,)
    with sqlite3.connect(str(path)) as new:
        tables = {r[0] for r in new.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "chats" not in tables and "conversations" in tables
        assert new.execute("SELECT value FROM schema_meta").fetchone() == ("2",)
    # A current DB is left alone on the next start.
    await database.init_db()
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_person_conversations_span_channels(api, db):
    ann = await f.identity(db, "77", display_name="Ann")
    tg = await f.conversation(db, "77", title="Ann", peer=ann, last_activity_at=f.BASE_DATE)
    mail_ident = await f.identity(db, "ann@x.com", channel="gmail", display_name="Ann")
    mail_ident.person_id = ann.person_id  # same human (linking lands in Phase 2)
    await db.commit()
    mail = await f.conversation(db, "t1", title="Hi", kind="email_thread", channel="gmail", peer=mail_ident,
                                last_activity_at=f.BASE_DATE + timedelta(days=1))
    await f.conversation(db, "-100", title="Group", kind="group")
    r = await api.get(f"/api/people/{ann.person_id}/conversations")
    assert r.status_code == 200
    assert [(c["id"], c["channel"]) for c in r.json()] == [(mail.id, "gmail"), (tg.id, "telegram")]
    assert (await api.get("/api/people/999999/conversations")).status_code == 404


@pytest.mark.asyncio
async def test_telegram_capabilities_narrow_per_conversation(db):
    dm = conversation_out(await f.conversation(db, "77"))
    forum = conversation_out(await f.conversation(db, "-100", title="F", kind="group", is_forum=True))
    assert {"pin", "forward", "react"} <= set(dm["capabilities"])
    assert "admin_moderation" not in dm["capabilities"] and "forum_topics" not in dm["capabilities"]
    assert {"admin_moderation", "forum_topics"} <= set(forum["capabilities"])
    mail = conversation_out(await f.conversation(db, "t1", title="Hi", kind="email_thread", channel="gmail"))
    assert mail["capabilities"] == []  # no Gmail connector yet


def test_crud_package_reexports_exactly_each_modules_public_names():
    import importlib
    import pkgutil
    from backend.db import crud as pkg

    expected = set()
    for info in pkgutil.iter_modules(pkg.__path__):
        expected |= set(importlib.import_module(f"backend.db.crud.{info.name}").__all__)
    assert set(pkg.__all__) == expected
    assert all(hasattr(pkg, name) for name in expected)
