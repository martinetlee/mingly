"""Scam detection orchestration service.

Manages the lifecycle of scam check requests: persistence, API calls,
follow-up handling, and broadcasting results via WebSocket.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update

from backend.db.database import AsyncSessionLocal
from backend.db.models import Message, ScamCheckRequest, ScamFlag
from backend.scam.client import ScamAPIClient, ScamCheckResult, FollowUpRequest
from backend.ws.manager import ws_manager

logger = logging.getLogger(__name__)

# Requests older than this are expired on startup
_EXPIRY_HOURS = 24

_service: Optional["ScamDetectionService"] = None


def get_scam_service() -> Optional["ScamDetectionService"]:
    return _service


def init_scam_service(client: ScamAPIClient) -> "ScamDetectionService":
    global _service
    _service = ScamDetectionService(client)
    return _service


class ScamDetectionService:
    def __init__(self, client: ScamAPIClient):
        self._client = client

    async def check_flagged_users(self, telegram_ids: list[int]) -> None:
        """Check a batch of telegram IDs against the flagged scams database."""
        if not telegram_ids:
            return

        request_id = str(uuid.uuid4())
        now = datetime.utcnow()

        # Persist request before calling API
        async with AsyncSessionLocal() as db:
            req = ScamCheckRequest(
                request_id=request_id,
                request_type="are_flagged_scams",
                status="pending",
                payload_json=json.dumps({"telegram_ids": telegram_ids}),
                related_telegram_ids_json=json.dumps(telegram_ids),
                created_at=now,
                updated_at=now,
            )
            db.add(req)
            await db.commit()

        try:
            results = await self._client.are_flagged_scams(telegram_ids)
            await self._apply_user_flags(request_id, results)
        except Exception as e:
            logger.error("areFlaggedScams failed: %s", e)
            await self._mark_request(request_id, "failed")

    async def check_messages_for_scam(
        self,
        chat_id: int,
        messages: list[dict],
        user_telegram_id: int,
        profiles: Optional[list[dict]] = None,
        chat_metadata: Optional[dict] = None,
    ) -> None:
        """Analyze messages in a chat for scam patterns."""
        if not messages:
            return

        request_id = str(uuid.uuid4())
        now = datetime.utcnow()
        msg_ids = [m.get("message_id") for m in messages if m.get("message_id")]
        sender_ids = list({m.get("sender_id") for m in messages if m.get("sender_id")})

        payload = {
            "request_id": request_id,
            "messages": messages,
            "user_telegram_id": user_telegram_id,
            "profiles": profiles or [],
            "chat_metadata": chat_metadata or {},
        }

        # Persist request
        async with AsyncSessionLocal() as db:
            req = ScamCheckRequest(
                request_id=request_id,
                request_type="is_this_scam",
                status="pending",
                payload_json=json.dumps(payload, default=str),
                related_chat_id=chat_id,
                related_message_ids_json=json.dumps(msg_ids),
                related_telegram_ids_json=json.dumps(sender_ids),
                created_at=now,
                updated_at=now,
            )
            db.add(req)
            await db.commit()

        try:
            result = await self._client.is_this_scam(
                request_id, messages, user_telegram_id, profiles or [], chat_metadata or {}
            )
            if result.error:
                # Client returned an error result — broadcast it so frontend knows
                await self._broadcast_error(chat_id, msg_ids, result.error)
                await self._mark_request(request_id, "failed")
            else:
                await self._apply_message_result(request_id, chat_id, msg_ids, sender_ids, result)
        except Exception as e:
            logger.error("isThisScam failed for chat %d: %s", chat_id, e)
            await self._broadcast_error(chat_id, msg_ids, f"Scam detection failed: {e}")
            await self._mark_request(request_id, "failed")

    async def handle_api_response(self, request_id: str, response: dict) -> None:
        """Handle a callback from the external API with a verdict or follow-up request."""
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ScamCheckRequest).where(ScamCheckRequest.request_id == request_id)
            )
            req = result.scalars().first()
            if not req:
                logger.warning("Unknown scam check request: %s", request_id)
                return

            if response.get("status") == "need_more_data":
                req.status = "awaiting_followup"
                req.followup_request_json = json.dumps(response)
                req.updated_at = datetime.utcnow()
                await db.commit()
                # Gather follow-up data and respond
                await self._handle_followup(req, response)
                return

            # Final verdict
            check_result = ScamCheckResult(
                is_scam=response.get("is_scam", False),
                confidence=response.get("confidence", 0.0),
                reason=response.get("reason", ""),
                raw_response=response,
            )

            if req.request_type == "are_flagged_scams":
                tg_ids = json.loads(req.related_telegram_ids_json or "[]")
                flagged = {}
                if check_result.is_scam:
                    for tid in tg_ids:
                        flagged[tid] = check_result
                await self._apply_user_flags(request_id, flagged)
            else:
                msg_ids = json.loads(req.related_message_ids_json or "[]")
                sender_ids = json.loads(req.related_telegram_ids_json or "[]")
                await self._apply_message_result(
                    request_id, req.related_chat_id, msg_ids, sender_ids, check_result
                )

    async def resume_pending_requests(self) -> None:
        """On startup, re-issue recent pending requests and expire old ones."""
        cutoff = datetime.utcnow() - timedelta(hours=_EXPIRY_HOURS)

        async with AsyncSessionLocal() as db:
            # Expire old requests
            await db.execute(
                update(ScamCheckRequest)
                .where(
                    ScamCheckRequest.status.in_(["pending", "awaiting_followup"]),
                    ScamCheckRequest.created_at < cutoff,
                )
                .values(status="expired", updated_at=datetime.utcnow())
            )
            await db.commit()

            # Re-issue recent pending requests
            result = await db.execute(
                select(ScamCheckRequest).where(
                    ScamCheckRequest.status.in_(["pending", "awaiting_followup"]),
                    ScamCheckRequest.created_at >= cutoff,
                )
            )
            pending = result.scalars().all()

        for req in pending:
            try:
                payload = json.loads(req.payload_json)

                # Try polling for result first (fallback if callback was missed)
                polled = await self._client.get_result(req.request_id)
                if polled is not None:
                    if req.request_type == "are_flagged_scams":
                        tg_ids = json.loads(req.related_telegram_ids_json or "[]")
                        flagged = {tid: polled for tid in tg_ids} if polled.is_scam else {}
                        await self._apply_user_flags(req.request_id, flagged)
                    else:
                        msg_ids = json.loads(req.related_message_ids_json or "[]")
                        sender_ids = json.loads(req.related_telegram_ids_json or "[]")
                        await self._apply_message_result(
                            req.request_id, req.related_chat_id, msg_ids, sender_ids, polled
                        )
                    continue

                # No result yet — re-issue the request
                if req.request_type == "are_flagged_scams":
                    results = await self._client.are_flagged_scams(payload["telegram_ids"])
                    await self._apply_user_flags(req.request_id, results)
                elif req.request_type == "is_this_scam":
                    result = await self._client.is_this_scam(
                        req.request_id,
                        payload["messages"],
                        payload["user_telegram_id"],
                        payload.get("profiles", []),
                        payload.get("chat_metadata", {}),
                    )
                    msg_ids = json.loads(req.related_message_ids_json or "[]")
                    sender_ids = json.loads(req.related_telegram_ids_json or "[]")
                    await self._apply_message_result(
                        req.request_id, req.related_chat_id, msg_ids, sender_ids, result
                    )
            except Exception as e:
                logger.error("Failed to resume scam check %s: %s", req.request_id, e)
                await self._mark_request(req.request_id, "failed")

    async def _apply_user_flags(
        self, request_id: str, results: dict[int, ScamCheckResult]
    ) -> None:
        """Create scam flags for flagged users and mark request completed."""
        now = datetime.utcnow()
        async with AsyncSessionLocal() as db:
            for tid, result in results.items():
                if not result.is_scam:
                    continue
                existing = await db.execute(
                    select(ScamFlag).where(ScamFlag.telegram_id == tid)
                )
                if existing.scalars().first():
                    continue
                flag = ScamFlag(
                    telegram_id=tid,
                    source="external_api",
                    confidence=result.confidence,
                    reason=result.reason,
                    request_id=request_id,
                    flagged_at=now,
                )
                db.add(flag)
            await db.commit()

        await self._mark_request(request_id, "completed")

        # Broadcast flagged users
        flagged_ids = [tid for tid, r in results.items() if r.is_scam]
        if flagged_ids:
            await ws_manager.broadcast({
                "type": "scam_users_flagged",
                "telegram_ids": flagged_ids,
            })

    async def _apply_message_result(
        self,
        request_id: str,
        chat_id: Optional[int],
        msg_ids: list[int],
        sender_ids: list[int],
        result: ScamCheckResult,
    ) -> None:
        """Apply scam verdict to messages and flag senders if scam detected."""
        now = datetime.utcnow()
        async with AsyncSessionLocal() as db:
            if result.is_scam and msg_ids and chat_id:
                # Update scam_score on the checked messages
                await db.execute(
                    update(Message)
                    .where(
                        Message.chat_id == chat_id,
                        Message.tg_message_id.in_(msg_ids),
                    )
                    .values(scam_score=result.confidence, scam_reason=result.reason)
                )

                # Flag senders
                for sid in sender_ids:
                    existing = await db.execute(
                        select(ScamFlag).where(ScamFlag.telegram_id == sid)
                    )
                    if existing.scalars().first():
                        continue
                    flag = ScamFlag(
                        telegram_id=sid,
                        source="local_llm",
                        confidence=result.confidence,
                        reason=result.reason,
                        request_id=request_id,
                        flagged_at=now,
                    )
                    db.add(flag)

            # Mark request completed
            req_result = await db.execute(
                select(ScamCheckRequest).where(ScamCheckRequest.request_id == request_id)
            )
            req = req_result.scalars().first()
            if req:
                req.status = "completed"
                req.response_json = json.dumps({
                    "is_scam": result.is_scam,
                    "confidence": result.confidence,
                    "reason": result.reason,
                })
                req.completed_at = now
                req.updated_at = now

            await db.commit()

        # Broadcast to frontend
        if result.is_scam:
            await ws_manager.broadcast({
                "type": "scam_detected",
                "chat_id": chat_id,
                "message_ids": msg_ids,
                "sender_ids": sender_ids,
                "confidence": result.confidence,
                "reason": result.reason,
            })

    async def _handle_followup(self, req: ScamCheckRequest, followup: dict) -> None:
        """Gather data requested by external API and respond."""
        data_type = followup.get("data_type", "")
        params = followup.get("params", {})

        gathered_data = None
        if data_type == "chat_history" and req.related_chat_id:
            # Fetch full chat history
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Message)
                    .where(Message.chat_id == req.related_chat_id)
                    .order_by(Message.date.desc())
                    .limit(params.get("limit", 100))
                )
                msgs = result.scalars().all()
                gathered_data = [
                    {"message_id": m.tg_message_id, "sender_id": m.sender_id,
                     "sender_name": m.sender_name, "text": m.text, "date": str(m.date)}
                    for m in msgs
                ]

        if gathered_data is not None:
            try:
                response = await self._client.respond_to_followup(req.request_id, gathered_data)
                if isinstance(response, FollowUpRequest):
                    # Another follow-up needed
                    async with AsyncSessionLocal() as db:
                        r = await db.execute(
                            select(ScamCheckRequest).where(ScamCheckRequest.request_id == req.request_id)
                        )
                        db_req = r.scalars().first()
                        if db_req:
                            db_req.followup_request_json = json.dumps({
                                "data_type": response.data_type, "params": response.params
                            })
                            db_req.updated_at = datetime.utcnow()
                            await db.commit()
                    await self._handle_followup(req, {"data_type": response.data_type, "params": response.params})
                elif isinstance(response, ScamCheckResult):
                    msg_ids = json.loads(req.related_message_ids_json or "[]")
                    sender_ids = json.loads(req.related_telegram_ids_json or "[]")
                    await self._apply_message_result(
                        req.request_id, req.related_chat_id, msg_ids, sender_ids, response
                    )
            except Exception as e:
                logger.error("Follow-up response failed for %s: %s", req.request_id, e)
                await self._mark_request(req.request_id, "failed")

    async def _broadcast_error(
        self, chat_id: Optional[int], msg_ids: list[int], error: str
    ) -> None:
        """Broadcast a scam check error to the frontend."""
        await ws_manager.broadcast({
            "type": "scam_check_error",
            "chat_id": chat_id,
            "message_ids": msg_ids,
            "error": error,
        })

    async def _mark_request(self, request_id: str, status: str) -> None:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ScamCheckRequest).where(ScamCheckRequest.request_id == request_id)
            )
            req = result.scalars().first()
            if req:
                req.status = status
                req.updated_at = datetime.utcnow()
                if status == "completed":
                    req.completed_at = datetime.utcnow()
                await db.commit()
