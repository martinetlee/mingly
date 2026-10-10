"""Uploads of reports, sender flags, scan results, dismissals, feedback and
analytics: snapshot rows, close the session, POST, then mark them synced only
if they did not change meanwhile. A mixin of ``CloudSyncManager``."""

import json
import logging
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import and_, or_, select, update

from backend.db.database import AsyncSessionLocal
from backend.db.models import Conversation, Message, ScamDismissal, ScamFeedback, ScamFlag, ScamReport
from backend.scam.channel_ids import cloud_refs, CloudRefs, handle_salt, telegram_scan_ids, telegram_user_ids
from backend.scam.verdict import not_pending_condition
from backend.scam.analytics import get_analytics_tracker
from backend.scam.cloud_contract import CONTRACT_HEADER, raise_for_status
from backend.scam.cloud_payloads import (
    cloud_v1,
    existing_rows,
    flag_upload_conditions,
    message_upload_conditions,
    report_payloads,
    telegram_refs,
    v1_dismissal,
    v1_dismissed_scan,
    v1_flag,
    v1_scan,
    v2_dismissal,
    v2_dismissed_scan,
    v2_flag,
    v2_period,
    v2_scan,
)


_BATCH = 100
_MAX_BATCHES = 1000  # hard stop for one sync run (100k rows per category)

logger = logging.getLogger(__name__)


class CloudUploads:
    """Upload half of ``CloudSyncManager``. The manager provides the session
    pieces declared below (its definitions override these stubs)."""

    _base_url: str

    @property
    def _headers(self) -> dict:
        raise NotImplementedError

    def _client(self, timeout: float) -> httpx.AsyncClient:
        raise NotImplementedError

    @property
    def is_configured(self) -> bool:
        raise NotImplementedError

    @property
    def url_error(self) -> Optional[str]:
        raise NotImplementedError

    async def contract_version(self) -> int:
        raise NotImplementedError

    async def _post(self, path: str, payload: dict, contract: int = 1) -> None:
        headers = self._headers
        if contract >= 2:
            headers[CONTRACT_HEADER] = str(contract)
        async with self._client(30.0) as client:
            resp = await client.post(f"{self._base_url}{path}", json=payload, headers=headers)
            raise_for_status(resp)

    async def _post_items(self, path: str, key: str, items: list, v2: bool) -> None:
        if v2:
            await self._post(path, {"contract": 2, key: items}, contract=2)
        else:
            await self._post(path, {key: items})

    async def _v2_salt(self) -> Optional[bytes]:
        """The install salt when the server speaks contract v2; None means v1."""
        if await self.contract_version() < 2:
            return None
        return handle_salt()

    async def _upload_pending_reports(self) -> int:
        salt = await self._v2_salt()
        total = 0
        last_id = 0
        for _ in range(_MAX_BATCHES):
            async with AsyncSessionLocal() as db:
                reports = (await db.execute(
                    select(ScamReport).where(
                        ScamReport.status == "submitted",
                        ScamReport.synced_at.is_(None),
                        ScamReport.id > last_id,
                    ).order_by(ScamReport.id).limit(_BATCH)
                )).scalars().all()
            if not reports:
                break
            last_id = reports[-1].id
            async with AsyncSessionLocal() as db:
                payloads = await report_payloads(db, reports, salt)
            if not payloads:
                continue
            await self._post_items("/sync/reports", "reports", [p for _, p in payloads], salt is not None)
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(ScamReport).where(ScamReport.id.in_([r.id for r, _ in payloads]))
                    .values(synced_at=datetime.utcnow())
                )
                await db.commit()
            total += len(payloads)
        return total

    async def upload_single_report(self, report_id: str) -> bool:
        """Try to upload one report right after submission."""
        if not self.is_configured or self.url_error:
            return False
        try:
            salt = await self._v2_salt()
        except Exception as e:
            logger.warning("Failed to upload report %s: %s", report_id, e)
            return False
        async with AsyncSessionLocal() as db:
            report = (await db.execute(
                select(ScamReport).where(ScamReport.report_id == report_id)
            )).scalars().first()
            payloads = await report_payloads(db, [report], salt) if report else []
        if not payloads:
            return False
        try:
            await self._post_items("/sync/reports", "reports", [payloads[0][1]], salt is not None)
        except Exception as e:
            logger.warning("Failed to upload report %s: %s", report_id, e)
            return False
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ScamReport).where(ScamReport.id == report.id).values(synced_at=datetime.utcnow())
            )
            await db.commit()
        return True

    async def _upload_unsent_flags(self) -> int:
        salt = await self._v2_salt()
        contract = 1 if salt is None else 2
        total = 0
        last_id = 0
        for _ in range(_MAX_BATCHES):
            users: dict = {}
            refs = CloudRefs()
            async with AsyncSessionLocal() as db:
                flags = (await db.execute(
                    select(ScamFlag).where(
                        ScamFlag.synced_to_cloud == False,  # noqa: E712
                        ScamFlag.id > last_id,
                        *flag_upload_conditions(contract),
                    ).order_by(ScamFlag.id).limit(_BATCH)
                )).scalars().all()
                if salt is None:
                    users = await telegram_user_ids(db, [f.identity_id for f in flags])
                else:
                    refs = await cloud_refs(db, salt, identity_ids=[f.identity_id for f in flags])
            if not flags:
                break
            last_id = flags[-1].id
            if salt is None:
                flags = [f for f in flags if f.identity_id in users]
                items = [v1_flag(f, users[f.identity_id]) for f in flags]
            else:
                flags = [f for f in flags if f.identity_id in refs.handles]
                items = [v2_flag(f, refs) for f in flags]
            if not flags:
                continue
            await self._post_items("/sync/flags", "flags", items, salt is not None)
            async with AsyncSessionLocal() as db:
                for f in flags:
                    await db.execute(
                        update(ScamFlag).where(
                            ScamFlag.id == f.id,
                            or_(ScamFlag.updated_at.is_(None), ScamFlag.updated_at <= f.updated_at)
                            if f.updated_at is not None else ScamFlag.updated_at.is_(None),
                        ).values(synced_to_cloud=True)
                    )
                await db.commit()
            total += len(flags)
        return total

    async def _upload_scan_results(self) -> int:
        """Upload scan rows changed since their last upload: pipeline scam,
        failed scans, manual verdicts, and corrections to anything uploaded
        before."""
        salt = await self._v2_salt()
        contract = 1 if salt is None else 2
        total = 0
        cursor: Optional[tuple] = None
        for _ in range(_MAX_BATCHES):
            conditions = [
                *message_upload_conditions(contract),
                Message.scam_updated_at.isnot(None),
                not_pending_condition(),
                or_(Message.scam_synced_at.is_(None), Message.scam_updated_at > Message.scam_synced_at),
                or_(
                    Message.effective_is_scam,
                    Message.scam_status == "failed",
                    Message.scam_manual_verdict.isnot(None),
                    Message.scam_synced_at.isnot(None),
                ),
            ]
            if cursor is not None:
                conditions.append(or_(
                    Message.scam_updated_at > cursor[0],
                    and_(Message.scam_updated_at == cursor[0], Message.id > cursor[1]),
                ))
            async with AsyncSessionLocal() as db:
                msgs = (await db.execute(
                    select(Message).where(*conditions)
                    .order_by(Message.scam_updated_at, Message.id).limit(_BATCH)
                )).scalars().all()
                if salt is None:
                    refs = await telegram_refs(
                        db, identity_ids=[m.sender_identity_id for m in msgs],
                        conversation_ids=[m.conversation_id for m in msgs],
                    )
                    for cid in {m.conversation_id for m in msgs if m.conversation_id in refs.peers}:
                        tg = await telegram_scan_ids(
                            db, await db.get(Conversation, cid), [m for m in msgs if m.conversation_id == cid]
                        )
                        if tg is not None:
                            refs.senders.update(tg.senders)
                else:
                    refs = await cloud_refs(
                        db, salt, identity_ids=[m.sender_identity_id for m in msgs],
                        conversation_ids=[m.conversation_id for m in msgs], message_ids=[m.id for m in msgs],
                    )
            if not msgs:
                break
            cursor = (msgs[-1].scam_updated_at, msgs[-1].id)

            if salt is None:
                scan_results = [v1_scan(m, refs) for m in msgs]
            else:
                scan_results = [v2_scan(m, refs) for m in msgs]
            await self._post_items("/sync/scan-results", "scan_results", scan_results, salt is not None)

            now = datetime.utcnow()
            async with AsyncSessionLocal() as db:
                for m in msgs:
                    await db.execute(
                        update(Message)
                        .where(Message.id == m.id, Message.scam_updated_at <= m.scam_updated_at)
                        .values(scam_synced_at=now, scam_synced_to_cloud=True)
                    )
                await db.commit()
            total += len(msgs)
        return total

    async def _upload_pending_dismissals(self) -> int:
        """Upload queued dismissals (sender flags and scan verdicts)."""
        salt = await self._v2_salt()
        total = 0
        last_id = 0
        for _ in range(_MAX_BATCHES):
            async with AsyncSessionLocal() as db:
                dismissals = (await db.execute(
                    select(ScamDismissal).where(ScamDismissal.synced_at.is_(None), ScamDismissal.id > last_id)
                    .order_by(ScamDismissal.id).limit(50)
                )).scalars().all()
            if not dismissals:
                break
            last_id = dismissals[-1].id

            parsed = []
            for d in dismissals:
                try:
                    data = json.loads(d.payload_json)
                except (ValueError, TypeError):
                    data = {}
                parsed.append((d, data if isinstance(data, dict) else {}))
            ids = dict(
                identity_ids=[data.get("identity_id") for _, data in parsed],
                conversation_ids=[data.get("conversation_id") for _, data in parsed],
                message_ids=[data.get("message_id") for _, data in parsed],
            )
            async with AsyncSessionLocal() as db:
                refs = await (telegram_refs(db, **ids) if salt is None else cloud_refs(db, salt, **ids))
                existing_identities, existing_conversations = await existing_rows(db, **ids)
            # v1: Telegram rows only (a sender flag needs its Telegram user, a
            # scan its Telegram conversation), resolved now or snapshotted at
            # dismissal time (``cloud_v1``). v2: any row still resolvable, plus
            # Telegram rows from their snapshot. A dismissal whose rows are
            # gone without a usable snapshot can never be uploaded: skipped.
            known_senders = refs.users if salt is None else refs.handles
            known_conversations = refs.peers if salt is None else refs.conversations

            scammer_items, scan_items, skipped = [], [], []
            for d, data in parsed:
                v1 = cloud_v1(data)
                if d.dismissal_type == "scammer":
                    if data.get("identity_id") in known_senders or v1.get("telegram_id") is not None:
                        scammer_items.append((d, data))
                    elif data.get("identity_id") not in existing_identities:
                        skipped.append(d)
                elif data.get("conversation_id") in known_conversations or (
                        salt is None and v1.get("chat_id") is not None):
                    scan_items.append((d, data))
                elif data.get("conversation_id") not in existing_conversations:
                    skipped.append(d)

            if scammer_items:
                build = v1_dismissal if salt is None else v2_dismissal
                await self._post_items("/sync/dismissals", "dismissals",
                                       [build(data, refs) for _, data in scammer_items], salt is not None)
                await self._mark_dismissals([d.id for d, _ in scammer_items])
            if scan_items:
                build = v1_dismissed_scan if salt is None else v2_dismissed_scan
                await self._post_items("/sync/scan-results", "scan_results",
                                       [build(data, refs) for _, data in scan_items], salt is not None)
                await self._mark_dismissals([d.id for d, _ in scan_items])
            if skipped:
                await self._skip_dismissals(skipped)
            total += len(scammer_items) + len(scan_items)
        return total

    async def _skip_dismissals(self, dismissals: list) -> None:
        """Retire dismissals no contract can carry any more (their message,
        conversation or sender was deleted and no Telegram snapshot exists),
        so they leave the queue instead of being retried forever."""
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ScamDismissal).where(ScamDismissal.id.in_([d.id for d in dismissals]))
                .values(synced_at=datetime.utcnow(), cloud_status="skipped",
                        cloud_skipped_reason="source rows deleted")
            )
            await db.commit()
        logger.info("Skipped %d dismissals whose rows were deleted", len(dismissals))

    async def _mark_dismissals(self, ids: list) -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ScamDismissal).where(ScamDismissal.id.in_(ids))
                .values(synced_at=datetime.utcnow(), cloud_status="synced")
            )
            await db.commit()

    async def upload_feedback(self, feedback_id: int) -> bool:
        """Try to upload a single feedback item immediately. Returns True if synced."""
        if not self.is_configured:
            return False
        if self.url_error:
            raise ValueError(self.url_error)
        salt = await self._v2_salt()
        async with AsyncSessionLocal() as db:
            fb = (await db.execute(select(ScamFeedback).where(ScamFeedback.id == feedback_id))).scalars().first()
            if not fb:
                return False
            ids = dict(identity_ids=[fb.related_flag_identity_id],
                       conversation_ids=[fb.related_conversation_id], message_ids=[fb.related_message_id])
            refs = await (telegram_refs(db, **ids) if salt is None else cloud_refs(db, salt, **ids))
        if salt is None:
            # Feedback about another channel's rows needs cloud contract v2
            if ((fb.related_conversation_id and fb.related_conversation_id not in refs.peers)
                    or (fb.related_message_id and fb.related_message_id not in refs.messages)
                    or (fb.related_flag_identity_id and fb.related_flag_identity_id not in refs.users)):
                return False
            item = {
                "id": fb.id,
                "feedback_type": fb.feedback_type,
                "message": fb.message,
                "related_chat_id": refs.peers.get(fb.related_conversation_id),
                "related_message_id": refs.messages.get(fb.related_message_id),
                "related_flag_telegram_id": refs.users.get(fb.related_flag_identity_id),
                "created_at": fb.created_at.isoformat() if fb.created_at else None,
            }
        else:
            item = {
                "id": fb.id,
                "channel": refs.channel_of(conversation_id=fb.related_conversation_id,
                                           message_id=fb.related_message_id,
                                           identity_id=fb.related_flag_identity_id),
                "feedback_type": fb.feedback_type,
                "message": fb.message,
                "related_conversation": refs.conversations.get(fb.related_conversation_id),
                "related_message": refs.messages.get(fb.related_message_id),
                "related_sender": refs.handles.get(fb.related_flag_identity_id),
                "created_at": fb.created_at.isoformat() if fb.created_at else None,
            }
        await self._post_items("/sync/feedback", "feedback", [item], salt is not None)
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ScamFeedback).where(ScamFeedback.id == feedback_id)
                .values(status="synced", synced_at=datetime.utcnow())
            )
            await db.commit()
        logger.info("Feedback %d synced to cloud", feedback_id)
        return True

    async def _upload_analytics(self) -> int:
        """Upload closed (past) analytics days that haven't been synced."""
        tracker = get_analytics_tracker()

        periods = await tracker.get_pending_periods()
        if not periods:
            return 0
        v2 = await self.contract_version() >= 2
        await self._post_items("/sync/analytics", "periods", [v2_period(p) for p in periods] if v2 else periods, v2)
        await tracker.mark_synced([p["period_start"] for p in periods])
        return len(periods)
