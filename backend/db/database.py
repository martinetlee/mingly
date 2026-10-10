"""The async engine, session factory and declarative Base.

Creating / upgrading the schema at startup is ``backend.db.startup.init_db``.
"""

import logging

from sqlalchemy import event
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


async def dispose_db():
    """Close pooled connections on shutdown (checkpoints the WAL cleanly)."""
    await engine.dispose()
