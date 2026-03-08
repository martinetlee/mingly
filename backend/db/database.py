from datetime import datetime

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
            # Tag system
            "CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, color TEXT NOT NULL DEFAULT 'slate', is_default BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME)",
            "CREATE TABLE IF NOT EXISTS chat_tags (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, UNIQUE(chat_id, tag_id))",
            "ALTER TABLE tags ADD COLUMN category TEXT",
            # Conversation persistence for Intelligence chat
            "CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, created_at DATETIME, updated_at DATETIME)",
            "CREATE TABLE IF NOT EXISTS conversation_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL, created_at DATETIME)",
            # Unread message counts
            "ALTER TABLE chats ADD COLUMN unread_count INTEGER DEFAULT 0",
            # Former employee support on ContactPosition
            "ALTER TABLE contact_positions ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE contact_positions ADD COLUMN ended_at TEXT",
        ]:
            try:
                await conn.execute(__import__('sqlalchemy').text(stmt))
            except Exception:
                pass  # column/table already exists

        # Seed default tags
        import sqlalchemy as _sa
        defaults = [
            ("Work", "indigo"), ("Personal", "emerald"), ("Family", "rose"),
            ("Finance", "amber"), ("Projects", "violet"), ("News", "sky"),
        ]
        for tag_name, tag_color in defaults:
            try:
                await conn.execute(_sa.text(
                    "INSERT OR IGNORE INTO tags (name, color, is_default, created_at) VALUES (:n, :c, 1, :d)"
                ), {"n": tag_name, "c": tag_color, "d": datetime.utcnow()})
            except Exception:
                pass
