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
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

from backend.db.models import AUTOINCREMENT_TABLES

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

def foreign_keys_off(step: Callable[[AsyncConnection], Awaitable[None]]):
    """Mark a migration step that rebuilds tables: its migration runs with
    ``PRAGMA foreign_keys=OFF`` (dropping a parent table with foreign keys on
    would cascade-delete its children; the pragma can't change inside a
    transaction) and fails if it adds foreign-key violations."""
    step.foreign_keys_off = True  # type: ignore[attr-defined]
    return step


_PK_COLUMN = re.compile(r'(\n\s*"?id"?\s+INTEGER\s+NOT\s+NULL)\s*,', re.IGNORECASE)
_PK_CONSTRAINT = re.compile(r',\s*PRIMARY\s+KEY\s*\(\s*"?id"?\s*\)', re.IGNORECASE)


def _autoincrement_ddl(sql: str, table: str, new_name: str) -> str:
    """``CREATE TABLE`` SQL of ``table`` (as stored in sqlite_master) turned
    into an AUTOINCREMENT table named ``new_name``; every other column and
    constraint is kept verbatim."""
    head = re.compile(r'^\s*CREATE\s+TABLE\s+"?' + re.escape(table) + r'"?\s*\(', re.IGNORECASE)
    out, n_head = head.subn(f'CREATE TABLE "{new_name}" (', sql, count=1)
    out, n_col = _PK_COLUMN.subn(r"\1 PRIMARY KEY AUTOINCREMENT,", out, count=1)
    out, n_pk = _PK_CONSTRAINT.subn("", out, count=1)
    if (n_head, n_col, n_pk) != (1, 1, 1):
        raise RuntimeError(f"Unexpected schema for table {table!r}; can't add AUTOINCREMENT: {sql}")
    return out


async def _rebuild_with_autoincrement(conn: AsyncConnection, table: str) -> bool:
    """Rebuild ``table`` as AUTOINCREMENT, keeping its rows (and ids), indexes
    and triggers; ``sqlite_sequence`` starts at the current max(id). No-op
    for a missing table or one that already is AUTOINCREMENT."""
    row = (await conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    )).first()
    if row is None or "AUTOINCREMENT" in (row[0] or "").upper():
        return False
    tmp = f"{table}__rebuild"
    extras = [r[0] for r in (await conn.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type IN ('index', 'trigger') AND sql IS NOT NULL",
        (table,),
    )).all()]
    await conn.exec_driver_sql(_autoincrement_ddl(row[0], table, tmp))
    await conn.exec_driver_sql(f'INSERT INTO "{tmp}" SELECT * FROM "{table}"')
    await conn.exec_driver_sql(f'DROP TABLE "{table}"')
    await conn.exec_driver_sql(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    for sql in extras:
        await conn.exec_driver_sql(sql)
    high = (await conn.exec_driver_sql(f'SELECT COALESCE(MAX(id), 0) FROM "{table}"')).scalar() or 0
    await conn.exec_driver_sql("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
    if high:
        await conn.exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, high))
    return True


@foreign_keys_off
async def _never_reuse_ids(conn: AsyncConnection) -> None:
    """Every integer-id table becomes AUTOINCREMENT (models.AUTOINCREMENT_TABLES)."""
    rebuilt = [t for t in AUTOINCREMENT_TABLES if await _rebuild_with_autoincrement(conn, t)]
    leftovers = [name for name, sql in (await conn.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master"
    )).all() if "__rebuild" in name or "__rebuild" in (sql or "")]
    if leftovers:
        raise RuntimeError(f"AUTOINCREMENT rebuild left references to temporary tables: {leftovers}")
    if rebuilt:
        logger.info("Rebuilt %d tables as AUTOINCREMENT", len(rebuilt))


async def _dedupe_unclaimed_accounts(conn: AsyncConnection) -> None:
    """Before UNIQUE(channel) WHERE external_id IS NULL: a channel with several
    unclaimed accounts (a past get_or_create race) keeps its oldest; empty
    duplicates are dropped, ones that own data get a marker external_id so
    nothing is lost."""
    rows = (await conn.exec_driver_sql(
        "SELECT id, channel FROM accounts WHERE external_id IS NULL ORDER BY channel, id"
    )).all()
    seen: set = set()
    for account_id, channel in rows:
        if channel not in seen:
            seen.add(channel)
            continue
        owns = (await conn.exec_driver_sql(
            "SELECT EXISTS (SELECT 1 FROM conversations WHERE account_id = ?) "
            "OR EXISTS (SELECT 1 FROM calendar_events WHERE account_id = ?)",
            (account_id, account_id),
        )).scalar()
        if owns:
            await conn.exec_driver_sql(
                "UPDATE accounts SET external_id = ? WHERE id = ?", (f"unclaimed-duplicate-{account_id}", account_id)
            )
        else:
            await conn.exec_driver_sql("DELETE FROM accounts WHERE id = ?", (account_id,))


async def _dismissal_cloud_status(conn: AsyncConnection) -> None:
    """Fill the new columns: uploaded rows are 'synced'; rows a stopgap
    retired by writing ``cloud_skipped`` into payload_json become 'skipped'
    with that reason, and the marker leaves the payload."""
    import json

    await conn.exec_driver_sql(
        "UPDATE scam_dismissals SET cloud_status = 'synced' WHERE synced_at IS NOT NULL AND cloud_status = 'pending'"
    )
    rows = (await conn.exec_driver_sql(
        "SELECT id, payload_json FROM scam_dismissals WHERE payload_json LIKE '%cloud_skipped%'"
    )).all()
    for row_id, raw in rows:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict) or "cloud_skipped" not in data:
            continue
        marker = data.pop("cloud_skipped")
        reason = marker.get("reason") if isinstance(marker, dict) else str(marker)
        if set(data) == {"payload"}:  # the stopgap wrapped a non-object payload
            payload = data["payload"]
        else:
            payload = json.dumps(data, default=str)
        await conn.exec_driver_sql(
            "UPDATE scam_dismissals SET cloud_status = 'skipped', cloud_skipped_reason = ?, payload_json = ? "
            "WHERE id = ?",
            (reason or "skipped", payload, row_id),
        )


MIGRATIONS: list[tuple[int, str, list[MigrationStep]]] = [
    # Schema generation 2 starts here: create_all builds the full v2 schema
    # (backend/db/models.py) on a fresh DB, and creates tables added later
    # on an existing one. Append column / data changes from 1 on.
    (
        1,
        "Connector sync state on accounts; channel labels on conversations",
        [
            "ALTER TABLE accounts ADD COLUMN sync_state_json TEXT",
            "ALTER TABLE conversations ADD COLUMN labels_json TEXT",
        ],
    ),
    (
        2,
        "Never reuse ids: every integer-id table becomes AUTOINCREMENT",
        [_never_reuse_ids],
    ),
    (
        3,
        "At most one unclaimed (not yet signed-in) account per channel",
        [
            _dedupe_unclaimed_accounts,
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_account_channel_unclaimed "
            "ON accounts (channel) WHERE external_id IS NULL",
        ],
    ),
    (
        4,
        "Shield dismissals: cloud upload status and skip reason as columns",
        [
            "ALTER TABLE scam_dismissals ADD COLUMN cloud_status VARCHAR DEFAULT 'pending' NOT NULL",
            "ALTER TABLE scam_dismissals ADD COLUMN cloud_skipped_reason TEXT",
            _dismissal_cloud_status,
        ],
    ),
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
        fk_off = any(getattr(s, "foreign_keys_off", False) for s in stmts)
        if fk_off:
            violations_before = await _foreign_key_violations(conn)
            await conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            await conn.exec_driver_sql("BEGIN")
            try:
                await _apply_statements(conn, version, stmts, fresh=fresh)
                if fk_off:
                    added = await _foreign_key_violations(conn) - violations_before
                    if added:
                        raise RuntimeError(f"Migration {version} would add foreign-key violations: {sorted(added)[:10]}")
                await _record_migration(conn, version, description)
            except BaseException:
                await conn.rollback()
                raise
            await conn.commit()
        finally:
            if fk_off:
                await conn.exec_driver_sql("PRAGMA foreign_keys=ON")
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


async def _foreign_key_violations(conn: AsyncConnection) -> set:
    """``PRAGMA foreign_key_check`` rows as a set of (table, rowid, parent)."""
    return {(r[0], r[1], r[2]) for r in (await conn.exec_driver_sql("PRAGMA foreign_key_check")).all()}


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
