"""
Versioned migration runner for Mingly.

Each migration is an (id, description, sql_statements) tuple.  The runner
tracks applied migrations in a ``schema_migrations`` table and applies
pending ones in order inside a transaction.  Failures are logged and cause
startup to abort so that partial schema states are never silently ignored.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# A migration step is a SQL string, or an async callable for data transforms
# that SQL can't express safely (JSON rewrites, per-row mapping). A callable
# runs inside the migration's transaction and must be a no-op on empty tables
# (a fresh DB already has the final schema from create_all).
MigrationStep = Union[str, Callable[[AsyncConnection], Awaitable[None]]]

# Pending migrations at or above this version run on an existing database
# only after it is backed up (see _backup_before).
BACKUP_FROM_VERSION = 1
BACKUPS_KEPT = 3

# ---------------------------------------------------------------------------
# Migration definitions -- append-only list.
# Each entry: (version: int, description: str, steps: list[MigrationStep])
#
# Rules:
#   * Never reorder or delete entries.
#   * Use IF NOT EXISTS / IF EXISTS where the DDL dialect supports it.
#   * ALTER TABLE ADD COLUMN is idempotent-safe on SQLite (duplicate column
#     raises "duplicate column name" which we catch explicitly).
#   * Avoid destructive DML (DELETE, UPDATE) unless required for data
#     cleanup before a constraint change.
# ---------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, str, list[MigrationStep]]] = [
    # Schema generation 2 starts here: create_all builds the full v2 schema
    # (backend/db/models.py) on a fresh DB. Append schema changes from 1 on.
]


_DEFAULT_TAGS = [
    ("Work", "indigo"),
    ("Personal", "emerald"),
    ("Family", "rose"),
    ("Finance", "amber"),
    ("Projects", "violet"),
    ("News", "sky"),
]


async def _ensure_migration_table(conn: AsyncConnection) -> None:
    """Create the schema_migrations tracking table if it doesn't exist."""
    await conn.execute(sa.text(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version INTEGER PRIMARY KEY,"
        "  description TEXT NOT NULL,"
        "  applied_at DATETIME NOT NULL"
        ")"
    ))


async def _applied_versions(conn: AsyncConnection) -> set[int]:
    """Return the set of already-applied migration versions."""
    result = await conn.execute(sa.text("SELECT version FROM schema_migrations"))
    return {row[0] for row in result.fetchall()}


def _is_duplicate_column_error(exc: Exception) -> bool:
    """Check if an exception is a SQLite 'duplicate column name' error."""
    msg = str(exc).lower()
    return "duplicate column name" in msg


def _is_no_such_column_error(exc: Exception) -> bool:
    """Check if an exception is a SQLite 'no such column' error."""
    msg = str(exc).lower()
    return "no such column" in msg


async def _record_migration(
    conn: AsyncConnection, version: int, description: str
) -> None:
    await conn.execute(
        sa.text(
            "INSERT INTO schema_migrations (version, description, applied_at) "
            "VALUES (:v, :d, :t)"
        ),
        {"v": version, "d": description, "t": datetime.utcnow()},
    )


async def run_migrations(conn: AsyncConnection, *, fresh: bool = False) -> None:
    """Apply all pending migrations in order.

    Raises on any unexpected failure so that the application does not start
    with an inconsistent schema.
    """
    await _ensure_migration_table(conn)
    applied = await _applied_versions(conn)
    await conn.commit()

    migrations = all_migrations()
    if not fresh:
        pending = [v for v, _, _ in migrations if v not in applied and v >= BACKUP_FROM_VERSION]
        if pending:
            await _backup_before(conn, min(pending))

    for version, description, stmts in migrations:
        if version in applied:
            continue

        logger.info("Applying migration %d: %s", version, description)
        # pysqlite only opens a transaction implicitly before DML, so DDL
        # would otherwise autocommit statement by statement. An explicit
        # BEGIN makes the whole migration (DDL + its record) atomic.
        await conn.exec_driver_sql("BEGIN")
        try:
            await _apply_statements(conn, version, stmts, fresh=fresh)
            await _record_migration(conn, version, description)
        except BaseException:
            await conn.rollback()
            raise
        await conn.commit()
        logger.info("Migration %d applied successfully", version)

    # Seed default tags (idempotent via INSERT OR IGNORE)
    _SEED_VERSION = 0
    if _SEED_VERSION not in applied:
        logger.info("Seeding default tags")
        for tag_name, tag_color in _DEFAULT_TAGS:
            await conn.execute(
                sa.text(
                    "INSERT OR IGNORE INTO tags (name, color, is_default, created_at) "
                    "VALUES (:n, :c, 1, :d)"
                ),
                {"n": tag_name, "c": tag_color, "d": datetime.utcnow()},
            )
        await _record_migration(conn, _SEED_VERSION, "Seed default tags")
        await conn.commit()


async def _database_path(conn: AsyncConnection) -> Optional[Path]:
    rows = (await conn.exec_driver_sql("PRAGMA database_list")).all()
    main = next((r[2] for r in rows if r[1] == "main"), "")
    return Path(main) if main else None  # "" = in-memory DB


def _copy_database(src: Path, dest: Path) -> None:
    # sqlite3's online backup reads a consistent snapshot (WAL included),
    # unlike a raw file copy.
    with sqlite3.connect(str(src)) as source, sqlite3.connect(str(dest)) as target:
        source.backup(target)


async def _backup_before(conn: AsyncConnection, version: int) -> Optional[Path]:
    """Snapshot the database before data-rewriting migrations. Restoring the
    file and running the previous build is the rollback (there are no
    down-migrations). Keeps the newest BACKUPS_KEPT snapshots."""
    src = await _database_path(conn)
    if src is None or not src.exists():
        return None
    backups = src.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"{src.stem}-pre-v{version}-{datetime.utcnow():%Y%m%d%H%M%S}.db"
    await asyncio.to_thread(_copy_database, src, dest)
    logger.info("Backed up database to %s before migration %d", dest, version)
    for old in sorted(backups.glob(f"{src.stem}-pre-v*.db"), key=lambda p: p.stat().st_mtime)[:-BACKUPS_KEPT]:
        old.unlink(missing_ok=True)
    return dest


async def _apply_statements(
    conn: AsyncConnection, version: int, stmts: list[MigrationStep], *, fresh: bool = False
) -> None:
    for stmt in stmts:
        if callable(stmt):
            try:
                await stmt(conn)
            except Exception as exc:
                logger.error("Migration %d failed in step %s -- %s", version, getattr(stmt, "__name__", stmt), exc)
                raise
            continue
        try:
            await conn.execute(sa.text(stmt))
        except Exception as exc:
            # ADD COLUMN for a column that already exists means the table was
            # just created by create_all with the final schema — the intended
            # end state. Only on a fresh DB may data migrations reference
            # columns that later migrations removed; on an existing DB that
            # error is a real bug and must abort.
            if stmt.upper().startswith("ALTER TABLE") and _is_duplicate_column_error(exc):
                logger.debug("Column already exists, skipping: %s", stmt)
            elif fresh and _is_no_such_column_error(exc):
                logger.debug("Column does not exist (fresh DB), skipping: %s", stmt)
            else:
                logger.error("Migration %d failed on statement: %s -- %s", version, stmt, exc)
                raise


def all_migrations() -> list[tuple[int, str, list[MigrationStep]]]:
    """Migrations ordered by version."""
    versions = [v for v, _, _ in MIGRATIONS]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Duplicate migration version numbers")
    return sorted(MIGRATIONS, key=lambda m: m[0])
