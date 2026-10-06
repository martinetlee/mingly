"""Replays migrations 90-92 against legacy states. Statements are executed
strictly here (the runner tolerates "no such column", which would hide typos)."""

import pytest
import sqlalchemy as sa

from backend.db.migrations.areas import relations


async def _run(engine, version, setup_sql=()):
    stmts = next(s for v, _d, s in relations.MIGRATIONS if v == version)
    async with engine.connect() as conn:
        await conn.exec_driver_sql("BEGIN")
        for stmt in setup_sql:
            await conn.execute(sa.text(stmt))
        for stmt in stmts:
            await conn.execute(sa.text(stmt))
        await conn.commit()


async def _rows(engine, sql):
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(sa.text(sql))).all()]


@pytest.mark.asyncio
async def test_stage_split_preserves_cards_and_frees_shared_tags(db_engine):
    await _run(db_engine, 90, [
        "DELETE FROM tags",
        # 1 = include tag "Investors"; 2 = stage "Done" shared by two workspaces; 3 = orphan stage
        "INSERT INTO tags (id, name, color, category, is_default, created_at) VALUES "
        "(1,'Investors','slate',NULL,0,'2026-01-01'), (2,'Done','slate','stage',0,'2026-01-01'),"
        "(3,'Old','slate','stage',0,'2026-01-01'), (4,'Sales','slate',NULL,0,'2026-01-01')",
        "INSERT INTO workspaces (id, name, color, match_mode, view_mode, position, created_at) VALUES "
        "(1,'A','indigo','any','kanban',0,'2026-01-01'), (2,'B','indigo','any','kanban',1,'2026-01-01')",
        "INSERT INTO workspace_tags (workspace_id, tag_id, is_exclusion) VALUES (1,1,0),(2,4,0)",
        "INSERT INTO workspace_boards (id, workspace_id, name, position) VALUES (1,1,'Main',0),(2,2,'Main',0)",
        # col 1: backed by A's own include tag (ambiguous); col 2: Done (owner);
        # col 3: B reuses Done (must split, cards copied for B members)
        "INSERT INTO workspace_columns (id, workspace_id, tag_id, board_id, label, position) VALUES "
        "(1,1,1,1,'investors',0),(2,1,2,1,'Done',1),(3,2,2,2,'Done',0)",
        "INSERT INTO chat_tags (chat_id, tag_id) VALUES (10,1),(10,2),(20,4),(20,2),(30,3)",
    ])
    cols = dict(await _rows(db_engine, "SELECT id, tag_id FROM workspace_columns"))
    assert cols[2] == 2  # lowest-id user of a pure stage tag keeps it
    assert cols[1] not in (1, 2) and cols[3] not in (1, 2) and cols[1] != cols[3]
    cats = dict(await _rows(db_engine, "SELECT id, category FROM tags"))
    assert cats[cols[1]] == cats[cols[3]] == "stage"
    assert 3 not in cats  # orphan stage tag removed
    tags = set(await _rows(db_engine, "SELECT chat_id, tag_id FROM chat_tags"))
    assert (10, 1) in tags and (20, 4) in tags  # membership untouched
    assert (10, cols[1]) not in tags  # ambiguous placement not invented
    assert (20, cols[3]) in tags  # B's card kept its place on B's board
    assert (30, 3) not in tags
    names = dict(await _rows(db_engine, "SELECT id, name FROM tags"))
    assert names[cols[3]] == f"Done · stage {cols[3]}"


@pytest.mark.asyncio
async def test_tag_dedupe_merges_case_variants_and_spares_stage_tags(db_engine):
    await _run(db_engine, 91, [
        "DROP INDEX IF EXISTS uq_tags_name_ci",
        "DELETE FROM tags",
        "INSERT INTO tags (id, name, color, category, is_default, created_at) VALUES "
        "(1,'Work','slate',NULL,1,'2026-01-01'), (2,'work ','slate',NULL,0,'2026-01-01'),"
        "(3,'WORK','slate','stage',0,'2026-01-01'), (4,'Other','slate',NULL,0,'2026-01-01')",
        "INSERT INTO workspaces (id, name, color, match_mode, view_mode, position, created_at) VALUES "
        "(1,'A','indigo','any','kanban',0,'2026-01-01')",
        "INSERT INTO workspace_tags (workspace_id, tag_id, is_exclusion) VALUES (1,2,0)",
        "INSERT INTO workspace_boards (id, workspace_id, name, scope_tag_id, position) VALUES (1,1,'Main',2,0)",
        "INSERT INTO workspace_columns (id, workspace_id, tag_id, board_id, label, position) VALUES (1,1,3,1,'Work',0)",
        "INSERT INTO chat_tags (chat_id, tag_id) VALUES (10,1),(10,2),(20,2),(30,3)",
    ])
    names = dict(await _rows(db_engine, "SELECT id, name FROM tags"))
    assert 2 not in names and names[1] == "Work"
    assert names[3] == "WORK · stage 3"  # stage tag kept, renamed out of the way
    assert set(await _rows(db_engine, "SELECT chat_id, tag_id FROM chat_tags")) == {(10, 1), (20, 1), (30, 3)}
    assert await _rows(db_engine, "SELECT tag_id FROM workspace_tags") == [(1,)]
    assert await _rows(db_engine, "SELECT scope_tag_id FROM workspace_boards") == [(1,)]
    with pytest.raises(Exception):
        await _run(db_engine, 91, ["INSERT INTO tags (name, color, is_default, created_at) VALUES (' work','x',0,'2026-01-01')"])


@pytest.mark.asyncio
async def test_org_fk_backfill_from_legacy_positions(db_engine):
    await _run(db_engine, 92, [
        "DROP INDEX IF EXISTS uq_organizations_name_ci",
        "DROP TABLE contact_positions",
        # legacy shape (migration 21)
        "CREATE TABLE contact_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, "
        "organization VARCHAR NOT NULL, title VARCHAR, is_active INTEGER NOT NULL DEFAULT 1, ended_at TEXT, "
        "created_at DATETIME NOT NULL DEFAULT (datetime('now')), UNIQUE(telegram_id, organization))",
        "INSERT INTO organizations (id, name, context, created_at) VALUES "
        "(1,'Acme',NULL,'2026-01-01'), (2,'acme ','ctx','2026-01-02')",
        "INSERT INTO contact_positions (id, telegram_id, organization, title, is_active) VALUES "
        "(1,100,'Acme',NULL,0), (2,100,'ACME','CEO',1), (3,200,' acme',NULL,1),"
        "(4,300,'Beta Corp','Eng',1), (5,300,'',NULL,1)",
    ])
    orgs = await _rows(db_engine, "SELECT id, name, context FROM organizations ORDER BY id")
    assert orgs[0] == (2, "acme", "ctx")  # keeper: has context; trimmed
    assert orgs[1][1] == "Beta Corp"
    beta_id = orgs[1][0]
    pos = await _rows(db_engine, "SELECT id, telegram_id, organization_id, organization, title FROM contact_positions ORDER BY id")
    assert pos == [
        (2, 100, 2, "acme", "CEO"),  # active+titled row wins over id 1
        (3, 200, 2, "acme", None),
        (4, 300, beta_id, "Beta Corp", "Eng"),
        (5, 300, None, "", None),  # blank org stays detached
    ]
    idx = {r[0] for r in await _rows(db_engine, "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_contact_positions_telegram_id", "ix_contact_positions_organization_id",
            "uq_organizations_name_ci"} <= idx


@pytest.mark.asyncio
async def test_fresh_schema_matches_model(db_engine):
    cols = {r[1] for r in await _rows(db_engine, "PRAGMA table_info(contact_positions)")}
    assert {"organization_id", "organization", "telegram_id"} <= cols
