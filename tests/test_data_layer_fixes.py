"""Data-layer review fixes: backups, id reuse, delete previews, suggestion
bookkeeping on merge / detach, "None = keep" ingest semantics, account
removal clean-up, single-account races, layering and identity memoization."""

import asyncio
import ast
import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from backend.db import crud
from backend.db.database import AsyncSessionLocal
from backend.db.models import (
    Account, ContactPoint, Conversation, Identity, Message, MessageAttachment, MessageParticipant, Person,
    PersonMerge, PersonNote,
)
from backend.ingest.normalized import (
    NormalizedAttachment, NormalizedConversation, NormalizedIdentity, NormalizedMessage,
)
from backend.people import linking
from tests import factories as f
from backend.people import identities as person_identities


@pytest.fixture
def sent(monkeypatch):
    events = []

    async def _capture(data):
        events.append(data)

    from backend.ws.manager import ws_manager
    monkeypatch.setattr(ws_manager, "broadcast", _capture)
    return events


async def _count(db, model) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar()


# ── 1. Backups ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_retired_database_backup_survives_migration_backup_rotation(tmp_path, monkeypatch):
    from backend.db import database, startup
    from backend.db.migrations import runner

    path = tmp_path / "mingly.db"
    with sqlite3.connect(str(path)) as old:
        old.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, telegram_id INTEGER)")
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine)
    await startup.init_db()
    [retired] = list((tmp_path / "backups").glob("mingly-retired-gen1-*.db"))
    os.utime(retired, (time.time() - 3600, time.time() - 3600))  # oldest backup on disk
    async with engine.connect() as conn:
        for version in range(10, 10 + runner.BACKUPS_KEPT + 2):
            dest = await runner._backup_before(conn, version)
            stamp = time.time() - 1000 + version  # older than the next backup, newer than the retired one
            os.utime(dest, (stamp, stamp))
    await engine.dispose()
    assert retired.exists()
    assert len(list((tmp_path / "backups").glob("mingly-pre-v*.db"))) == runner.BACKUPS_KEPT


@pytest.mark.asyncio
async def test_newer_schema_generation_refuses_to_start_and_keeps_the_file(tmp_path, monkeypatch):
    from backend.db import database, startup

    path = tmp_path / "mingly.db"
    with sqlite3.connect(str(path)) as newer:
        newer.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT)")
        newer.execute("INSERT INTO schema_meta VALUES ('schema_generation', '3')")
        newer.execute("CREATE TABLE precious (x)")
    before = path.read_bytes()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    monkeypatch.setattr(database, "engine", engine)
    with pytest.raises(startup.NewerSchemaError):
        await startup.init_db()
    await engine.dispose()
    assert path.read_bytes() == before
    assert not (tmp_path / "backups").exists()


# ── 2. Ids are never reused ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ids_are_not_reused_after_merge_or_delete(db):
    tg = await f.identity(db, "5", display_name="Ann", phone="15551234567")
    await person_identities.upsert_identity(db, channel="signal", external_id="aci-1", display_name="Ann S", phone="+15551234567")
    gone = (await db.execute(select(PersonMerge))).scalar_one().from_person_id
    assert gone != tg.person_id
    bob = await f.identity(db, "9", display_name="Bob")
    assert bob.person_id > gone

    conv = await f.conversation(db, "77")
    await f.message(db, conv, 1, "keep")
    cited = await f.message(db, conv, 2, "cited, then deleted")
    citation = [{"conversation_id": conv.id, "message_id": cited.id}]
    await crud.delete_messages(db, [cited.id])
    newer = await f.message(db, conv, 3, "unrelated")
    assert newer.id > cited.id
    assert await crud.missing_citations(db, citation) == citation


async def _legacy_schema_db(path: Path):
    """A database at the schema before migrations 2-4: tables without
    AUTOINCREMENT, no unclaimed-account index, no dismissal status columns;
    migration 1 applied."""
    from backend.db import models
    from backend.db.database import Base
    from backend.db.migrations import runner

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    from backend.db import database, startup
    from sqlalchemy import event
    event.listen(engine.sync_engine, "connect", database._set_sqlite_pragmas)
    tables = [Base.metadata.tables[n] for n in models.AUTOINCREMENT_TABLES]
    for t in tables:
        t.dialect_options["sqlite"]["autoincrement"] = False
    try:
        async with engine.connect() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.exec_driver_sql("DROP INDEX uq_account_channel_unclaimed")
            await conn.exec_driver_sql("ALTER TABLE scam_dismissals DROP COLUMN cloud_status")
            await conn.exec_driver_sql("ALTER TABLE scam_dismissals DROP COLUMN cloud_skipped_reason")
            await conn.commit()
    finally:
        for t in tables:
            t.dialect_options["sqlite"]["autoincrement"] = True
    async with engine.connect() as conn:
        await runner._ensure_migration_table(conn)
        await runner._record_migration(conn, 0, "Seed default tags")
        await runner._record_migration(conn, 1, "v1")
        await conn.commit()
    return engine


def _schema(path: Path) -> dict:
    """Columns, indexes and foreign keys of every table (names normalised)."""
    out = {}
    with sqlite3.connect(str(path)) as c:
        tables = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "AND name NOT IN ('schema_migrations', 'schema_meta')"
        )]
        for t in tables:
            cols = sorted(tuple(r[1:]) for r in c.execute(f'PRAGMA table_info("{t}")'))  # ALTER appends
            idx = sorted(
                (r[1], r[2], tuple(x[2] for x in c.execute(f'PRAGMA index_info("{r[1]}")')))
                for r in c.execute(f'PRAGMA index_list("{t}")')
            )
            fks = sorted(tuple(r[2:]) for r in c.execute(f'PRAGMA foreign_key_list("{t}")'))
            out[t] = (cols, idx, fks)
    return out


@pytest.mark.asyncio
async def test_migration_2_rebuilds_tables_as_autoincrement_keeping_rows_indexes_and_fks(tmp_path):
    from backend.db.database import Base
    from backend.db.migrations import runner
    from backend.db import models

    legacy = tmp_path / "legacy.db"
    engine = await _legacy_schema_db(legacy)
    async with AsyncSession(engine, expire_on_commit=False) as s:
        acc = Account(channel="telegram")
        s.add(acc)
        await s.flush()
        people = [Person(display_name=f"P{i}") for i in range(4)]
        s.add_all(people)
        await s.flush()
        idents = [Identity(person_id=p.id, channel="telegram", external_id=str(p.id)) for p in people]
        s.add_all(idents)
        await s.flush()
        conv = Conversation(account_id=acc.id, external_id="77", kind="direct", title="Ann",
                            peer_identity_id=idents[0].id)
        s.add(conv)
        await s.flush()
        msgs = [Message(conversation_id=conv.id, external_id=str(i), seq=i, date=datetime(2026, 1, 1, 0, i),
                        sender_identity_id=idents[1].id, text=f"m{i}") for i in range(5)]
        s.add_all(msgs)
        await s.flush()
        s.add(MessageAttachment(message_id=msgs[0].id, position=0, kind="photo"))
        s.add(ContactPoint(identity_id=idents[0].id, kind="phone", value="+15551234567"))
        s.add(PersonNote(person_id=people[0].id, kind="context", title="t", body=""))
        conv.pinned_message_id = msgs[2].id
        await s.commit()
        # The newest message and person are gone: a reused id would be max+1 again
        await s.delete(msgs[-1])
        await s.delete(people[-1])
        await s.commit()
        max_msg, max_person = msgs[-1].id, people[-1].id

    def dump(path):
        with sqlite3.connect(str(path)) as c:
            return {t: c.execute(f'SELECT * FROM "{t}" ORDER BY 1').fetchall()
                    for t in ("accounts", "people", "identities", "conversations", "messages",
                              "message_attachments", "contact_points", "person_notes")}

    before = dump(legacy)
    async with engine.connect() as conn:
        await runner.run_migrations(conn, fresh=False)
    async with engine.connect() as conn:
        assert (await conn.exec_driver_sql("PRAGMA foreign_keys")).scalar() == 1
    await engine.dispose()

    assert dump(legacy) == before  # every row kept with its id; no cascade fired
    with sqlite3.connect(str(legacy)) as c:
        for t in models.AUTOINCREMENT_TABLES:
            sql = c.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0]
            assert "AUTOINCREMENT" in sql, t
        seq = dict(c.execute("SELECT name, seq FROM sqlite_sequence"))
        assert seq["messages"] == max_msg - 1 and seq["people"] == max_person - 1
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert not [r for r in c.execute("SELECT name, sql FROM sqlite_master") if "__rebuild" in (r[0] + (r[1] or ""))]

    # Same tables, columns, indexes and foreign keys as a fresh database
    fresh = tmp_path / "fresh.db"
    feng = create_async_engine(f"sqlite+aiosqlite:///{fresh}")
    async with feng.connect() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()
        await runner.run_migrations(conn, fresh=True)
    await feng.dispose()
    assert _schema(legacy) == _schema(fresh)

    # From now on a deleted id is never handed out again
    with sqlite3.connect(str(legacy)) as c:
        c.execute("PRAGMA foreign_keys=ON")
        top = c.execute("SELECT MAX(id) FROM people").fetchone()[0]
        c.execute("DELETE FROM person_notes WHERE person_id = ?", (top,))
        c.execute("DELETE FROM people WHERE id = ?", (top,))
        cur = c.execute("INSERT INTO people (is_important, is_self, relationship_status_manual, created_at, "
                        "updated_at) VALUES (0, 0, 0, '2026-01-01', '2026-01-01')")
        assert cur.lastrowid == top + 1


@pytest.mark.asyncio
async def test_migration_2_is_a_no_op_on_a_fresh_database(db_engine):
    async with db_engine.connect() as conn:
        rows = (await conn.exec_driver_sql("SELECT version FROM schema_migrations ORDER BY version")).scalars().all()
        sql = (await conn.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = 'messages'")).scalar()
    assert list(rows) == [0, 1, 2, 3, 4] and "AUTOINCREMENT" in sql


# ── 3. Deleting messages re-derives the preview ──────────────────────────────

@pytest.mark.asyncio
async def test_deleting_the_newest_message_restores_the_previous_preview(db):
    conv = await f.conversation(db)
    older = await f.message(db, conv, 1, "older")
    newest = await f.message(db, conv, 2, "SECRET deleted for everyone")
    assert await crud.delete_messages(db, [newest.id]) == {conv.id: [newest.id]}
    c = await crud.get_conversation(db, conv.id)
    await db.refresh(c)
    assert c.last_message_text == "older" and c.last_message_at == older.date
    assert c.last_activity_at == older.date

    await crud.delete_messages(db, [older.id])
    await db.refresh(c)
    assert c.last_message_text is None and c.last_message_at is None and c.last_activity_at is None


@pytest.mark.asyncio
async def test_deleting_an_old_message_keeps_activity_known_from_elsewhere(db):
    conv = await f.conversation(db)
    old = await f.message(db, conv, 1, "old")
    await f.message(db, conv, 2, "newest")
    dialog_date = f.BASE_DATE + timedelta(days=1)  # the channel's dialog list knows a later activity
    conv.last_activity_at = dialog_date
    await db.commit()
    await crud.delete_messages(db, [old.id])
    await db.refresh(conv)
    assert conv.last_message_text == "newest" and conv.last_activity_at == dialog_date


@pytest.mark.asyncio
async def test_channel_delete_recomputes_preview_and_broadcasts(db_engine, sent):
    from backend.ingest import service

    async with AsyncSessionLocal() as s:
        acc = Account(channel="signal", external_id="+1")
        s.add(acc)
        await s.commit()
        account_id = acc.id
    conv = NormalizedConversation("c1", "direct", title="Bob", peer=NormalizedIdentity("aci-b"))
    d = datetime(2026, 1, 1)
    await service.ingest(account_id, "signal", conv, [
        NormalizedMessage("m1", d, text="first"), NormalizedMessage("m2", d + timedelta(minutes=1), text="gone"),
    ])
    await service.delete_messages(account_id, "c1", ["m2"])
    async with AsyncSessionLocal() as s:
        stored = (await s.execute(select(Conversation))).scalar_one()
        assert stored.last_message_text == "first"
    [event] = [e for e in sent if e["type"] == "messages_deleted"]
    assert event["conversation_id"] == stored.id and len(event["message_ids"]) == 1


# ── 4. Suggestions on merge / detach ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_detach_marker_survives_a_later_merge(db):
    tg = await f.identity(db, "5", display_name="Ann", phone="15551234567")
    sig = await person_identities.upsert_identity(db, channel="signal", external_id="aci-1", display_name="Ann S",
                                     phone="+15551234567")
    a = tg.person_id
    d = await linking.detach(db, sig.id)
    await db.commit()
    gmail = await person_identities.upsert_identity(db, channel="gmail", external_id="ann@x.com", display_name="Ann G")
    g = gmail.person_id
    await linking.merge(db, g, a)
    await linking.suggest_for_person(db, g)  # what POST /people/{id}/merge does next
    await db.commit()
    pair = await crud.merge_suggestion_for_pair(db, g, d.id)
    assert pair is not None and pair.status == "dismissed"


@pytest.mark.asyncio
async def test_merge_carries_suggestions_and_drops_the_merged_pair(db):
    x = await f.identity(db, "1", display_name="X")
    y = await f.identity(db, "2", display_name="Y")
    z = await f.identity(db, "3", display_name="Z")
    w = await f.identity(db, "4", display_name="W")
    await crud.add_merge_suggestion(db, y.person_id, z.person_id, linking.SAME_NAME_SHARED_GROUP, 0.6)
    await crud.add_merge_suggestion(db, y.person_id, w.person_id, linking.DETACHED, 0.0, status="dismissed")
    await crud.add_merge_suggestion(db, x.person_id, w.person_id, linking.USERNAME_MATCHES_EMAIL, 0.5)
    await crud.add_merge_suggestion(db, x.person_id, y.person_id, linking.USERNAME_MATCHES_EMAIL, 0.5)
    await db.commit()
    await linking.merge(db, x.person_id, y.person_id)
    await db.commit()
    assert (await crud.merge_suggestion_for_pair(db, x.person_id, z.person_id)).status == "pending"
    assert (await crud.merge_suggestion_for_pair(db, x.person_id, w.person_id)).status == "dismissed"
    assert await crud.merge_suggestion_for_pair(db, x.person_id, y.person_id) is None


@pytest.mark.asyncio
async def test_detach_recomputes_suggestions_for_both_people(db):
    t1 = await f.identity(db, "1", display_name="Ann", username="annz")
    t2 = await f.identity(db, "2", display_name="Ann work")
    await linking.merge(db, t1.person_id, t2.person_id)
    await db.commit()
    a = t1.person_id
    x = await person_identities.upsert_identity(db, channel="gmail", external_id="annz@x.com", display_name="Z")
    assert (await crud.merge_suggestion_for_pair(db, a, x.person_id)).status == "pending"
    d = await linking.detach(db, t1.id)
    await db.commit()
    assert await crud.merge_suggestion_for_pair(db, a, x.person_id) is None
    assert (await crud.merge_suggestion_for_pair(db, d.id, x.person_id)).status == "pending"


# ── 5. Normalized ingest: None keeps the stored value ────────────────────────

async def _signal_account() -> int:
    async with AsyncSessionLocal() as s:
        acc = Account(channel="signal", external_id="+1")
        s.add(acc)
        await s.commit()
        return acc.id


@pytest.mark.asyncio
async def test_reingest_without_optional_fields_keeps_stored_values(db_engine, sent):
    from backend.ingest import service

    account_id = await _signal_account()
    conv = NormalizedConversation("c1", "direct", title="Bob", peer=NormalizedIdentity("aci-b"))
    d = datetime(2026, 1, 1)
    await service.ingest(account_id, "signal", conv, [NormalizedMessage(
        "m1", d, sender=NormalizedIdentity("aci-b"), text="hi", subject="s", reply_to_external_id="m0",
        attachments=[NormalizedAttachment("photo", "a.jpg")], reactions_json='{"x": 1}', meta={"k": 1},
        participants=[(NormalizedIdentity("aci-c"), "to")],
    )])
    # An edit that only knows the new text; the conversation title unknown too
    edit = NormalizedMessage("m1", d, sender=NormalizedIdentity("aci-b"), text="hi (edited)", is_edited=True)
    res = await service.ingest(account_id, "signal", NormalizedConversation("c1", "direct"), [edit])
    assert res.updated
    async with AsyncSessionLocal() as s:
        m = (await s.execute(select(Message))).scalar_one()
        assert (m.text, m.subject, m.reply_to_external_id, m.reactions_json, m.meta_json) == (
            "hi (edited)", "s", "m0", '{"x": 1}', '{"k": 1}')
        assert len((await s.execute(select(MessageAttachment))).scalars().all()) == 1
        roles = sorted(r for r in (await s.execute(select(MessageParticipant.role))).scalars())
        assert roles == ["from", "to"]
        c = (await s.execute(select(Conversation))).scalar_one()
        assert c.title == "Bob" and c.peer_identity_id is not None

    # Explicit empty values clear
    clear = NormalizedMessage("m1", d, text="hi (edited)", attachments=[], meta={}, participants=[])
    await service.ingest(account_id, "signal", NormalizedConversation("c1", "direct", clear_peer=True), [clear])
    async with AsyncSessionLocal() as s:
        m = (await s.execute(select(Message))).scalar_one()
        assert m.meta_json is None
        assert (await s.execute(select(MessageAttachment))).scalars().all() == []
        assert list((await s.execute(select(MessageParticipant.role))).scalars()) == ["from"]  # sender unknown: kept
        assert (await s.execute(select(Conversation))).scalar_one().peer_identity_id is None


# ── 6. Account removal clean-up ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orphans_are_collected_after_account_removal(db_engine, sent):
    from backend import prefs
    from backend.ingest import service

    async with AsyncSessionLocal() as s:
        a1 = Account(channel="gmail", external_id="me@x.com")
        a2 = Account(channel="gmail", external_id="other@x.com")
        s.add_all([a1, a2])
        await s.commit()
        a1_id, a2_id = a1.id, a2.id
    prefs.save_prefs({"calendar_ids": {str(a1_id): ["primary"], str(a2_id): ["work"]}})
    d = datetime(2026, 1, 1)
    await service.ingest(a1_id, "gmail", NormalizedConversation("t1", "email_thread", title="hi"), [
        NormalizedMessage(f"m{k}", d + timedelta(minutes=k), sender=NormalizedIdentity(f"news{k}@shop.com"))
        for k in range(3)
    ] + [NormalizedMessage("m9", d, sender=NormalizedIdentity("shared@x.com"))])
    await service.ingest(a2_id, "gmail", NormalizedConversation("t2", "email_thread", title="yo"), [
        NormalizedMessage("n1", d, sender=NormalizedIdentity("shared@x.com")),
    ])
    async with AsyncSessionLocal() as s:
        keep = await crud.get_identity_by_external(s, "gmail", "news0@shop.com")
        keeper = await s.get(Person, keep.person_id)
        keeper.is_important = True  # user data keeps the person
        await s.delete(await s.get(Account, a1_id))
        await s.commit()
        counts = await crud.collect_orphans_after_account_removal(s, "gmail", a1_id)
        assert counts == {"identities": 3, "people": 2, "prefs": ["calendar_ids"]}
        left = {i.external_id for i in (await s.execute(select(Identity))).scalars()}
        assert left == {"shared@x.com"}
        assert await s.get(Person, keep.person_id) is not None
        assert await _count(s, ContactPoint) == 1
    assert prefs.get_pref("calendar_ids") == {str(a2_id): ["work"]}


# ── 7. Single-account channels ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_first_use_creates_one_channel_account(db_engine):
    async def one():
        async with AsyncSessionLocal() as s:
            acc = await crud.get_or_create_channel_account(s, "telegram")
            await asyncio.sleep(0)
            await s.commit()
            return acc.id

    for _ in range(5):
        ids = await asyncio.gather(*[one() for _ in range(8)])
        assert len(set(ids)) == 1
    async with AsyncSessionLocal() as s:
        assert len(await crud.get_accounts(s, "telegram")) == 1


@pytest.mark.asyncio
async def test_get_or_create_channel_account_does_not_commit_the_callers_work(db_engine):
    async with AsyncSessionLocal() as s:
        s.add(Person(display_name="uncommitted"))
        await crud.get_or_create_channel_account(s, "sms")
        await s.rollback()
    async with AsyncSessionLocal() as s:
        assert await _count(s, Person) == 0 and await _count(s, Account) == 0


@pytest.mark.asyncio
async def test_migration_3_keeps_data_of_duplicate_unclaimed_accounts(tmp_path):
    from backend.db.migrations import runner

    engine = await _legacy_schema_db(tmp_path / "legacy.db")
    async with engine.connect() as c:
        for _ in range(3):
            await c.exec_driver_sql("INSERT INTO accounts (channel, created_at) VALUES ('telegram', '2026-01-01')")
        await c.exec_driver_sql(
            "INSERT INTO conversations (account_id, external_id, kind, is_monitored, is_hidden, is_briefing, "
            "is_tracked, is_pinned, is_archived, is_muted, is_marked_unread, unread_count, is_forum, "
            "slow_mode_seconds, is_deactivated, is_self, is_bot, is_deleted_account, created_at) "
            "VALUES (3, 'x', 'direct', 0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'2026-01-01')"
        )
        await c.commit()
        await runner.run_migrations(c, fresh=False)
        rows = (await c.exec_driver_sql("SELECT id, external_id FROM accounts ORDER BY id")).all()
    await engine.dispose()
    assert [tuple(r) for r in rows] == [(1, None), (3, "unclaimed-duplicate-3")]


# ── D. Shield dismissal status columns ──────────────────────────────────────

@pytest.mark.asyncio
async def test_migration_4_moves_the_skip_marker_into_columns(tmp_path):
    import json

    from backend.db.migrations import runner

    engine = await _legacy_schema_db(tmp_path / "legacy.db")
    rows = [
        ("scan", json.dumps({"a": 1}), "2026-01-02"),
        ("scan", json.dumps({"a": 2, "cloud_skipped": {"reason": "source rows deleted", "at": "x"}}), "2026-01-02"),
        ("scammer", json.dumps({"payload": "raw", "cloud_skipped": {"reason": "gone"}}), "2026-01-02"),
        ("scan", json.dumps({"a": 3}), None),
    ]
    async with engine.connect() as c:
        for kind, payload, synced in rows:
            await c.exec_driver_sql(
                "INSERT INTO scam_dismissals (dismissal_type, payload_json, synced_at, created_at) "
                "VALUES (?, ?, ?, '2026-01-01')", (kind, payload, synced))
        await c.commit()
        await runner.run_migrations(c, fresh=False)
        out = (await c.exec_driver_sql(
            "SELECT cloud_status, cloud_skipped_reason, payload_json FROM scam_dismissals ORDER BY id")).all()
    await engine.dispose()
    assert [tuple(r) for r in out] == [
        ("synced", None, json.dumps({"a": 1})),
        ("skipped", "source rows deleted", json.dumps({"a": 2})),
        ("skipped", "gone", "raw"),
        ("pending", None, json.dumps({"a": 3})),
    ]


# ── 9. Identity resolution is memoized per ingest ────────────────────────────

@pytest.mark.asyncio
async def test_ingest_upserts_each_identity_once(db_engine, sent, monkeypatch):
    from backend.ingest import service

    account_id = await _signal_account()
    calls = []
    real = person_identities.upsert_identity

    async def counting(db, **kw):
        calls.append(kw["external_id"])
        return await real(db, **kw)

    monkeypatch.setattr(person_identities, "upsert_identity", counting)
    bob = NormalizedIdentity("aci-b", display_name="Bob")
    msgs = [NormalizedMessage(f"m{k}", datetime(2026, 1, 1) + timedelta(minutes=k), sender=bob, text="x",
                              participants=[(NormalizedIdentity("aci-me"), "to")]) for k in range(20)]
    await service.ingest(account_id, "signal", NormalizedConversation("c1", "direct", title="Bob", peer=bob), msgs)
    assert sorted(calls) == ["aci-b", "aci-me"]


# ── B / C. Live events carry what stored reads carry ─────────────────────────

@pytest.mark.asyncio
async def test_live_events_carry_reply_preview_and_channel(db_engine, sent, monkeypatch):
    from backend.ingest import fanout, service

    monkeypatch.setattr(fanout, "send_native_notification", lambda *a, **k: None)

    async def _no_scan(*a, **k):
        return None

    monkeypatch.setattr(fanout, "inspect_messages", _no_scan)
    account_id = await _signal_account()
    conv = NormalizedConversation("c1", "direct", title="Bob", peer=NormalizedIdentity("aci-b"))
    d = datetime(2026, 1, 1)
    first = await service.ingest(account_id, "signal", conv, [NormalizedMessage("m1", d, text="original")])
    await service.ingest(account_id, "signal", conv, [NormalizedMessage(
        "m2", d + timedelta(minutes=1), text="reply", reply_to_external_id="m1", sender=NormalizedIdentity("aci-b"),
    )], source="realtime")
    [event] = [e for e in sent if e["type"] == "new_message"]
    assert event["channel"] == "signal"
    assert event["message"]["reply_to_message_id"] == first.created[0]
    assert event["message"]["reply_to_preview"]["text"] == "original"
    assert [p["role"] for p in event["message"]["participants"]] == ["from"]

    async with AsyncSessionLocal() as s:
        reply = (await s.execute(select(Message).where(Message.external_id == "m2"))).scalar_one()
    await fanout.message_edited(reply.conversation_id, reply, text_changed=False)
    edited = [e for e in sent if e["type"] == "message_edited"][-1]
    assert edited["message"]["reply_to_preview"]["text"] == "original"


# ── E. Telegram messages posted as a channel / chat keep the peer ────────────

@pytest.mark.asyncio
async def test_message_posted_as_a_channel_keeps_its_peer(db):
    from datetime import timezone

    from telethon.tl import types

    from backend.telegram import ids as tg_ids
    from backend.telegram.convert import rows_for

    conv = await f.conversation(db, "-1001", title="Group", kind="group")
    when = datetime(2026, 1, 1, tzinfo=timezone.utc)
    as_channel = types.Message(id=1, peer_id=types.PeerChannel(1001), from_id=types.PeerChannel(777),
                               date=when, message="from a channel")
    as_chat = types.Message(id=2, peer_id=types.PeerChannel(1001), from_id=types.PeerChat(55),
                            date=when, message="from a chat")
    user = types.User(id=900, first_name="U", access_hash=1)
    as_user = types.Message(id=3, peer_id=types.PeerChannel(1001), from_id=types.PeerUser(900),
                            date=when, message="from a user")
    as_user._sender, as_user._sender_id = user, user.id
    await crud.save_messages(db, await rows_for(db, conv.id, [as_channel, as_chat, as_user], forum=False))
    stored = {m.external_id: m for m in (await db.execute(select(Message))).scalars()}
    assert stored["1"].sender_identity_id is None and tg_ids.sender_peer_of(stored["1"]) == -1000000000777
    assert tg_ids.sender_peer_of(stored["2"]) == -55
    assert stored["3"].sender_identity_id is not None and tg_ids.sender_peer_of(stored["3"]) is None
