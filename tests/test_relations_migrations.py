"""Schema-level invariants of the CRM tables (once established by v1
migrations 90-92, now part of the v2 schema): case/whitespace-insensitive
unique tag and organization names, FK-keyed positions, and cascades that
leave no orphaned tag assignments or board state."""

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from tests import factories as f


async def _exec(engine, *stmts):
    async with engine.connect() as conn:
        for stmt in stmts:
            await conn.execute(sa.text(stmt))
        await conn.commit()


async def _rows(engine, sql):
    async with engine.connect() as conn:
        return [tuple(r) for r in (await conn.execute(sa.text(sql))).all()]


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["tags", "organizations"])
async def test_names_are_unique_case_and_whitespace_insensitively(db_engine, table):
    cols = "name, color, is_default, created_at" if table == "tags" else "name, created_at"
    vals = "'{}', 'slate', 0, '2026-01-01'" if table == "tags" else "'{}', '2026-01-01'"
    await _exec(db_engine, f"DELETE FROM {table}", f"INSERT INTO {table} ({cols}) VALUES ({vals.format('Work')})")
    for variant in ("work", " WORK ", "Work"):
        with pytest.raises(IntegrityError):
            await _exec(db_engine, f"INSERT INTO {table} ({cols}) VALUES ({vals.format(variant)})")
    await _exec(db_engine, f"INSERT INTO {table} ({cols}) VALUES ({vals.format('Workshop')})")


@pytest.mark.asyncio
async def test_one_position_per_person_and_org_and_org_delete_detaches(db_engine, db):
    pid = (await f.identity(db, "1")).person_id
    await _exec(
        db_engine,
        "INSERT INTO organizations (id, name, created_at) VALUES (1, 'Acme', '2026-01-01')",
        f"INSERT INTO person_positions (person_id, organization_id, organization, is_active, created_at) "
        f"VALUES ({pid}, 1, 'Acme', 1, '2026-01-01')",
    )
    with pytest.raises(IntegrityError):
        await _exec(db_engine, f"INSERT INTO person_positions (person_id, organization_id, organization, is_active, "
                               f"created_at) VALUES ({pid}, 1, 'Acme', 0, '2026-01-01')")
    # Detached history (organization_id NULL) may repeat
    for _ in range(2):
        await _exec(db_engine, f"INSERT INTO person_positions (person_id, organization_id, organization, is_active, "
                               f"created_at) VALUES ({pid}, NULL, 'Old Co', 0, '2026-01-01')")
    await _exec(db_engine, "DELETE FROM organizations WHERE id = 1")
    rows = await _rows(db_engine, "SELECT organization_id, organization FROM person_positions ORDER BY id")
    assert rows == [(None, "Acme"), (None, "Old Co"), (None, "Old Co")]
    await _exec(db_engine, f"DELETE FROM people WHERE id = {pid}")
    assert await _rows(db_engine, "SELECT COUNT(*) FROM person_positions") == [(0,)]


@pytest.mark.asyncio
async def test_conversation_delete_cascades_tags_and_board_state(db_engine, db):
    conv = await f.conversation(db, "-1", title="G", kind="group")
    await _exec(
        db_engine,
        "INSERT INTO tags (id, name, color, is_default, created_at) VALUES (100, 'T', 'slate', 0, '2026-01-01')",
        "INSERT INTO workspaces (id, name, color, match_mode, view_mode, position, created_at) "
        "VALUES (1, 'W', 'indigo', 'any', 'kanban', 0, '2026-01-01')",
        f"INSERT INTO conversation_tags (conversation_id, tag_id) VALUES ({conv.id}, 100)",
        f"INSERT INTO workspace_conversation_overrides (workspace_id, conversation_id) VALUES (1, {conv.id})",
    )
    with pytest.raises(IntegrityError):  # one assignment per (conversation, tag)
        await _exec(db_engine, f"INSERT INTO conversation_tags (conversation_id, tag_id) VALUES ({conv.id}, 100)")
    await _exec(db_engine, f"DELETE FROM conversations WHERE id = {conv.id}")
    assert await _rows(db_engine, "SELECT COUNT(*) FROM conversation_tags") == [(0,)]
    assert await _rows(db_engine, "SELECT COUNT(*) FROM workspace_conversation_overrides") == [(0,)]


@pytest.mark.asyncio
async def test_tag_delete_cascades_rules_columns_and_unscopes_boards(db_engine):
    await _exec(
        db_engine,
        "INSERT INTO tags (id, name, color, is_default, created_at) VALUES "
        "(100, 'Stream', 'slate', 0, '2026-01-01'), (101, 'Stage', 'slate', 0, '2026-01-01')",
        "INSERT INTO workspaces (id, name, color, match_mode, view_mode, position, created_at) "
        "VALUES (1, 'W', 'indigo', 'any', 'kanban', 0, '2026-01-01')",
        "INSERT INTO workspace_tags (workspace_id, tag_id, is_exclusion) VALUES (1, 100, 0)",
        "INSERT INTO workspace_boards (id, workspace_id, name, scope_tag_id, position) VALUES (1, 1, 'Main', 100, 0)",
        "INSERT INTO workspace_columns (workspace_id, tag_id, board_id, label, position) VALUES (1, 101, 1, 'S', 0)",
        "DELETE FROM tags",
    )
    assert await _rows(db_engine, "SELECT COUNT(*) FROM workspace_tags") == [(0,)]
    assert await _rows(db_engine, "SELECT COUNT(*) FROM workspace_columns") == [(0,)]
    assert await _rows(db_engine, "SELECT scope_tag_id FROM workspace_boards") == [(None,)]
