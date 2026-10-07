"""Telegram sync engine migrations. Versions 46-59 are reserved for this area.

Same rules as runner.MIGRATIONS: append-only (version, description, [sql]).
"""

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        46,
        "Keyset index for message windows ordered by (date, tg_message_id)",
        [
            "CREATE INDEX IF NOT EXISTS ix_messages_chat_date_tgid ON messages (chat_id, date, tg_message_id)",
        ],
    ),
    (
        47,
        "Queue chats whose stored formatted text must be re-fetched (markdown-garbled text/text_html)",
        [
            # Rows with text_html were formatted messages stored from msg.text
            # (markdown-unparsed) + raw-text entity offsets. The runtime's
            # repair task re-fetches exactly these ids through the new upsert
            # and deletes each chat from the queue when done.
            "CREATE TABLE IF NOT EXISTS sync_text_repair (chat_id BIGINT PRIMARY KEY)",
            "INSERT OR IGNORE INTO sync_text_repair (chat_id) "
            "SELECT DISTINCT chat_id FROM messages WHERE text_html IS NOT NULL AND text_html != ''",
        ],
    ),
    (
        48,
        "Also queue chats whose text has markdown markers but no HTML (compose-sent / fallback rows)",
        [
            "CREATE TABLE IF NOT EXISTS sync_text_repair (chat_id BIGINT PRIMARY KEY)",
            "INSERT OR IGNORE INTO sync_text_repair (chat_id) "
            "SELECT DISTINCT chat_id FROM messages WHERE text_html IS NOT NULL "
            "OR text LIKE '%**%' OR text LIKE '%\\_\\_%' ESCAPE '\\' OR text LIKE '%~~%' "
            "OR text LIKE '%`%' OR text LIKE '%](%'",
        ],
    ),
    (
        49,
        "History coverage per chat for the DM archive (absent row = nothing known)",
        [
            "CREATE TABLE IF NOT EXISTS chat_history_coverage ("
            "chat_id BIGINT NOT NULL PRIMARY KEY, "
            "history_complete BOOLEAN NOT NULL DEFAULT 0, "
            "oldest_tg_id BIGINT, "
            "newest_contiguous_tg_id BIGINT, "
            "walk_cursor_tg_id BIGINT, "
            "last_walk_at DATETIME, "
            "last_topup_at DATETIME, "
            "error TEXT, "
            "attempts INTEGER NOT NULL DEFAULT 0)",
        ],
    ),
    (
        50,
        "Chat flags from the dialog entity: bot, deleted account, Saved Messages",
        [
            "ALTER TABLE chats ADD COLUMN is_bot BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN is_deleted_account BOOLEAN NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN is_self BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
    (
        51,
        "Store every message kind: service rows, structured meta, forward source",
        [
            "ALTER TABLE messages ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'message'",
            "ALTER TABLE messages ADD COLUMN meta_json TEXT",
            "ALTER TABLE messages ADD COLUMN fwd_from_name VARCHAR",
        ],
    ),
]
