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
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

# A migration step is a SQL string, or an async callable for data transforms
# that SQL can't express safely (JSON rewrites, per-row mapping). A callable
# runs inside the migration's transaction and must be a no-op on empty tables
# (a fresh DB already has the final schema from create_all).
MigrationStep = Union[str, Callable[[AsyncConnection], Awaitable[None]]]

# Pending migrations at or above this version rewrite user data in place, so
# an existing database is backed up first (see _backup_before).
BACKUP_FROM_VERSION = 120
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
#     cleanup before a constraint change (see migration 4 for example).
# ---------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, str, list[MigrationStep]]] = [
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
    (
        33,
        "Add scam category support and create scam_reports table",
        [
            "ALTER TABLE messages ADD COLUMN scam_category TEXT",
            "ALTER TABLE scam_flags ADD COLUMN category TEXT",
            """CREATE TABLE IF NOT EXISTS scam_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id TEXT UNIQUE NOT NULL,
                reported_telegram_id INTEGER NOT NULL,
                reported_in_chat_id INTEGER NOT NULL,
                chat_type TEXT,
                reporter_verdict TEXT NOT NULL,
                scam_category TEXT,
                custom_category TEXT,
                messages_json TEXT NOT NULL,
                local_detection_json TEXT,
                status TEXT NOT NULL DEFAULT 'submitted',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )""",
        ],
    ),
    (
        34,
        "Add triggering message fields to scam_flags and normalize source values",
        [
            "ALTER TABLE scam_flags ADD COLUMN flagged_message_text TEXT",
            "ALTER TABLE scam_flags ADD COLUMN flagged_message_chat_id INTEGER",
            "ALTER TABLE scam_flags ADD COLUMN flagged_message_id INTEGER",
            # Normalize source values: local_llm → local, external_api → cloud
            "UPDATE scam_flags SET source = 'local' WHERE source = 'local_llm'",
            "UPDATE scam_flags SET source = 'cloud' WHERE source = 'external_api'",
        ],
    ),
    (
        35,
        "Add scam trace JSON column for pipeline execution traces",
        [
            "ALTER TABLE messages ADD COLUMN scam_trace_json TEXT",
        ],
    ),
    (
        36,
        "Add cloud sync tracking columns",
        [
            "ALTER TABLE scam_reports ADD COLUMN synced_at DATETIME",
            "ALTER TABLE scam_flags ADD COLUMN synced_to_cloud BOOLEAN DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN scam_synced_to_cloud BOOLEAN DEFAULT 0",
        ],
    ),
    (
        37,
        "Create scam_analytics table for aggregated detection metrics",
        [
            """CREATE TABLE IF NOT EXISTS scam_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period_start DATETIME NOT NULL,
                period_end DATETIME,
                synced_at DATETIME,
                total_scanned INTEGER DEFAULT 0,
                scam_detected INTEGER DEFAULT 0,
                safe_detected INTEGER DEFAULT 0,
                short_circuited_ml INTEGER DEFAULT 0,
                short_circuited_signatures INTEGER DEFAULT 0,
                pipeline_errors INTEGER DEFAULT 0,
                total_pipeline_ms INTEGER DEFAULT 0,
                categories_json TEXT,
                ml_safe INTEGER DEFAULT 0,
                ml_borderline INTEGER DEFAULT 0,
                ml_auto_scam INTEGER DEFAULT 0,
                ml_unavailable INTEGER DEFAULT 0,
                tool_calls_json TEXT,
                scenarios_matched_json TEXT,
                safe_patterns_matched_json TEXT,
                flags_dismissed INTEGER DEFAULT 0,
                messages_unflagged INTEGER DEFAULT 0,
                reports_submitted INTEGER DEFAULT 0,
                manual_checks INTEGER DEFAULT 0,
                rescans INTEGER DEFAULT 0,
                scans_private INTEGER DEFAULT 0,
                scans_group INTEGER DEFAULT 0,
                scans_channel INTEGER DEFAULT 0,
                system_info_json TEXT
            )""",
        ],
    ),
    (
        38,
        "Add scam_feedback table for user feedback",
        [
            """CREATE TABLE IF NOT EXISTS scam_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                feedback_type TEXT NOT NULL,
                message TEXT NOT NULL,
                related_chat_id BIGINT,
                related_message_id BIGINT,
                related_flag_telegram_id BIGINT,
                status TEXT DEFAULT 'pending',
                synced_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
        ],
    ),
    (
        39,
        "Add scam_dismissals table for queued dismissal sync",
        [
            """CREATE TABLE IF NOT EXISTS scam_dismissals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dismissal_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                synced_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""",
        ],
    ),
    (
        40,
        "Add scam_manual_verdict column to messages",
        [
            "ALTER TABLE messages ADD COLUMN scam_manual_verdict TEXT",
        ],
    ),
    (
        41,
        "Purge corrupted group/channel chat rows created under raw positive Telegram IDs "
        "(marked IDs for groups/channels are always negative)",
        [
            "DELETE FROM messages WHERE chat_id IN (SELECT telegram_id FROM chats WHERE telegram_id > 0 AND type IN ('group', 'channel'))",
            "DELETE FROM chat_tags WHERE chat_id IN (SELECT telegram_id FROM chats WHERE telegram_id > 0 AND type IN ('group', 'channel'))",
            "DELETE FROM catchup_items WHERE chat_telegram_id IN (SELECT telegram_id FROM chats WHERE telegram_id > 0 AND type IN ('group', 'channel'))",
            "DELETE FROM catchup_summaries WHERE chat_telegram_id IN (SELECT telegram_id FROM chats WHERE telegram_id > 0 AND type IN ('group', 'channel'))",
            "DELETE FROM chats WHERE telegram_id > 0 AND type IN ('group', 'channel')",
        ],
    ),
    (
        42,
        "Add is_deactivated column to chats (groups migrated to supergroups)",
        [
            "ALTER TABLE chats ADD COLUMN is_deactivated BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
    (
        43,
        "Create workspaces tables (tag-scoped contexts with kanban boards)",
        [
            "CREATE TABLE IF NOT EXISTS workspaces ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name TEXT NOT NULL, "
            "emoji TEXT, "
            "color TEXT DEFAULT 'indigo', "
            "match_mode TEXT NOT NULL DEFAULT 'any', "
            "view_mode TEXT NOT NULL DEFAULT 'list', "
            "position INTEGER NOT NULL DEFAULT 0, "
            "board_watermark DATETIME, "
            "created_at DATETIME DEFAULT (datetime('now')))",
            "CREATE TABLE IF NOT EXISTS workspace_tags ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "workspace_id INTEGER NOT NULL, "
            "tag_id INTEGER NOT NULL, "
            "is_exclusion BOOLEAN NOT NULL DEFAULT 0, "
            "UNIQUE(workspace_id, tag_id))",
            "CREATE TABLE IF NOT EXISTS workspace_columns ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "workspace_id INTEGER NOT NULL, "
            "tag_id INTEGER NOT NULL, "
            "position INTEGER NOT NULL DEFAULT 0, "
            "color TEXT, "
            "UNIQUE(workspace_id, tag_id))",
            "CREATE TABLE IF NOT EXISTS workspace_chat_overrides ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "workspace_id INTEGER NOT NULL, "
            "chat_id BIGINT NOT NULL, "
            "needs_triage_since DATETIME, "
            "triaged_at DATETIME, "
            "UNIQUE(workspace_id, chat_id))",
            "CREATE INDEX IF NOT EXISTS ix_workspace_tags_workspace ON workspace_tags (workspace_id)",
            "CREATE INDEX IF NOT EXISTS ix_workspace_columns_workspace ON workspace_columns (workspace_id)",
            "CREATE INDEX IF NOT EXISTS ix_workspace_overrides_workspace ON workspace_chat_overrides (workspace_id)",
        ],
    ),
    (
        44,
        "Add workspace_boards (multiple stream-scoped boards per workspace)",
        [
            "CREATE TABLE IF NOT EXISTS workspace_boards ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "workspace_id INTEGER NOT NULL, "
            "name TEXT NOT NULL, "
            "scope_tag_id INTEGER, "
            "position INTEGER NOT NULL DEFAULT 0, "
            "UNIQUE(workspace_id, name))",
            "CREATE INDEX IF NOT EXISTS ix_workspace_boards_workspace ON workspace_boards (workspace_id)",
            "ALTER TABLE workspace_columns ADD COLUMN board_id INTEGER",
            # Every kanban workspace (or any workspace that already has columns)
            # gets one unscoped 'Main' board that inherits the existing columns.
            "INSERT INTO workspace_boards (workspace_id, name, position) "
            "SELECT id, 'Main', 0 FROM workspaces "
            "WHERE view_mode = 'kanban' OR id IN (SELECT DISTINCT workspace_id FROM workspace_columns)",
            "UPDATE workspace_columns SET board_id = "
            "(SELECT wb.id FROM workspace_boards wb WHERE wb.workspace_id = workspace_columns.workspace_id)",
        ],
    ),
    (
        45,
        "Add label column to workspace_columns (display name decoupled from stage tag)",
        [
            "ALTER TABLE workspace_columns ADD COLUMN label TEXT",
            "UPDATE workspace_columns SET label = "
            "(SELECT name FROM tags WHERE tags.id = workspace_columns.tag_id)",
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
        await conn.exec_driver_sql("BEGIN")
        try:
            await _apply_statements(conn, version, stmts, fresh=fresh)
            await _record_migration(conn, version, description)
        except BaseException:
            await conn.rollback()
            raise
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
    """Core migrations plus per-area modules, ordered by version."""
    from backend.db.migrations import areas

    combined = list(MIGRATIONS)
    for mod in areas.MODULES:
        combined.extend(mod.MIGRATIONS)
    versions = [v for v, _, _ in combined]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Duplicate migration version numbers")
    return sorted(combined, key=lambda m: m[0])
