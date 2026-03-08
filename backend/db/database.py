from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

from backend.config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    from backend.db import models  # noqa: registers models with Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add columns introduced after initial schema (safe to run repeatedly)
        for stmt in [
            "ALTER TABLE chats ADD COLUMN is_hidden BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN is_briefing BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN media_path TEXT",
            # Deduplicate before adding unique constraint
            "ALTER TABLE chats ADD COLUMN is_tracked BOOLEAN NOT NULL DEFAULT 0",
            "DELETE FROM messages WHERE id NOT IN (SELECT MIN(id) FROM messages GROUP BY telegram_id, chat_id)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_message_telegram_chat ON messages (telegram_id, chat_id)",
            "ALTER TABLE messages ADD COLUMN text_html TEXT",
            "ALTER TABLE messages ADD COLUMN reactions_json TEXT",
        ]:
            try:
                await conn.execute(__import__('sqlalchemy').text(stmt))
            except Exception:
                pass  # column already exists
