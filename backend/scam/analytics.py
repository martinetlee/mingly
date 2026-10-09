"""Scam detection analytics tracker.

Tracks counters in the scam_analytics table. A new row is created daily.
Counters are incremented on each event. Uploaded to cloud on sync.
"""

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import DateTime, bindparam, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.db.database import AsyncSessionLocal
from backend.db.models import ScamAnalytics

logger = logging.getLogger(__name__)

_tracker: Optional["AnalyticsTracker"] = None


def get_analytics_tracker() -> "AnalyticsTracker":
    global _tracker
    if _tracker is None:
        _tracker = AnalyticsTracker()
    return _tracker


_COUNTER_COLUMNS = (
    "total_scanned", "scam_detected", "safe_detected", "short_circuited_ml",
    "short_circuited_signatures", "pipeline_errors", "total_pipeline_ms",
    "ml_safe", "ml_borderline", "ml_auto_scam", "ml_unavailable",
    "flags_dismissed", "messages_unflagged", "reports_submitted", "manual_checks",
    "rescans", "scans_direct", "scans_group", "scans_channel",
)
_JSON_COLUMNS = ("categories_json", "tool_calls_json", "scenarios_matched_json", "safe_patterns_matched_json")
_JSON_KEY_RE = re.compile(r"[^A-Za-z0-9_.:\- ]")


def _today() -> datetime:
    return datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


class AnalyticsTracker:
    """Tracks scam detection analytics with daily rollover.

    Every write is a single atomic statement (``INSERT … ON CONFLICT DO
    UPDATE SET col = col + :n``) against the unique ``period_start`` index, so
    concurrent events never lose increments or create duplicate day rows.
    """

    async def increment(self, **counters: int) -> None:
        """Atomically add to one or more counters on today's row."""
        counters = {k: int(v) for k, v in counters.items() if k in _COUNTER_COLUMNS and v}
        if not counters:
            return
        table = ScamAnalytics.__table__
        values = {c: 0 for c in _COUNTER_COLUMNS}
        values.update(counters)
        stmt = sqlite_insert(table).values(period_start=_today(), **values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.period_start],
            set_={k: table.c[k] + stmt.excluded[k] for k in counters},
        )
        async with AsyncSessionLocal() as db:
            await db.execute(stmt)
            await db.commit()

    # Backwards-compatible name
    _increment = increment

    async def _increment_json_counter(self, field: str, key: str, amount: int = 1):
        """Atomically increment a counter inside a JSON field (e.g. categories_json)."""
        if field not in _JSON_COLUMNS or not key:
            return
        key = _JSON_KEY_RE.sub("", str(key))[:100]
        if not key:
            return
        today = _today()
        table = ScamAnalytics.__table__
        ensure = sqlite_insert(table).values(
            period_start=today, **{c: 0 for c in _COUNTER_COLUMNS}
        ).on_conflict_do_nothing(index_elements=[table.c.period_start])
        path = f'$."{key}"'
        async with AsyncSessionLocal() as db:
            await db.execute(ensure)
            await db.execute(
                text(
                    f"UPDATE scam_analytics SET {field} = json_set(COALESCE({field}, '{{}}'), :path, "
                    f"COALESCE(json_extract(COALESCE({field}, '{{}}'), :path), 0) + :n) "
                    f"WHERE period_start = :p"
                ).bindparams(bindparam("p", type_=DateTime)),
                {"path": path, "n": amount, "p": today},
            )
            await db.commit()

    # --- Event methods ---

    async def record_scan(self, is_scam: bool, category: Optional[str],
                          duration_ms: int, conversation_kind: Optional[str],
                          trace_status: str, ml_verdict: Optional[str] = None):
        """Record a completed scan."""
        counters = {
            "total_scanned": 1,
            "total_pipeline_ms": duration_ms,
        }

        if is_scam:
            counters["scam_detected"] = 1
        else:
            counters["safe_detected"] = 1

        if trace_status == "short_circuited":
            if ml_verdict == "safe":
                counters["short_circuited_ml"] = 1
            else:
                counters["short_circuited_signatures"] = 1
        elif trace_status == "failed":
            counters["pipeline_errors"] = 1

        # Conversation kind (no counter for email threads yet)
        if conversation_kind == "direct":
            counters["scans_direct"] = 1
        elif conversation_kind == "group":
            counters["scans_group"] = 1
        elif conversation_kind == "channel":
            counters["scans_channel"] = 1

        await self._increment(**counters)

        # Category breakdown
        if is_scam and category:
            await self._increment_json_counter("categories_json", category)

    async def record_ml_verdict(self, verdict: str):
        """Record ML prefilter verdict."""
        field_map = {
            "safe": "ml_safe",
            "borderline": "ml_borderline",
            "auto_scam": "ml_auto_scam",
            "unavailable": "ml_unavailable",
            "no_models": "ml_unavailable",
        }
        field = field_map.get(verdict)
        if field:
            await self._increment(**{field: 1})

    async def record_tool_call(self, tool_name: str):
        """Record a tool call."""
        await self._increment_json_counter("tool_calls_json", tool_name)

    async def record_scenario_match(self, scenario_name: str):
        """Record a matched scam scenario."""
        await self._increment_json_counter("scenarios_matched_json", scenario_name)

    async def record_safe_pattern_match(self, pattern_name: str):
        """Record a matched safe pattern."""
        await self._increment_json_counter("safe_patterns_matched_json", pattern_name)

    async def record_flag_dismissed(self):
        """Record user dismissing a scam flag."""
        await self._increment(flags_dismissed=1)

    async def record_message_unflagged(self):
        """Record user clearing a message's scam score."""
        await self._increment(messages_unflagged=1)

    async def record_report_submitted(self):
        """Record user submitting a scam report."""
        await self._increment(reports_submitted=1)

    async def record_manual_check(self):
        """Record user manually triggering 'Check for scam'."""
        await self._increment(manual_checks=1)

    async def record_rescan(self):
        """Record user triggering a rescan."""
        await self._increment(rescans=1)

    # --- Sync methods ---

    async def get_pending_periods(self) -> list[dict]:
        """Get analytics periods that haven't been synced to cloud."""
        async with AsyncSessionLocal() as db:
            # Only closed days: today's row is still being incremented, and
            # uploading it would freeze a partial count (later increments would
            # never reach the cloud once synced_at is set).
            result = await db.execute(
                select(ScamAnalytics).where(
                    ScamAnalytics.synced_at.is_(None),
                    ScamAnalytics.total_scanned > 0,
                    ScamAnalytics.period_start < _today(),
                ).order_by(ScamAnalytics.period_start.asc()).limit(30)
            )
            rows = result.scalars().all()

            return [{
                "period_start": r.period_start.isoformat(),
                "period_end": (r.period_start + timedelta(days=1)).isoformat(),
                "detection": {
                    "total_scanned": r.total_scanned,
                    "scam_detected": r.scam_detected,
                    "safe_detected": r.safe_detected,
                    "short_circuited_ml": r.short_circuited_ml,
                    "short_circuited_signatures": r.short_circuited_signatures,
                    "pipeline_errors": r.pipeline_errors,
                    "avg_pipeline_ms": round(r.total_pipeline_ms / max(r.total_scanned, 1)),
                },
                "categories": json.loads(r.categories_json or "{}"),
                "ml_prefilter": {
                    "safe": r.ml_safe,
                    "borderline": r.ml_borderline,
                    "auto_scam": r.ml_auto_scam,
                    "unavailable": r.ml_unavailable,
                },
                "tool_calls": json.loads(r.tool_calls_json or "{}"),
                "scenarios_matched": json.loads(r.scenarios_matched_json or "{}"),
                "safe_patterns_matched": json.loads(r.safe_patterns_matched_json or "{}"),
                "user_actions": {
                    "flags_dismissed": r.flags_dismissed,
                    "messages_unflagged": r.messages_unflagged,
                    "reports_submitted": r.reports_submitted,
                    "manual_checks": r.manual_checks,
                    "rescans": r.rescans,
                },
                # Current cloud contract: "chat_types" with "private" = direct
                "chat_types": {
                    "private": r.scans_direct,
                    "group": r.scans_group,
                    "channel": r.scans_channel,
                },
                "system": json.loads(r.system_info_json or "{}"),
            } for r in rows]

    async def mark_synced(self, period_starts: list[str]):
        """Mark analytics periods (as returned by get_pending_periods) synced."""
        starts = [datetime.fromisoformat(ps) for ps in period_starts]
        if not starts:
            return
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ScamAnalytics)
                .where(ScamAnalytics.period_start.in_(starts))
                .values(synced_at=datetime.utcnow())
            )
            await db.commit()
