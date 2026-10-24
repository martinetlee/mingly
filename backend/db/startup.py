"""Startup schema management: create a fresh database, import a
generation-1 one (backend/db/legacy_import.py), and run pending migrations."""

import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import insert, inspect

from backend.db import database
from backend.db.legacy_import import import_generation1
from backend.db.migrations.runner import run_migrations
from backend.db.models import SCHEMA_GENERATION, SchemaMeta

logger = logging.getLogger(__name__)


def _schema_generation(path: Path) -> Optional[str]:
    """The generation stamped in an existing DB file: None = no tables at
    all (new file), "1" = a pre-v2 database, else the stamped value."""
    with closing(sqlite3.connect(str(path))) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return None
        if "schema_meta" not in tables:
            return "1"
        row = conn.execute("SELECT value FROM schema_meta WHERE key = 'schema_generation'").fetchone()
        return row[0] if row else "1"


class NewerSchemaError(RuntimeError):
    """The database was written by a newer build (a later schema generation)."""


def _generation_number(gen: str) -> Optional[int]:
    try:
        return int(gen)
    except ValueError:
        return None


async def create_fresh_schema(engine) -> None:
    """The full current schema on an empty database: create_all, the
    generation stamp, then the migrations (recorded as applied)."""
    async with engine.connect() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
        await conn.execute(insert(SchemaMeta).values(key="schema_generation", value=SCHEMA_GENERATION))
        await conn.commit()
        await run_migrations(conn, fresh=True)


async def _upgrade_old_database(path: Path) -> Optional[Path]:
    """A database from an older schema generation can't be migrated in
    place (v2 re-keyed every table). Generation 1 is imported into a new
    database (its original kept in backups/); any other older generation is
    backed up and replaced by a fresh database. Returns the backup path, or
    None when the DB is current or new.

    Backups are named ``<stem>-retired-gen<N>-<ts>.db`` so the migration
    runner's backup rotation (``<stem>-pre-v*.db``) never deletes them. A
    database from a NEWER generation (or an unreadable stamp) is never
    touched: startup fails with NewerSchemaError instead."""
    if not path.exists():
        return None
    gen = _schema_generation(path)
    if gen is None or gen == SCHEMA_GENERATION:
        return None
    number = _generation_number(gen)
    if number is None or number > int(SCHEMA_GENERATION):
        raise NewerSchemaError(
            f"{path} has schema generation {gen!r}, newer than this build's {SCHEMA_GENERATION!r}. "
            "Run a newer Mingly build, or move the file away to start fresh; it was left untouched."
        )
    await database.engine.dispose()  # no pooled connection may keep the old file open
    if number == 1:
        return (await import_generation1(path, create_schema=create_fresh_schema)).backup
    backups = path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"{path.stem}-retired-gen{number}-{datetime.utcnow():%Y%m%d%H%M%S}.db"
    with closing(sqlite3.connect(str(path))) as source, closing(sqlite3.connect(str(dest))) as target:
        source.backup(target)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    logger.warning("Database was from an older schema; backed up to %s and starting fresh", dest)
    return dest


async def init_db():
    """Create the schema on a fresh DB, upgrade one from an older schema
    generation, and run pending migrations."""
    engine = database.engine
    if engine.url.database:
        await _upgrade_old_database(Path(engine.url.database))

    async with engine.connect() as conn:
        fresh = not await conn.run_sync(lambda c: inspect(c).has_table("schema_meta"))
    if fresh:
        await create_fresh_schema(engine)
        return
    async with engine.connect() as conn:
        # Tables added since are created; existing tables are left alone
        # (later changes arrive as versioned migrations).
        await conn.run_sync(database.Base.metadata.create_all)
        await conn.commit()
        # Each migration commits atomically with its schema_migrations row; a
        # failure rolls it back and aborts startup.
        await run_migrations(conn)
