import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import event, insert, inspect
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from backend.config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


@event.listens_for(engine.sync_engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    # WAL lets readers coexist with a writer; busy_timeout makes a second
    # writer wait instead of failing immediately with "database is locked".
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


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


async def _retire_old_database(path: Path) -> Optional[Path]:
    """A database from an older schema generation can't be upgraded in place
    (v2 re-keyed every table): back it up next to the DB and start fresh.
    Returns the backup path, or None when the DB is current or new.

    The backup is named ``<stem>-retired-gen<N>-<ts>.db`` so the migration
    runner's backup rotation (``<stem>-pre-v*.db``) never deletes it. A
    database from a NEWER generation (or an unreadable stamp) is never
    touched: startup fails with NewerSchemaError instead."""
    from backend.db.models import SCHEMA_GENERATION

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
    backups = path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"{path.stem}-retired-gen{number}-{datetime.utcnow():%Y%m%d%H%M%S}.db"
    with closing(sqlite3.connect(str(path))) as source, closing(sqlite3.connect(str(dest))) as target:
        source.backup(target)
    await engine.dispose()  # no pooled connection may keep the old file open
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    logger.warning("Database was from an older schema; backed up to %s and starting fresh", dest)
    return dest


async def init_db():
    import importlib

    models = importlib.import_module("backend.db.models")  # registers models with Base
    from backend.db.migrations.runner import run_migrations

    if engine.url.database:
        await _retire_old_database(Path(engine.url.database))

    async with engine.connect() as conn:
        fresh = not await conn.run_sync(lambda c: inspect(c).has_table("schema_meta"))
        # A fresh DB gets the final schema from create_all; existing tables
        # are left alone (later changes arrive as versioned migrations).
        await conn.run_sync(Base.metadata.create_all)
        if fresh:
            await conn.execute(
                insert(models.SchemaMeta).values(key="schema_generation", value=models.SCHEMA_GENERATION)
            )
        await conn.commit()

        # Each migration commits atomically with its schema_migrations row; a
        # failure rolls it back and aborts startup.
        await run_migrations(conn, fresh=fresh)


async def dispose_db():
    """Close pooled connections on shutdown (checkpoints the WAL cleanly)."""
    await engine.dispose()
