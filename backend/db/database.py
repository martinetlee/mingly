import logging

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from backend.config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


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
