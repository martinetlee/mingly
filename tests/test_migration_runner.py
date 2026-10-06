"""Migration runner: atomic per migration; strict on existing databases."""

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from backend.db.migrations import areas, runner


async def _run(tmp_path, migrations, *, fresh):
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    async with eng.connect() as c:
        await c.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        # run_migrations also seeds default tags once
        await c.exec_driver_sql(
            "CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE, color TEXT, "
            "is_default BOOLEAN, created_at DATETIME)"
        )
        await c.commit()
    orig_core, orig_areas = list(runner.MIGRATIONS), list(areas.MODULES)
    runner.MIGRATIONS[:] = migrations
    areas.MODULES[:] = []
    try:
        async with eng.connect() as c:
            await runner.run_migrations(c, fresh=fresh)
    finally:
        runner.MIGRATIONS[:] = orig_core
        areas.MODULES[:] = orig_areas
    return eng


async def _state(eng):
    async with eng.connect() as c:
        cols = [r[1] for r in (await c.exec_driver_sql("PRAGMA table_info(t)")).all()]
        versions = [r[0] for r in (await c.exec_driver_sql("SELECT version FROM schema_migrations")).all()]
    await eng.dispose()
    return cols, versions


@pytest.mark.asyncio
async def test_failed_migration_rolls_back_ddl_and_record(tmp_path):
    bad = [(1, "bad", ["ALTER TABLE t ADD COLUMN a INTEGER", "CREATE INDEX ix_a ON t(a)",
                       "ALTER TABLE missing ADD COLUMN b INTEGER"])]
    with pytest.raises(Exception):
        await _run(tmp_path, bad, fresh=False)
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    cols, versions = await _state(eng)
    assert cols == ["id"] and 1 not in versions


@pytest.mark.asyncio
async def test_unknown_column_aborts_on_existing_db(tmp_path):
    typo = [(1, "typo", ["UPDATE t SET nonexistent_col = 1"])]
    with pytest.raises(Exception):
        await _run(tmp_path, typo, fresh=False)


@pytest.mark.asyncio
async def test_unknown_column_tolerated_on_fresh_db(tmp_path):
    legacy = [(1, "legacy data fix", ["UPDATE t SET removed_col = 1"])]
    eng = await _run(tmp_path, legacy, fresh=True)
    _, versions = await _state(eng)
    assert 1 in versions
