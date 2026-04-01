"""Cloud sync manager for Shield scam detection assets.

Syncs pipeline, scenarios, and signatures from the cloud.
Uploads scam reports and flagged users upstream.
Works without a real cloud endpoint — stubs downloads, does real local reload.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import httpx

from backend.config import DATA_DIR

logger = logging.getLogger(__name__)

def _extract_pipeline_version(trace_json: Optional[str]) -> Optional[str]:
    """Extract pipeline_version from a trace JSON string."""
    if not trace_json:
        return None
    try:
        return json.loads(trace_json).get("pipeline_version")
    except Exception:
        return None


def _extract_pipeline_input(trace_json: Optional[str]) -> Optional[str]:
    """Extract the $message input from the first step's trace."""
    if not trace_json:
        return None
    try:
        trace = json.loads(trace_json)
        steps = trace.get("steps", [])
        if steps:
            inp = steps[0].get("input", {})
            return inp.get("$message")
    except Exception:
        pass
    return None


def _extract_models_used(trace_json: Optional[str]) -> Optional[list]:
    """Extract model info from all LLM steps in the trace."""
    if not trace_json:
        return None
    try:
        trace = json.loads(trace_json)
        models = []
        seen = set()
        for step in trace.get("steps", []):
            if step.get("type") != "llm":
                continue
            model_name = step.get("model_name")
            if model_name and model_name not in seen:
                seen.add(model_name)
                models.append({
                    "node_id": step.get("node_id"),
                    "model_name": model_name,
                    "provider": step.get("provider_name"),
                    "max_tokens": step.get("max_tokens"),
                    "temperature": step.get("temperature"),
                })
        return models if models else None
    except Exception:
        return None


def _extract_scan_status(trace_json: Optional[str]) -> str:
    """Extract pipeline status (completed/failed/short_circuited) from trace."""
    if not trace_json:
        return "unknown"
    try:
        return json.loads(trace_json).get("status", "unknown")
    except Exception:
        return "unknown"


_manager: Optional["CloudSyncManager"] = None


def get_cloud_sync_manager() -> Optional["CloudSyncManager"]:
    return _manager


def init_cloud_sync_manager() -> "CloudSyncManager":
    global _manager
    _manager = CloudSyncManager()
    # Load config from prefs
    from backend.api.settings import _load_prefs
    prefs = _load_prefs()
    _manager.configure(
        base_url=prefs.get("shield_cloud_url", ""),
        api_key=prefs.get("shield_api_key", ""),
    )
    last_synced = prefs.get("shield_last_synced_at")
    if last_synced:
        try:
            _manager._last_synced_at = datetime.fromisoformat(last_synced)
        except (ValueError, TypeError):
            pass
    _manager._last_sync_status = prefs.get("shield_last_sync_status")
    _manager._last_sync_error = prefs.get("shield_last_sync_error")
    _manager._last_sync_details = prefs.get("shield_last_sync_details")
    return _manager


@dataclass
class SyncResult:
    status: str = "pending"  # success, failed, not_configured
    error: Optional[str] = None
    files_updated: list = None  # names of files that were updated
    reports_submitted: int = 0
    flags_submitted: int = 0
    scan_results_submitted: int = 0
    dismissals_submitted: int = 0


class CloudSyncManager:
    def __init__(self):
        self._base_url: str = ""
        self._api_key: str = ""
        self._last_synced_at: Optional[datetime] = None
        self._last_sync_status: Optional[str] = None  # success, failed
        self._last_sync_error: Optional[str] = None
        self._last_sync_details: Optional[dict] = None  # files_updated, reports, flags, scans counts

    def configure(self, base_url: str, api_key: str):
        self._base_url = base_url.rstrip("/") if base_url else ""
        self._api_key = api_key or ""

    @property
    def is_configured(self) -> bool:
        return bool(self._base_url and self._api_key)

    @property
    def last_synced_at(self) -> Optional[datetime]:
        return self._last_synced_at

    async def sync(self) -> SyncResult:
        """Run a full sync cycle."""
        if not self.is_configured:
            return SyncResult(status="not_configured")

        result = SyncResult(status="started")

        try:
            # 1. Check manifest
            manifest = await self._get_manifest()
            if manifest is None:
                result.status = "failed"
                result.error = "Could not reach cloud server"
                return result

            # 2. Download changed files
            files = manifest.get("files", [])
            updated_files = []

            for file_info in files:
                fname = file_info.get("name", "")
                remote_version = file_info.get("version", "")
                endpoint = file_info.get("endpoint", "")
                file_type = file_info.get("type", "json")

                if not fname or not endpoint:
                    continue

                # For model bundles, check version via thresholds.json inside the domain dir
                if file_type == "model_bundle":
                    domain = fname.replace("ml_models/", "").replace(".zip", "")
                    local_version = self._get_local_version(DATA_DIR / "ml_models" / domain / "thresholds.json")
                else:
                    local_version = self._get_local_version(DATA_DIR / fname)

                if remote_version == local_version:
                    continue

                if file_type == "model_bundle":
                    success = await self._download_model_bundle(endpoint, domain, remote_version)
                    if success:
                        updated_files.append(fname)
                        logger.info("Synced model bundle %s: %s → %s", domain, local_version or "(none)", remote_version)
                else:
                    data = await self._download(endpoint)
                    if data:
                        target = DATA_DIR / fname
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(
                            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                        )
                        updated_files.append(fname)
                        logger.info("Synced %s: %s → %s", fname, local_version or "(none)", remote_version)

            result.files_updated = updated_files

            # Post-download hooks for specific files
            if updated_files:
                from backend.scam.pipeline import get_pipeline_manager
                mgr = get_pipeline_manager()

                if "scam_pipeline.json" in updated_files and mgr:
                    mgr.reload()

                if "scam_signatures.json" in updated_files and mgr:
                    from backend.scam.pipeline import _load_signatures_file
                    _load_signatures_file(mgr.get_config())

                # Reload ML ensemble if any model bundles were updated
                model_updated = any(f.startswith("ml_models/") for f in updated_files)
                if model_updated:
                    try:
                        from backend.scam.tools.ml_prefilter import reload_ensemble
                        reload_ensemble()
                    except ImportError:
                        pass

                # Invalidate search library cache for any updated knowledge base files
                from backend.scam.tools.search_library import invalidate_cache
                for fname in updated_files:
                    if fname not in ("scam_pipeline.json", "scam_signatures.json"):
                        invalidate_cache(fname)

            # 3. Upload pending reports
            reports_count = await self._upload_pending_reports()
            result.reports_submitted = reports_count

            # 4. Upload unsent flags
            flags_count = await self._upload_unsent_flags()
            result.flags_submitted = flags_count

            # 5. Upload unsynced scan results
            scans_count = await self._upload_scan_results()
            result.scan_results_submitted = scans_count

            # 6. Upload queued dismissals (if sync_dismissals enabled)
            from backend.api.settings import _load_prefs
            prefs = _load_prefs()
            dismissals_count = 0
            if prefs.get("shield_sync_dismissals", False):
                dismissals_count = await self._upload_pending_dismissals()
            result.dismissals_submitted = dismissals_count

            # 7. Upload analytics
            await self._upload_analytics()

            # Record sync time
            self._last_synced_at = datetime.utcnow()
            result.status = "success"
            self._last_sync_status = "success"
            self._last_sync_error = None
            self._last_sync_details = {
                "files_updated": result.files_updated or [],
                "reports_submitted": result.reports_submitted,
                "flags_submitted": result.flags_submitted,
                "scan_results_submitted": result.scan_results_submitted,
                "dismissals_submitted": result.dismissals_submitted,
            }
            self._save_sync_state()
            logger.info("Cloud sync completed: %d files updated (%s), reports=%d flags=%d scans=%d",
                        len(result.files_updated or []), ", ".join(result.files_updated or []),
                        result.reports_submitted, result.flags_submitted,
                        result.scan_results_submitted)

        except Exception as e:
            result.status = "failed"
            result.error = str(e)
            self._last_sync_status = "failed"
            self._last_sync_error = str(e)
            self._last_sync_details = None
            self._save_sync_state()
            logger.warning("Cloud sync failed: %s", e)

        return result

    async def upload_single_report(self, report_id: str):
        """Try to upload a single report immediately after submission."""
        if not self.is_configured:
            return
        try:
            from backend.db.database import AsyncSessionLocal
            from backend.db.models import ScamReport
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ScamReport).where(ScamReport.report_id == report_id)
                )
                report = result.scalars().first()
                if not report:
                    return

                payload = {
                    "reports": [{
                        "report_id": report.report_id,
                        "reported_telegram_id": report.reported_telegram_id,
                        "reported_in_chat_id": report.reported_in_chat_id,
                        "chat_type": report.chat_type,
                        "reporter_verdict": report.reporter_verdict,
                        "scam_category": report.scam_category,
                        "custom_category": report.custom_category,
                        "messages_json": report.messages_json,
                        "local_detection_json": report.local_detection_json,
                        "created_at": report.created_at.isoformat() if report.created_at else None,
                    }]
                }

                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{self._base_url}/sync/reports",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                    if resp.is_success:
                        report.synced_at = datetime.utcnow()
                        await db.commit()
                        logger.info("Report %s synced to cloud", report_id)

        except Exception as e:
            logger.warning("Failed to upload report %s: %s", report_id, e)

    async def _upload_pending_dismissals(self) -> int:
        """Upload queued dismissals (flags and scans) to cloud."""
        from backend.db.database import AsyncSessionLocal
        from backend.db.models import ScamDismissal
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ScamDismissal).where(
                    ScamDismissal.synced_at.is_(None),
                ).order_by(ScamDismissal.created_at.asc()).limit(50)
            )
            dismissals = result.scalars().all()
            if not dismissals:
                return 0

            # Split into scammer dismissals and scan dismissals
            scammer_items = []
            scan_items = []
            for d in dismissals:
                data = json.loads(d.payload_json)
                if d.dismissal_type == "scammer":
                    scammer_items.append((d, data))
                else:
                    scan_items.append((d, data))

            synced_count = 0

            # Upload scammer dismissals via /sync/dismissals
            if scammer_items:
                payload = {"dismissals": [data for _, data in scammer_items]}
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(
                            f"{self._base_url}/sync/dismissals",
                            json=payload,
                            headers={"Authorization": f"Bearer {self._api_key}"},
                        )
                        if resp.is_success:
                            now = datetime.utcnow()
                            for d, _ in scammer_items:
                                d.synced_at = now
                            synced_count += len(scammer_items)
                except Exception as e:
                    logger.warning("Failed to upload scammer dismissals: %s", e)

            # Upload scan dismissals via /sync/scan-results (same endpoint)
            if scan_items:
                scan_results = []
                for _, data in scan_items:
                    trace = data.get("trace")
                    trace_json_str = json.dumps(trace) if trace else None
                    scan_results.append({
                        "chat_id": data.get("chat_id"),
                        "tg_message_id": data.get("tg_message_id"),
                        "sender_id": data.get("sender_id"),
                        "scam_score": data.get("scam_score"),
                        "scam_reason": data.get("scam_reason"),
                        "scam_category": data.get("scam_category"),
                        "scan_status": "dismissed",
                        "dismissed": True,
                        "pipeline_version": trace.get("pipeline_version") if trace else None,
                        "pipeline_input": _extract_pipeline_input(trace_json_str),
                        "models_used": _extract_models_used(trace_json_str),
                        "llm_prompt": data.get("llm_prompt"),
                        "llm_response": data.get("llm_response"),
                        "trace": trace,
                    })
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(
                            f"{self._base_url}/sync/scan-results",
                            json={"scan_results": scan_results},
                            headers={"Authorization": f"Bearer {self._api_key}"},
                        )
                        if resp.is_success:
                            now = datetime.utcnow()
                            for d, _ in scan_items:
                                d.synced_at = now
                            synced_count += len(scan_items)
                except Exception as e:
                    logger.warning("Failed to upload scan dismissals: %s", e)

            if synced_count > 0:
                await db.commit()
            logger.info("Uploaded %d dismissals (%d scammer, %d scan)",
                        synced_count, len(scammer_items), len(scan_items))
            return synced_count

    async def upload_feedback(self, feedback_id: int):
        """Try to upload a single feedback item immediately."""
        if not self.is_configured:
            return
        try:
            from backend.db.database import AsyncSessionLocal
            from backend.db.models import ScamFeedback
            from sqlalchemy import select

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ScamFeedback).where(ScamFeedback.id == feedback_id)
                )
                fb = result.scalars().first()
                if not fb:
                    return

                payload = {
                    "feedback": [{
                        "id": fb.id,
                        "feedback_type": fb.feedback_type,
                        "message": fb.message,
                        "related_chat_id": fb.related_chat_id,
                        "related_message_id": fb.related_message_id,
                        "related_flag_telegram_id": fb.related_flag_telegram_id,
                        "created_at": fb.created_at.isoformat() if fb.created_at else None,
                    }]
                }

                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{self._base_url}/sync/feedback",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                    if resp.is_success:
                        fb.status = "synced"
                        fb.synced_at = datetime.utcnow()
                        await db.commit()
                        logger.info("Feedback %d synced to cloud", feedback_id)

        except Exception as e:
            logger.warning("Failed to upload feedback %d: %s", feedback_id, e)

    # --- Internal helpers ---

    async def _get_manifest(self) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{self._base_url}/sync/manifest",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if resp.is_success:
                    return resp.json()
                logger.warning("Manifest request failed: %d %s", resp.status_code, resp.text[:200])
                return None
        except Exception as e:
            logger.warning("Manifest request error: %s", e)
            return None

    async def _download(self, path: str) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if resp.is_success:
                    return resp.json()
                return None
        except Exception:
            return None

    async def _download_model_bundle(self, path: str, domain: str, version: str) -> bool:
        """Download a model bundle (zip) and extract to DATA_DIR/ml_models/<domain>/."""
        import zipfile
        import io

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.get(
                    f"{self._base_url}{path}",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if not resp.is_success:
                    logger.warning("Model bundle download failed for %s: %d", domain, resp.status_code)
                    return False

                # Extract zip to target directory
                target_dir = DATA_DIR / "ml_models" / domain
                target_dir.mkdir(parents=True, exist_ok=True)

                with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                    zf.extractall(target_dir)

                # Write version to thresholds.json if not already there
                thresholds_file = target_dir / "thresholds.json"
                if thresholds_file.exists():
                    import json as _json
                    t = _json.loads(thresholds_file.read_text())
                    t["version"] = version
                    thresholds_file.write_text(_json.dumps(t, indent=2))
                else:
                    thresholds_file.write_text(json.dumps({"version": version}))

                logger.info("Extracted model bundle for domain '%s' to %s", domain, target_dir)
                return True

        except Exception as e:
            logger.warning("Failed to download model bundle for %s: %s", domain, e)
            return False

    async def _upload_pending_reports(self) -> int:
        from backend.db.database import AsyncSessionLocal
        from backend.db.models import ScamReport
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ScamReport).where(
                    ScamReport.status == "submitted",
                    ScamReport.synced_at.is_(None),
                ).limit(50)
            )
            reports = result.scalars().all()
            if not reports:
                return 0

            payload = {
                "reports": [{
                    "report_id": r.report_id,
                    "reported_telegram_id": r.reported_telegram_id,
                    "reported_in_chat_id": r.reported_in_chat_id,
                    "chat_type": r.chat_type,
                    "reporter_verdict": r.reporter_verdict,
                    "scam_category": r.scam_category,
                    "custom_category": r.custom_category,
                    "messages_json": r.messages_json,
                    "local_detection_json": r.local_detection_json,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                } for r in reports]
            }

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{self._base_url}/sync/reports",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                    if resp.is_success:
                        now = datetime.utcnow()
                        for r in reports:
                            r.synced_at = now
                        await db.commit()
                        return len(reports)
            except Exception as e:
                logger.warning("Failed to upload reports: %s", e)

            return 0

    async def _upload_unsent_flags(self) -> int:
        from backend.db.database import AsyncSessionLocal
        from backend.db.models import ScamFlag
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ScamFlag).where(ScamFlag.synced_to_cloud == False).limit(100)
            )
            flags = result.scalars().all()
            if not flags:
                return 0

            payload = {
                "flags": [{
                    "telegram_id": f.telegram_id,
                    "source": f.source,
                    "confidence": f.confidence,
                    "reason": f.reason,
                    "category": f.category,
                    "flagged_message_text": f.flagged_message_text,
                    "flagged_at": f.flagged_at.isoformat() if f.flagged_at else None,
                } for f in flags]
            }

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{self._base_url}/sync/flags",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                    if resp.is_success:
                        for f in flags:
                            f.synced_to_cloud = True
                        await db.commit()
                        return len(flags)
            except Exception as e:
                logger.warning("Failed to upload flags: %s", e)

            return 0

    async def _upload_scan_results(self) -> int:
        from backend.db.database import AsyncSessionLocal
        from backend.db.models import Message
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            from sqlalchemy import or_, and_
            # Upload scam-detected messages (score > 0.5) and failed scans
            result = await db.execute(
                select(Message).where(
                    or_(
                        Message.scam_score > 0.5,
                        and_(
                            Message.scam_trace_json.isnot(None),
                            Message.scam_trace_json.contains('"status": "failed"'),
                        ),
                    ),
                    Message.scam_synced_to_cloud == False,
                ).order_by(Message.date.desc()).limit(100)
            )
            msgs = result.scalars().all()
            if not msgs:
                return 0

            payload = {
                "scan_results": [{
                    "chat_id": m.chat_id,
                    "tg_message_id": m.tg_message_id,
                    "sender_id": m.sender_id,
                    "scam_score": m.scam_score,
                    "scam_reason": m.scam_reason,
                    "scam_category": m.scam_category,
                    "scan_status": _extract_scan_status(m.scam_trace_json),
                    "date": m.date.isoformat() if m.date else None,
                    "pipeline_version": _extract_pipeline_version(m.scam_trace_json),
                    "pipeline_input": _extract_pipeline_input(m.scam_trace_json),
                    "models_used": _extract_models_used(m.scam_trace_json),
                } for m in msgs]
            }

            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{self._base_url}/sync/scan-results",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
                    if resp.is_success:
                        for m in msgs:
                            m.scam_synced_to_cloud = True
                        await db.commit()
                        return len(msgs)
            except Exception as e:
                logger.warning("Failed to upload scan results: %s", e)

            return 0

    async def _upload_analytics(self):
        """Upload pending analytics periods to cloud."""
        from backend.scam.analytics import get_analytics_tracker
        tracker = get_analytics_tracker()

        periods = await tracker.get_pending_periods()
        if not periods:
            return

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    f"{self._base_url}/sync/analytics",
                    json={"periods": periods},
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if resp.is_success:
                    await tracker.mark_synced([p["period_start"] for p in periods])
                    logger.info("Uploaded %d analytics periods", len(periods))
        except Exception as e:
            logger.warning("Failed to upload analytics: %s", e)

    def _get_local_version(self, path) -> str:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("version", "")
        except Exception:
            return ""

    def _save_sync_state(self):
        from backend.api.settings import _load_prefs, _save_prefs
        prefs = _load_prefs()
        prefs["shield_last_synced_at"] = self._last_synced_at.isoformat() if self._last_synced_at else None
        prefs["shield_last_sync_status"] = self._last_sync_status
        prefs["shield_last_sync_error"] = self._last_sync_error
        prefs["shield_last_sync_details"] = self._last_sync_details
        _save_prefs(prefs)
