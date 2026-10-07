"""Scam/Shield migrations. Versions 60-79 are reserved for this area.

Same rules as runner.MIGRATIONS: append-only (version, description, [sql]).
"""

# Sums duplicate scam_analytics rows into the lowest id per period_start.
_INT_COUNTERS = [
    "total_scanned", "scam_detected", "safe_detected", "short_circuited_ml",
    "short_circuited_signatures", "pipeline_errors", "total_pipeline_ms",
    "ml_safe", "ml_borderline", "ml_auto_scam", "ml_unavailable",
    "flags_dismissed", "messages_unflagged", "reports_submitted", "manual_checks",
    "rescans", "scans_private", "scans_group", "scans_channel",
]
_JSON_COUNTERS = [
    "categories_json", "tool_calls_json", "scenarios_matched_json", "safe_patterns_matched_json",
]
_DUP_ROW = (
    "id = (SELECT MIN(d.id) FROM scam_analytics d WHERE d.period_start = scam_analytics.period_start) "
    "AND (SELECT COUNT(*) FROM scam_analytics d WHERE d.period_start = scam_analytics.period_start) > 1"
)
_MERGE_ANALYTICS = (
    "UPDATE scam_analytics SET "
    + ", ".join(
        f"{c} = (SELECT SUM(b.{c}) FROM scam_analytics b WHERE b.period_start = scam_analytics.period_start)"
        for c in _INT_COUNTERS
    )
    + ", "
    + ", ".join(
        f"{c} = (SELECT json_group_object(k, n) FROM (SELECT j.key AS k, SUM(j.value) AS n "
        f"FROM scam_analytics b, json_each(COALESCE(b.{c}, '{{}}')) j "
        f"WHERE b.period_start = scam_analytics.period_start GROUP BY j.key))"
        for c in _JSON_COUNTERS
    )
    + " WHERE " + _DUP_ROW
)

_TRACE_STATUS = (
    "CASE WHEN json_valid(scam_trace_json) THEN json_extract(scam_trace_json, '$.status') END"
)

MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        60,
        "Scam scan status model: status/error/retry/change-tracking columns, backfill from traces",
        [
            "ALTER TABLE messages ADD COLUMN scam_status VARCHAR",
            "ALTER TABLE messages ADD COLUMN scam_error TEXT",
            "ALTER TABLE messages ADD COLUMN scam_attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN scam_next_retry_at DATETIME",
            "ALTER TABLE messages ADD COLUMN scam_updated_at DATETIME",
            "ALTER TABLE messages ADD COLUMN scam_synced_at DATETIME",
            # Failed scans were stored as score 0.5 + error-as-reason ("safe").
            # Move the error out and drop the fake score.
            f"UPDATE messages SET scam_status = 'failed', scam_error = scam_reason, "
            f"scam_reason = NULL, scam_score = NULL, scam_attempts = 1 "
            f"WHERE scam_trace_json IS NOT NULL AND {_TRACE_STATUS} = 'failed'",
            f"UPDATE messages SET scam_status = 'short_circuited' "
            f"WHERE scam_status IS NULL AND scam_score IS NOT NULL AND {_TRACE_STATUS} = 'short_circuited'",
            "UPDATE messages SET scam_status = 'completed' "
            "WHERE scam_status IS NULL AND scam_score IS NOT NULL",
            "UPDATE messages SET scam_updated_at = created_at "
            "WHERE scam_status IS NOT NULL OR scam_manual_verdict IS NOT NULL",
            "UPDATE messages SET scam_synced_at = scam_updated_at WHERE scam_synced_to_cloud = 1",
            "CREATE INDEX IF NOT EXISTS ix_messages_scam_status ON messages (scam_status)",
            "CREATE INDEX IF NOT EXISTS ix_messages_date_id ON messages (date DESC, id DESC)",
        ],
    ),
    (
        61,
        "Per-sender scam verdict record: scam_flags.status/dismissed_at/updated_at",
        [
            "ALTER TABLE scam_flags ADD COLUMN status VARCHAR NOT NULL DEFAULT 'active'",
            "ALTER TABLE scam_flags ADD COLUMN dismissed_at DATETIME",
            "ALTER TABLE scam_flags ADD COLUMN updated_at DATETIME",
            "UPDATE scam_flags SET updated_at = flagged_at WHERE updated_at IS NULL",
        ],
    ),
    (
        62,
        "scam_analytics: merge duplicate day rows, unique period_start",
        [
            _MERGE_ANALYTICS,
            "DELETE FROM scam_analytics WHERE id NOT IN (SELECT MIN(id) FROM scam_analytics GROUP BY period_start)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_scam_analytics_period ON scam_analytics (period_start)",
        ],
    ),
    (
        63,
        "Prune finished scam_check_requests older than 7 days",
        [
            "DELETE FROM scam_check_requests WHERE status IN ('completed', 'failed', 'expired') "
            "AND created_at < datetime('now', '-7 days')",
        ],
    ),
]
