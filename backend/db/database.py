import logging

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
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
    from backend.db import models  # noqa: registers models with Base
    from backend.db.migrations.runner import run_migrations

    async with engine.begin() as conn:
        # Create any tables that don't exist yet (safe, non-destructive).
        await conn.run_sync(Base.metadata.create_all)

        # Apply versioned migrations for columns/indexes added after the
        # initial schema.  Failures are logged and re-raised so the app
        # never starts with a partially-migrated database.
        await run_migrations(conn)
