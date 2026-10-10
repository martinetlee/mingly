"""Scam detection orchestration service.

Manages the lifecycle of scam check requests: persistence, API calls,
follow-up handling, and broadcasting results via WebSocket.

Invariants:
* A request judges only ``ScanRequest.targets``; context is never scored and
  context senders are never flagged.
* A failed check never touches ``scam_score``/``scam_reason``: the error goes
  to ``scam_error`` and a row with no earlier successful scan becomes
  ``scam_status='failed'`` with a retry backoff.
* Exactly one terminal WS event per check (``scam_check_error`` |
  ``scam_check_safe`` | ``scam_detected``), broadcast after the commit and
  carrying the stored row fields.
* Automated flagging never re-activates a sender the user dismissed.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.db.database import AsyncSessionLocal
from backend.db.models import SCAM_SUCCESS_STATUSES, Conversation, Message, ScamCheckRequest, ScamFlag
from backend.scam.channel_ids import telegram_scan_ids, telegram_user_ids
from backend.scam.requests import build_scan_request
from backend.scam.scan import (
    MAX_AUTO_ATTEMPTS, RETRY_BACKOFF, FollowUpRequest, ScamAPIClient, ScamCheckResult, ScanRequest,
)
from backend.scam.verdict import FLAG_ACTIVE, ws_message_entry
from backend.ws.manager import ws_manager
from backend.db.results import affected_rows

logger = logging.getLogger(__name__)

# Requests older than this are expired on startup
_EXPIRY_HOURS = 24
# Finished requests are pruned after this many days (payloads are compact, but
# the table would still grow by one row per scanned message forever)
_PRUNE_DAYS = 7

_service: Optional["ScamDetectionService"] = None


def get_scam_service() -> Optional["ScamDetectionService"]:
    return _service


def init_scam_service(client: ScamAPIClient) -> "ScamDetectionService":
    global _service
    _service = ScamDetectionService(client)
    return _service


def _trace_status(result: ScamCheckResult) -> str:
    if result.trace_json:
        try:
            status = json.loads(result.trace_json).get("status")
        except (ValueError, TypeError):
            status = None
        if status in SCAM_SUCCESS_STATUSES:
            return status
    return "completed"


def _score(result: ScamCheckResult) -> float:
    # scam_score encodes verdict and confidence: scam 0.5-1.0, safe 0.0-0.5,
    # so "> 0.5" stays the pipeline-verdict threshold.
    conf = max(0.0, min(1.0, float(result.confidence or 0.0)))
    return 0.5 + conf * 0.5 if result.is_scam else 0.5 - conf * 0.5


class ScamDetectionService:
    def __init__(self, client: ScamAPIClient):
        self._client = client

    def set_client(self, client: ScamAPIClient) -> None:
        """Swap the detection backend (settings changed or the vault unlocked)."""
        self._client = client

    # ------------------------------------------------------------------
    # Sender lookups
    # ------------------------------------------------------------------

    async def check_flagged_users(self, identity_ids: list[int]) -> None:
        """Check a batch of senders against the flagged scams database. The
        lookup is keyed by Telegram user id, so only Telegram identities are
        checked; a batch with none of them completes with no flags."""
        identity_ids = list(dict.fromkeys(int(i) for i in identity_ids if i))
        if not identity_ids:
            return

        request_id = str(uuid.uuid4())
        now = datetime.utcnow()
        async with AsyncSessionLocal() as db:
            db.add(ScamCheckRequest(
                request_id=request_id,
                request_type="are_flagged_scams",
                status="pending",
                payload_json=json.dumps({"identity_ids": identity_ids}),
                related_identity_ids_json=json.dumps(identity_ids),
                created_at=now,
                updated_at=now,
            ))
            await db.commit()
            tg_users = await telegram_user_ids(db, identity_ids)

        try:
            results = await self._are_flagged(tg_users) if tg_users else {}
            await self._apply_user_flags(request_id, results)
        except Exception as e:
            logger.error("areFlaggedScams failed: %s", e)
            await self._mark_request(request_id, "failed")

    async def _are_flagged(self, tg_users: dict[int, int]) -> dict[int, ScamCheckResult]:
        """``are_flagged_scams`` for {identity_id: Telegram user id}; results
        keyed back by identity_id."""
        by_user = {uid: iid for iid, uid in tg_users.items()}
        results = await self._client.are_flagged_scams(list(by_user))
        return {by_user[uid]: r for uid, r in results.items() if uid in by_user}

    # ------------------------------------------------------------------
    # Message checks
    # ------------------------------------------------------------------

    async def check_messages_for_scam(self, req: ScanRequest, source: str = "manual") -> None:
        """Judge ``req.targets``. Always ends with exactly one terminal WS event."""
        if not req.targets:
            return

        request_id = str(uuid.uuid4())
        now = datetime.utcnow()
        async with AsyncSessionLocal() as db:
            db.add(ScamCheckRequest(
                request_id=request_id,
                request_type="is_this_scam",
                status="pending",
                # Compact: the request is rebuilt from the DB on resume, so the
                # message texts/profiles are not duplicated here.
                payload_json=json.dumps({
                    "conversation_id": req.conversation_id,
                    "target_ids": req.target_ids,
                    "context_ids": [m["message_id"] for m in req.context],
                    "source": source,
                }),
                related_conversation_id=req.conversation_id,
                related_message_ids_json=json.dumps(req.target_ids),
                related_identity_ids_json=json.dumps(req.target_identity_ids),
                created_at=now,
                updated_at=now,
            ))
            await db.commit()

        try:
            result = await self._client.is_this_scam(request_id, req)
        except Exception as e:
            logger.error("isThisScam failed for conversation %s: %s", req.conversation_id, e)
            result = ScamCheckResult(is_scam=False, confidence=0.0, reason="",
                                     error=f"Scam detection failed: {e or type(e).__name__}")
        await self._apply_result(request_id, req, result, source=source)

    async def _apply_result(
        self, request_id: str, req: ScanRequest, result: ScamCheckResult, *, source: str
    ) -> None:
        now = datetime.utcnow()
        target_ids = req.target_ids
        new_flags: list[int] = []
        failed = bool(result.error)

        async with AsyncSessionLocal() as db:
            rows = (await db.execute(
                select(Message).where(Message.conversation_id == req.conversation_id, Message.id.in_(target_ids))
            )).scalars().all()

            if failed:
                for m in rows:
                    m.scam_error = (result.error or "")[:2000]
                    m.scam_attempts = (m.scam_attempts or 0) + 1
                    if m.scam_status in SCAM_SUCCESS_STATUSES:
                        continue  # keep the earlier good result; just record the error
                    m.scam_status = "failed"
                    m.scam_trace_json = result.trace_json
                    step = min(m.scam_attempts, len(RETRY_BACKOFF)) - 1
                    m.scam_next_retry_at = (
                        now + RETRY_BACKOFF[step] if m.scam_attempts < MAX_AUTO_ATTEMPTS else None
                    )
                    m.scam_updated_at = now
                    m.scam_synced_to_cloud = False
            else:
                status = _trace_status(result)
                for m in rows:
                    m.scam_score = _score(result)
                    m.scam_reason = result.reason
                    m.scam_category = result.category
                    m.scam_llm_prompt = result.llm_prompt
                    m.scam_llm_response = result.llm_response
                    m.scam_trace_json = result.trace_json
                    m.scam_status = status
                    m.scam_error = None
                    m.scam_attempts = (m.scam_attempts or 0) + 1
                    m.scam_next_retry_at = None
                    m.scam_updated_at = now
                    m.scam_synced_to_cloud = False
                await db.flush()

                # Flag only target senders whose target message is (effectively)
                # scam. A sender with any existing record — active, or dismissed
                # by the user — is left as is.
                for sid in req.target_identity_ids:
                    trigger = next((m for m in rows if m.sender_identity_id == sid and m.effective_is_scam), None)
                    if trigger is None:
                        continue
                    inserted = await db.execute(
                        sqlite_insert(ScamFlag.__table__).values(
                            identity_id=sid,
                            source="local",
                            status=FLAG_ACTIVE,
                            confidence=result.confidence,
                            reason=result.reason,
                            category=result.category,
                            request_id=request_id,
                            flagged_message_text=(trigger.text or "")[:500],
                            flagged_conversation_id=trigger.conversation_id,
                            flagged_message_id=trigger.id,
                            flagged_at=now,
                            updated_at=now,
                            synced_to_cloud=False,
                        ).on_conflict_do_nothing(index_elements=["identity_id"])
                    )
                    if affected_rows(inserted):
                        new_flags.append(sid)

            await db.execute(
                update(ScamCheckRequest)
                .where(ScamCheckRequest.request_id == request_id)
                .values(
                    status="failed" if failed else "completed",
                    response_json=json.dumps(
                        {"error": result.error} if failed else
                        {"is_scam": result.is_scam, "confidence": result.confidence, "reason": result.reason}
                    ),
                    completed_at=None if failed else now,
                    updated_at=now,
                )
            )
            await db.commit()
            entries = [ws_message_entry(m) for m in rows]

        logger.info(
            "Scam check %s conversation=%s targets=%s source=%s -> %s",
            request_id, req.conversation_id, target_ids, source,
            f"error: {result.error}" if failed else f"is_scam={result.is_scam} conf={result.confidence}",
        )

        # --- Exactly one terminal event, after the commit ---
        event = {
            "request_id": request_id,
            "source": source,
            "conversation_id": req.conversation_id,
            "message_ids": target_ids,
            "identity_ids": req.target_identity_ids,
            "messages": entries,
        }
        if failed:
            event.update(type="scam_check_error", error=result.error)
        else:
            detected = any(e["effective_is_scam"] for e in entries)
            event.update(
                type="scam_detected" if detected else "scam_check_safe",
                pipeline_is_scam=result.is_scam,
                confidence=result.confidence,
                reason=result.reason,
                category=result.category,
            )
        await ws_manager.broadcast(event)
        if new_flags:
            await ws_manager.broadcast({"type": "scam_users_flagged", "identity_ids": new_flags})

        await self._record_scan_analytics(result, req.conversation_kind)

        # Check if flagged senders are also known scammers upstream (a
        # Telegram-user-id lookup, so Telegram senders only)
        tg_users = {m["identity_id"]: m["telegram_user_id"] for m in req.targets
                    if m.get("identity_id") and m.get("telegram_user_id")}
        if not failed and result.is_scam and tg_users:
            try:
                flagged = await self._are_flagged(tg_users)
                if flagged:
                    await self._apply_user_flags(request_id, flagged, mark_request=False)
            except Exception as e:
                logger.warning("are_flagged_scams check failed: %s", e)

    async def _apply_user_flags(
        self, request_id: str, results: dict[int, ScamCheckResult], mark_request: bool = True
    ) -> None:
        """Create scam flags for flagged senders ({identity_id: result}),
        never overriding an existing record, including a user dismissal."""
        now = datetime.utcnow()
        flagged_ids: list[int] = []
        async with AsyncSessionLocal() as db:
            for iid, result in results.items():
                if not result.is_scam:
                    continue
                trigger = (await db.execute(
                    select(Message).where(Message.sender_identity_id == iid)
                    .order_by(Message.date.desc(), Message.id.desc()).limit(1)
                )).scalars().first()
                inserted = await db.execute(
                    sqlite_insert(ScamFlag.__table__).values(
                        identity_id=iid,
                        source="cloud",
                        status=FLAG_ACTIVE,
                        confidence=result.confidence,
                        reason=result.reason,
                        category=result.category,
                        request_id=request_id,
                        flagged_message_text=(trigger.text or "")[:500] if trigger else None,
                        flagged_conversation_id=trigger.conversation_id if trigger else None,
                        flagged_message_id=trigger.id if trigger else None,
                        flagged_at=now,
                        updated_at=now,
                        synced_to_cloud=False,
                    ).on_conflict_do_nothing(index_elements=["identity_id"])
                )
                if affected_rows(inserted):
                    flagged_ids.append(iid)
            await db.commit()

        if mark_request:
            await self._mark_request(request_id, "completed")
        if flagged_ids:
            await ws_manager.broadcast({"type": "scam_users_flagged", "identity_ids": flagged_ids})

    # ------------------------------------------------------------------
    # External API callbacks / follow-ups
    # ------------------------------------------------------------------

    async def _request_from_row(self, req: ScamCheckRequest) -> Optional[ScanRequest]:
        target_ids = json.loads(req.related_message_ids_json or "[]")
        if req.related_conversation_id is None or not target_ids:
            return None
        return await build_scan_request(req.related_conversation_id, target_ids)

    def _source_of(self, req: ScamCheckRequest) -> str:
        try:
            return json.loads(req.payload_json).get("source") or "callback"
        except (ValueError, TypeError, AttributeError):
            return "callback"

    async def handle_api_response(self, request_id: str, response: dict) -> None:
        """Handle a callback from the external API with a verdict or follow-up request."""
        async with AsyncSessionLocal() as db:
            req = (await db.execute(
                select(ScamCheckRequest).where(ScamCheckRequest.request_id == request_id)
            )).scalars().first()
            if not req:
                logger.warning("Unknown scam check request: %s", request_id)
                return

            if response.get("status") == "need_more_data":
                req.status = "awaiting_followup"
                req.followup_request_json = json.dumps(response)
                req.updated_at = datetime.utcnow()
                await db.commit()
                await self._handle_followup(req, response)
                return

        check_result = ScamCheckResult(
            is_scam=response.get("is_scam", False),
            confidence=response.get("confidence", 0.0),
            reason=response.get("reason", ""),
            raw_response=response,
        )
        if req.request_type == "are_flagged_scams":
            identity_ids = json.loads(req.related_identity_ids_json or "[]")
            flagged = {iid: check_result for iid in identity_ids} if check_result.is_scam else {}
            await self._apply_user_flags(request_id, flagged)
            return
        scan_req = await self._request_from_row(req)
        if scan_req is None:
            await self._mark_request(request_id, "failed")
            return
        await self._apply_result(request_id, scan_req, check_result, source=self._source_of(req))

    async def resume_pending_requests(self) -> list[tuple[int, list[int]]]:
        """On startup: prune old finished requests, expire stale ones and
        settle what can be settled here (external results that arrived,
        flagged-user lookups). Returns the message checks to run again as
        ``(conversation_id, message_ids)``; the scan queue re-enqueues them
        (``queue.resume_pending_checks``)."""
        now = datetime.utcnow()
        cutoff = now - timedelta(hours=_EXPIRY_HOURS)

        async with AsyncSessionLocal() as db:
            await db.execute(
                delete(ScamCheckRequest).where(
                    ScamCheckRequest.status.in_(["completed", "failed", "expired", "superseded"]),
                    ScamCheckRequest.created_at < now - timedelta(days=_PRUNE_DAYS),
                )
            )
            await db.execute(
                update(ScamCheckRequest)
                .where(
                    ScamCheckRequest.status.in_(["pending", "awaiting_followup"]),
                    ScamCheckRequest.created_at < cutoff,
                )
                .values(status="expired", updated_at=now)
            )
            await db.commit()

            pending = (await db.execute(
                select(ScamCheckRequest).where(
                    ScamCheckRequest.status.in_(["pending", "awaiting_followup"]),
                    ScamCheckRequest.created_at >= cutoff,
                )
            )).scalars().all()

        requeue: list[tuple[int, list[int]]] = []
        for req in pending:
            try:
                # Fallback if an external callback was missed
                polled = await self._client.get_result(req.request_id)
                if polled is not None:
                    await self.handle_api_response(req.request_id, {
                        "is_scam": polled.is_scam, "confidence": polled.confidence, "reason": polled.reason,
                    })
                    continue

                if req.request_type == "are_flagged_scams":
                    await self._mark_request(req.request_id, "superseded")
                    payload = json.loads(req.payload_json or "{}")
                    await self.check_flagged_users(payload.get("identity_ids") or [])
                elif req.related_conversation_id is not None:
                    await self._mark_request(req.request_id, "superseded")
                    requeue.append((req.related_conversation_id, json.loads(req.related_message_ids_json or "[]")))
            except Exception as e:
                logger.error("Failed to resume scam check %s: %s", req.request_id, e)
                await self._mark_request(req.request_id, "failed")
        return requeue

    async def _handle_followup(self, req: ScamCheckRequest, followup: dict) -> None:
        """Gather data requested by external API and respond."""
        data_type = followup.get("data_type", "")
        params = followup.get("params", {})

        gathered_data = None
        if data_type == "chat_history" and req.related_conversation_id:
            async with AsyncSessionLocal() as db:
                msgs = (await db.execute(
                    select(Message)
                    .where(Message.conversation_id == req.related_conversation_id)
                    .order_by(Message.date.desc(), Message.id.desc())
                    .limit(min(int(params.get("limit", 100)), 500))
                )).scalars().all()
                # The external API's v1 shape: Telegram message and sender ids
                # (other channels have none: internal message ids, no sender)
                conv = await db.get(Conversation, req.related_conversation_id)
                tg = await telegram_scan_ids(db, conv, msgs)
                gathered_data = [
                    {"message_id": tg.message_ids[m.id] if tg else m.id,
                     "sender_id": tg.senders[m.id] if tg else None,
                     "sender_name": m.sender_name, "text": m.text, "date": str(m.date)}
                    for m in msgs
                ]

        if gathered_data is None:
            return
        try:
            response = await self._client.respond_to_followup(req.request_id, gathered_data)
            if isinstance(response, FollowUpRequest):
                async with AsyncSessionLocal() as db:
                    await db.execute(
                        update(ScamCheckRequest)
                        .where(ScamCheckRequest.request_id == req.request_id)
                        .values(
                            followup_request_json=json.dumps(
                                {"data_type": response.data_type, "params": response.params}
                            ),
                            updated_at=datetime.utcnow(),
                        )
                    )
                    await db.commit()
                await self._handle_followup(req, {"data_type": response.data_type, "params": response.params})
            elif isinstance(response, ScamCheckResult):
                scan_req = await self._request_from_row(req)
                if scan_req is not None:
                    await self._apply_result(req.request_id, scan_req, response, source=self._source_of(req))
        except Exception as e:
            logger.error("Follow-up response failed for %s: %s", req.request_id, e)
            await self._mark_request(req.request_id, "failed")

    # ------------------------------------------------------------------
    # Analytics / bookkeeping
    # ------------------------------------------------------------------

    async def _record_scan_analytics(self, result: ScamCheckResult, conversation_kind: Optional[str] = None):
        try:
            from backend.scam.analytics import get_analytics_tracker
            tracker = get_analytics_tracker()

            trace: dict = {}
            if result.trace_json:
                try:
                    trace = json.loads(result.trace_json) or {}
                except (ValueError, TypeError):
                    trace = {}

            if result.error:
                await tracker.increment(pipeline_errors=1)
                return

            ml_verdict = None
            for step in trace.get("steps", []):
                output = step.get("output") or {}
                if step.get("node_id") == "ml_prefilter" and output:
                    ml_verdict = output.get("verdict")
                for call in step.get("tool_calls") or []:
                    await tracker.record_tool_call(call.get("tool", "unknown"))
                if step.get("node_id") == "classify":
                    if output.get("matched_scenario") not in (None, "", "null"):
                        await tracker.record_scenario_match(str(output["matched_scenario"]))
                    if output.get("matched_safe_pattern") not in (None, "", "null"):
                        await tracker.record_safe_pattern_match(str(output["matched_safe_pattern"]))

            await tracker.record_scan(
                is_scam=result.is_scam,
                category=result.category,
                duration_ms=int(trace.get("duration_ms") or 0),
                conversation_kind=conversation_kind,
                trace_status=trace.get("status", "completed"),
                ml_verdict=ml_verdict,
            )
            if ml_verdict:
                await tracker.record_ml_verdict(ml_verdict)
        except Exception as e:
            logger.debug("Failed to record scan analytics: %s", e)

    async def _mark_request(self, request_id: str, status: str) -> None:
        now = datetime.utcnow()
        values: dict = {"status": status, "updated_at": now}
        if status == "completed":
            values["completed_at"] = now
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ScamCheckRequest).where(ScamCheckRequest.request_id == request_id).values(**values)
            )
            await db.commit()
