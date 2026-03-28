"""
Versioned migration runner for Mingly.

Each migration is an (id, description, sql_statements) tuple.  The runner
tracks applied migrations in a ``schema_migrations`` table and applies
pending ones in order inside a transaction.  Failures are logged and cause
startup to abort so that partial schema states are never silently ignored.
"""

from __future__ import annotations

import logging
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Migration definitions -- append-only list.
# Each entry: (version: int, description: str, stmts: list[str])
#
# Rules:
#   * Never reorder or delete entries.
#   * Use IF NOT EXISTS / IF EXISTS where the DDL dialect supports it.
#   * ALTER TABLE ADD COLUMN is idempotent-safe on SQLite (duplicate column
#     raises "duplicate column name" which we catch explicitly).
#   * Avoid destructive DML (DELETE, UPDATE) unless required for data
#     cleanup before a constraint change (see migration 4 for example).
# ---------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "Add is_hidden and is_briefing columns to chats",
        [
            "ALTER TABLE chats ADD COLUMN is_hidden BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN is_briefing BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
    (
        2,
        "Add media_path column to messages",
        [
            "ALTER TABLE messages ADD COLUMN media_path TEXT",
        ],
    ),
    (
        3,
        "Add is_tracked column to chats",
        [
            "ALTER TABLE chats ADD COLUMN is_tracked BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
    (
        4,
        "Add unique index on messages(telegram_id, chat_id)",
        [
            # Delete duplicate (telegram_id, chat_id) rows, keeping only the
            # row with the highest id, so the UNIQUE index creation succeeds
            # on databases that already contain duplicates.
            (
                "DELETE FROM messages WHERE id NOT IN ("
                "  SELECT MAX(id) FROM messages"
                "  GROUP BY telegram_id, chat_id"
                ")"
            ),
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_message_telegram_chat ON messages (telegram_id, chat_id)",
        ],
    ),
    (
        5,
        "Add text_html and reactions_json columns to messages",
        [
            "ALTER TABLE messages ADD COLUMN text_html TEXT",
            "ALTER TABLE messages ADD COLUMN reactions_json TEXT",
        ],
    ),
    (
        6,
        "Create tags and chat_tags tables",
        [
            "CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, color TEXT NOT NULL DEFAULT 'slate', is_default BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME)",
            "CREATE TABLE IF NOT EXISTS chat_tags (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, tag_id INTEGER NOT NULL, UNIQUE(chat_id, tag_id))",
        ],
    ),
    (
        7,
        "Add category column to tags",
        [
            "ALTER TABLE tags ADD COLUMN category TEXT",
        ],
    ),
    (
        8,
        "Create conversations and conversation_messages tables",
        [
            "CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, created_at DATETIME, updated_at DATETIME)",
            "CREATE TABLE IF NOT EXISTS conversation_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL, content TEXT NOT NULL, created_at DATETIME)",
        ],
    ),
    (
        9,
        "Add unread_count column to chats",
        [
            "ALTER TABLE chats ADD COLUMN unread_count INTEGER DEFAULT 0",
        ],
    ),
    (
        10,
        "Add is_active and ended_at columns to contact_positions",
        [
            "ALTER TABLE contact_positions ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE contact_positions ADD COLUMN ended_at TEXT",
        ],
    ),
    (
        11,
        "Add reply_to_telegram_id column to messages",
        [
            "ALTER TABLE messages ADD COLUMN reply_to_telegram_id INTEGER",
        ],
    ),
    (
        12,
        "Add my_reaction column to messages",
        [
            "ALTER TABLE messages ADD COLUMN my_reaction TEXT",
        ],
    ),
    (
        13,
        "Add is_important column to contacts",
        [
            "ALTER TABLE contacts ADD COLUMN is_important BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
    (
        14,
        "Create briefing_runs and briefing_entries tables",
        [
            "CREATE TABLE IF NOT EXISTS briefing_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, generated_at DATETIME, days INTEGER DEFAULT 1)",
            "CREATE TABLE IF NOT EXISTS briefing_entries (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES briefing_runs(id) ON DELETE CASCADE, chat_id INTEGER NOT NULL, chat_name TEXT NOT NULL, category TEXT NOT NULL, summary TEXT NOT NULL)",
        ],
    ),
    (
        15,
        "Add sources_json column to briefing_entries",
        [
            "ALTER TABLE briefing_entries ADD COLUMN sources_json TEXT",
        ],
    ),
    (
        16,
        "Add members_count column to chats",
        [
            "ALTER TABLE chats ADD COLUMN members_count INTEGER",
        ],
    ),
    (
        17,
        "Add relationship status columns to contacts",
        [
            "ALTER TABLE contacts ADD COLUMN relationship_status TEXT",
            "ALTER TABLE contacts ADD COLUMN relationship_status_manual BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE contacts ADD COLUMN last_interaction_at DATETIME",
            "ALTER TABLE contacts ADD COLUMN last_outgoing_at DATETIME",
            "ALTER TABLE contacts ADD COLUMN last_incoming_at DATETIME",
        ],
    ),
    (
        18,
        "Create catchup_items table for triage queue",
        [
            "CREATE TABLE IF NOT EXISTS catchup_items (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_telegram_id INTEGER NOT NULL UNIQUE, contact_id INTEGER, status TEXT NOT NULL DEFAULT 'pending', note TEXT, snoozed_until DATETIME, handled_at DATETIME, created_at DATETIME, updated_at DATETIME)",
        ],
    ),
    (
        19,
        "Create catchup_summaries table for cronjob-generated summaries",
        [
            "CREATE TABLE IF NOT EXISTS catchup_summaries (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_telegram_id INTEGER NOT NULL UNIQUE, summary TEXT NOT NULL, summary_messages_json TEXT, recent_messages_json TEXT, last_activity_at DATETIME, total_messages_at_generation INTEGER DEFAULT 0, generated_at DATETIME)",
        ],
    ),
    (
        20,
        "Migrate contact_positions from contact_id to telegram_id",
        [
            # Add telegram_id column
            "ALTER TABLE contact_positions ADD COLUMN telegram_id INTEGER",
            # Populate from contacts table
            "UPDATE contact_positions SET telegram_id = (SELECT telegram_id FROM contacts WHERE contacts.id = contact_positions.contact_id)",
            # Remove rows that couldn't be resolved (orphaned)
            "DELETE FROM contact_positions WHERE telegram_id IS NULL",
        ],
    ),
    (
        21,
        "Rebuild contact_positions table: drop contact_id FK, make telegram_id NOT NULL with new unique constraint",
        [
            # SQLite doesn't support DROP COLUMN/CONSTRAINT, so recreate the table
            "CREATE TABLE contact_positions_new (id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_id INTEGER NOT NULL, organization VARCHAR NOT NULL, title VARCHAR, is_active INTEGER NOT NULL DEFAULT 1, ended_at TEXT, created_at DATETIME NOT NULL DEFAULT (datetime('now')), UNIQUE(telegram_id, organization))",
            "INSERT INTO contact_positions_new (id, telegram_id, organization, title, is_active, ended_at, created_at) SELECT id, telegram_id, organization, title, is_active, ended_at, created_at FROM contact_positions WHERE telegram_id IS NOT NULL",
            "DROP TABLE contact_positions",
            "ALTER TABLE contact_positions_new RENAME TO contact_positions",
            "CREATE INDEX ix_contact_positions_telegram_id ON contact_positions (telegram_id)",
        ],
    ),
    (
        22,
        "Add delivery_state, is_edited columns to messages and pinned_message_id to chats",
        [
            "ALTER TABLE messages ADD COLUMN delivery_state TEXT DEFAULT 'sent'",
            "ALTER TABLE messages ADD COLUMN is_edited BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN pinned_message_id INTEGER",
        ],
    ),
    (
        23,
        "Add pin, archive, mute, and mark-unread columns to chats",
        [
            "ALTER TABLE chats ADD COLUMN is_pinned BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN is_archived BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN is_muted BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN muted_until DATETIME",
            "ALTER TABLE chats ADD COLUMN is_marked_unread BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
    (
        24,
        "Create admin_action_log table for group/channel admin actions",
        [
            "CREATE TABLE IF NOT EXISTS admin_action_log ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  chat_telegram_id INTEGER NOT NULL,"
            "  action TEXT NOT NULL,"
            "  target_user_id INTEGER,"
            "  details TEXT,"
            "  created_at DATETIME NOT NULL"
            ")",
            "CREATE INDEX IF NOT EXISTS ix_admin_action_log_chat ON admin_action_log (chat_telegram_id, created_at)",
        ],
    ),
    (
        25,
        "Add media_filename, media_size, and grouped_id columns to messages",
        [
            "ALTER TABLE messages ADD COLUMN media_filename TEXT",
            "ALTER TABLE messages ADD COLUMN media_size INTEGER",
            "ALTER TABLE messages ADD COLUMN grouped_id INTEGER",
        ],
    ),
    (
        26,
        "Add forum topic support: is_forum and slow_mode_seconds on chats, topic_id and is_pinned on messages",
        [
            "ALTER TABLE chats ADD COLUMN is_forum BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN slow_mode_seconds INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN topic_id INTEGER",
            "ALTER TABLE messages ADD COLUMN is_pinned BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
    (
        27,
        "Add scam detection tables and message scam columns",
        [
            "ALTER TABLE messages ADD COLUMN scam_score REAL",
            "ALTER TABLE messages ADD COLUMN scam_reason TEXT",
            """CREATE TABLE IF NOT EXISTS scam_check_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT UNIQUE NOT NULL,
                request_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payload_json TEXT NOT NULL,
                response_json TEXT,
                related_chat_id INTEGER,
                related_message_ids_json TEXT,
                related_telegram_ids_json TEXT,
                followup_request_json TEXT,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                completed_at DATETIME
            )""",
            """CREATE TABLE IF NOT EXISTS scam_flags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id BIGINT UNIQUE NOT NULL,
                source TEXT NOT NULL,
                confidence REAL,
                reason TEXT,
                request_id TEXT,
                flagged_at DATETIME NOT NULL
            )""",
        ],
    ),
    (
        28,
        "Add last_activity_at to chats for sorting before messages are synced",
        [
            "ALTER TABLE chats ADD COLUMN last_activity_at DATETIME",
        ],
    ),
    (
        29,
        "Rename messages.telegram_id to messages.tg_message_id",
        [
            "ALTER TABLE messages RENAME COLUMN telegram_id TO tg_message_id",
            "ALTER TABLE messages RENAME COLUMN reply_to_telegram_id TO reply_to_tg_message_id",
            "DROP INDEX IF EXISTS uq_message_telegram_chat",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_message_tgmsgid_chat ON messages (tg_message_id, chat_id)",
        ],
    ),
    (
        30,
        "Add performance indexes on messages for chat loading",
        [
            "CREATE INDEX IF NOT EXISTS ix_messages_chat_date ON messages (chat_id, date DESC)",
            "CREATE INDEX IF NOT EXISTS ix_messages_chat_tgmsgid ON messages (chat_id, tg_message_id)",
            "CREATE INDEX IF NOT EXISTS ix_messages_sender_id ON messages (sender_id)",
        ],
    ),
    (
        31,
        "Cache last message fields on chats table to avoid window function",
        [
            "ALTER TABLE chats ADD COLUMN last_message_text TEXT",
            "ALTER TABLE chats ADD COLUMN last_message_sender TEXT",
            "ALTER TABLE chats ADD COLUMN last_message_date DATETIME",
            # Backfill from existing messages
            """UPDATE chats SET
                last_message_text = (SELECT text FROM messages WHERE messages.chat_id = chats.telegram_id ORDER BY date DESC LIMIT 1),
                last_message_sender = (SELECT sender_name FROM messages WHERE messages.chat_id = chats.telegram_id ORDER BY date DESC LIMIT 1),
                last_message_date = (SELECT date FROM messages WHERE messages.chat_id = chats.telegram_id ORDER BY date DESC LIMIT 1)
            """,
        ],
    ),
    (
        32,
        "Add scam LLM prompt and raw response columns to messages for audit trail",
        [
            "ALTER TABLE messages ADD COLUMN scam_llm_prompt TEXT",
            "ALTER TABLE messages ADD COLUMN scam_llm_response TEXT",
        ],
    ),
]

# Seed data applied once (tracked as migration version 0).
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


async def run_migrations(conn: AsyncConnection) -> None:
    """Apply all pending migrations in order.

    Raises on any unexpected failure so that the application does not start
    with an inconsistent schema.
    """
    await _ensure_migration_table(conn)
    applied = await _applied_versions(conn)

    for version, description, stmts in MIGRATIONS:
        if version in applied:
            continue

        logger.info("Applying migration %d: %s", version, description)
        for stmt in stmts:
            try:
                await conn.execute(sa.text(stmt))
            except Exception as exc:
                # ALTER TABLE ADD COLUMN is not idempotent on SQLite; a
                # "duplicate column name" error means the column already
                # exists (e.g. created by create_all on a fresh DB).
                if stmt.upper().startswith("ALTER TABLE") and _is_duplicate_column_error(exc):
                    logger.debug(
                        "Column already exists, skipping: %s", stmt
                    )
                elif _is_no_such_column_error(exc):
                    # On a fresh DB, data migrations referencing removed columns
                    # are harmless — the table was created with the final schema.
                    logger.debug(
                        "Column does not exist (fresh DB), skipping: %s", stmt
                    )
                else:
                    logger.error(
                        "Migration %d failed on statement: %s -- %s",
                        version, stmt, exc,
                    )
                    raise
        await _record_migration(conn, version, description)
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
