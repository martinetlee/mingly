import logging

from sqlalchemy import event, inspect
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
    cursor.close()


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    import importlib

    importlib.import_module("backend.db.models")  # registers models with Base
    from backend.db.migrations.runner import run_migrations

    async with engine.connect() as conn:
        # A fresh DB gets the final schema from create_all, so historical
        # migrations may legitimately reference columns that no longer exist.
        fresh = not await conn.run_sync(lambda c: inspect(c).has_table("chats"))
        # Create any tables that don't exist yet (safe, non-destructive).
        await conn.run_sync(Base.metadata.create_all)
        await conn.commit()

        # Apply versioned migrations for columns/indexes added after the
        # initial schema. Each migration commits atomically with its
        # schema_migrations row; a failure rolls it back and aborts startup.
        await run_migrations(conn, fresh=fresh)


async def dispose_db():
    """Close pooled connections on shutdown (checkpoints the WAL cleanly)."""
    await engine.dispose()
