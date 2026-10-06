"""Intelligence/briefing/catch-up migrations. Versions 80-89 are reserved for this area.

Same rules as runner.MIGRATIONS: append-only (version, description, [sql]).
"""

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        80,
        "Briefing run lifecycle (status/error) and lookup indexes",
        [
            # running | done | partial | failed. Existing rows were complete runs.
            "ALTER TABLE briefing_runs ADD COLUMN status VARCHAR NOT NULL DEFAULT 'done'",
            "ALTER TABLE briefing_runs ADD COLUMN error TEXT",
            "ALTER TABLE briefing_runs ADD COLUMN finished_at DATETIME",
            "CREATE INDEX IF NOT EXISTS ix_briefing_entries_run ON briefing_entries (run_id)",
            "CREATE INDEX IF NOT EXISTS ix_conversation_messages_conv "
            "ON conversation_messages (conversation_id, created_at)",
        ],
    ),
    (
        81,
        "Catch-up summaries: flag rule-based fallbacks written while the LLM was unavailable",
        [
            "ALTER TABLE catchup_summaries ADD COLUMN is_degraded BOOLEAN NOT NULL DEFAULT 0",
        ],
    ),
]
