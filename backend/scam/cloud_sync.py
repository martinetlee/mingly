"""Cloud sync manager for Shield scam detection assets.

Downloads pipeline, scenarios, signatures and ML model bundles listed in the
cloud manifest; uploads reports, sender flags, scan results, dismissals and
analytics.

Safety rules:
* One sync at a time (``jobs.await_single``): concurrent callers (startup
  loop, both "Sync Now" buttons) join the run in flight.
* The cloud URL must be https (plain http only for localhost).
* Manifest names are allowlisted and resolved inside DATA_DIR; endpoints must
  be paths on the configured host. A manifest ``sha256`` is verified before a
  file is installed (a mismatching model bundle is never unpickled).
* Files are written atomically; model bundles are extracted to a temp dir and
  swapped in by rename, then the ML ensemble is rebuilt off-loop and swapped.
* Uploads snapshot rows, close the DB session, POST, then mark rows synced
  only if they did not change meanwhile (``scam_updated_at <= snapshot``), so
  re-scores and manual corrections always reach the cloud.
* The cloud contract is still Telegram-shaped (telegram_id, chat_id,
  tg_message_id). Rows are stored with internal ids; uploads resolve the
  Telegram ids through ``backend.scam.channel_ids`` and skip rows from other
  channels (they stay unsynced until cloud contract v2).
"""

import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import and_, or_, select, update

from backend.config import DATA_DIR
from backend.db.database import AsyncSessionLocal
from backend.db.models import (
    Account, Conversation, Identity, Message, ScamDismissal, ScamFeedback, ScamFlag, ScamReport,
)
from backend.fsutil import atomic_write_text
from backend.jobs import await_single
from backend.prefs import get_pref, update_prefs
from backend.scam.channel_ids import TELEGRAM, telegram_message_id, telegram_peer_ids, telegram_user_ids
from backend.scam.verdict import not_pending_condition

logger = logging.getLogger(__name__)

_BATCH = 100
_MAX_BATCHES = 1000  # hard stop for one sync run (100k rows per category)
_MAX_BUNDLE_BYTES = 300 * 1024 * 1024

# Files the cloud may install into DATA_DIR. App-owned files (prefs.json,
# llm_config.json, vault...) can never match.
_JSON_NAME_RE = re.compile(
    r"^(?:scam_[a-z0-9_]+|scenarios|safe_patterns|[a-z0-9_]+_(?:library|patterns|scenarios))\.json$"
)
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,63}$")
_BUNDLE_NAME_RE = re.compile(r"^ml_models/([A-Za-z0-9][A-Za-z0-9_\-]{0,63})\.zip$")
_BUNDLE_MEMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}$")
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _raise_for_status(resp: httpx.Response) -> None:
    """Like resp.raise_for_status() but includes the response body in the error."""
    if resp.is_success:
        return
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text[:500]
    raise httpx.HTTPStatusError(
        f"{resp.status_code} {resp.reason_phrase} — {detail}",
        request=resp.request,
        response=resp,
    )


def validate_cloud_url(url: str) -> Optional[str]:
    """Return an error message if ``url`` may not be used for cloud sync."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return None
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return None
    return "Cloud URL must use https (plain http is only allowed for localhost)"


def _contained(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(DATA_DIR.resolve()):
        raise ValueError(f"Path escapes the data directory: {path}")
    return resolved


def _valid_endpoint(endpoint: str) -> bool:
    return bool(endpoint) and endpoint.startswith("/") and not endpoint.startswith("//") \
        and "://" not in endpoint and not any(c.isspace() for c in endpoint)


def verify_artifact(data: bytes, file_info: dict, name: str) -> None:
    """The single gate every downloaded file passes before it is installed
    (and, for model bundles, before anything could unpickle it).

    Today: the manifest's sha256, when given (transport is https-only).
    Bundle *signatures* need the cloud to sign them; when it does, verify the
    manifest/bundle signature here and nowhere else.
    """
    expected = file_info.get("sha256")
    if expected:
        actual = hashlib.sha256(data).hexdigest()
        if actual.lower() != str(expected).strip().lower():
            raise ValueError(f"sha256 mismatch for {name}: expected {expected}, got {actual}")


# ---------------------------------------------------------------------------
# Telegram ids for the current cloud contract
# ---------------------------------------------------------------------------

def telegram_flag_condition():
    """Flags the current cloud contract can carry (Telegram senders)."""
    return ScamFlag.identity_id.in_(select(Identity.id).where(Identity.channel == TELEGRAM))


def telegram_message_condition():
    """Messages the current cloud contract can carry (Telegram conversations)."""
    return Message.conversation_id.in_(
        select(Conversation.id).join(Account, Account.id == Conversation.account_id)
        .where(Account.channel == TELEGRAM)
    )


@dataclass
class _TelegramRefs:
    users: dict  # identity_id -> Telegram user id
    peers: dict  # conversation_id -> Telegram peer id
    messages: dict  # message_id -> Telegram message id


async def _telegram_refs(db, *, identity_ids=(), conversation_ids=(), message_ids=()) -> _TelegramRefs:
    """Telegram ids of the given rows; rows of other channels (or deleted
    rows) are simply absent from the maps."""
    mids = [m for m in set(message_ids) if m]
    rows = (await db.execute(
        select(Message.id, Message.conversation_id, Message.seq).where(Message.id.in_(mids))
    )).all() if mids else []
    peers = await telegram_peer_ids(db, set(conversation_ids) | {r.conversation_id for r in rows})
    return _TelegramRefs(
        users=await telegram_user_ids(db, identity_ids),
        peers=peers,
        messages={r.id: telegram_message_id(r) for r in rows if r.conversation_id in peers},
    )


_CLOUD_KIND = {"direct": "private"}  # cloud v1 chat types


def _trace(trace_json: Optional[str]) -> dict:
    if not trace_json:
        return {}
    try:
        data = json.loads(trace_json)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _extract_pipeline_input(trace: dict) -> Optional[str]:
    if trace.get("input"):
        return trace["input"]
    for step in trace.get("steps", []):
        inp = step.get("input") or {}
        if isinstance(inp, dict) and "$message" in inp:
            return inp["$message"]
    return None


def _extract_models_used(trace: dict) -> Optional[list]:
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
    return models or None


_manager: Optional["CloudSyncManager"] = None
_loop_task: Optional[asyncio.Task] = None
SYNC_INITIAL_DELAY = 5
SYNC_INTERVAL = 86400


def get_cloud_sync_manager() -> Optional["CloudSyncManager"]:
    return _manager


async def _sync_loop() -> None:
    """Sync shortly after unlock, then daily. Runs only while the vault is
    unlocked: the lock/reset listener cancels it."""
    await asyncio.sleep(SYNC_INITIAL_DELAY)
    while True:
        mgr = _manager
        if mgr is not None and mgr.is_configured:
            await mgr.sync()
        await asyncio.sleep(SYNC_INTERVAL)


def start_sync_loop() -> bool:
    """Start the background sync loop (idempotent). Needs a running loop."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return False
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    from backend.tasks import spawn

    _loop_task = spawn(_sync_loop(), name="cloud_sync_loop")
    return True


def stop_sync_loop() -> None:
    """Stop the loop and abort an upload in flight (vault locked or reset:
    nothing may keep talking to the cloud with the revoked key)."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        _loop_task.cancel()
    _loop_task = None
    from backend.jobs import get_running

    running = get_running("cloud_sync")
    if running is not None:
        running.cancel()


def _register_vault_listeners() -> None:
    from backend import secrets as vault

    vault.add_lock_listener(stop_sync_loop)  # reset_vault() locks first
    vault.add_unlock_listener(start_sync_loop)
    if vault.vault_status() == "unlocked":
        start_sync_loop()


def init_cloud_sync_manager() -> "CloudSyncManager":
    global _manager
    from backend.api.settings import get_shield_api_key

    _manager = CloudSyncManager(key_provider=get_shield_api_key)
    _manager.configure(base_url=get_pref("shield_cloud_url", "") or "")
    last_synced = get_pref("shield_last_synced_at")
    if last_synced:
        try:
            _manager._last_synced_at = datetime.fromisoformat(str(last_synced).rstrip("Z"))
        except (ValueError, TypeError):
            pass
    _manager._last_sync_status = get_pref("shield_last_sync_status")
    _manager._last_sync_error = get_pref("shield_last_sync_error")
    _manager._last_sync_details = get_pref("shield_last_sync_details")
    _register_vault_listeners()
    return _manager


@dataclass
class SyncResult:
    status: str = "pending"  # success, partial, failed, not_configured
    error: Optional[str] = None
    errors: Optional[list] = None  # per-category error messages
    files_updated: Optional[list] = None  # names of files that were updated
    reports_submitted: int = 0
    flags_submitted: int = 0
    scan_results_submitted: int = 0
    dismissals_submitted: int = 0
    analytics_submitted: int = 0


class CloudSyncManager:
    def __init__(self, key_provider: Optional[Callable[[], str]] = None):
        self._base_url: str = ""
        # The API key lives in the vault and is read on every use (never
        # cached), so locking/resetting the vault revokes it immediately.
        self._key_provider: Callable[[], str] = key_provider or (lambda: "")
        self._last_synced_at: Optional[datetime] = None
        self._last_sync_status: Optional[str] = None  # success, partial, failed
        self._last_sync_error: Optional[str] = None
        self._last_sync_details: Optional[dict] = None

    def configure(self, base_url: str, api_key: Optional[str] = None):
        """Set the cloud URL. ``api_key`` is accepted for old callers and
        ignored: the key is read from the vault on each use."""
        self._base_url = base_url.rstrip("/") if base_url else ""

    @property
    def _api_key(self) -> str:
        try:
            return self._key_provider() or ""
        except Exception:
            logger.exception("Could not read the Shield API key")
            return ""

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def url_error(self) -> Optional[str]:
        return validate_cloud_url(self._base_url)

    @property
    def is_configured(self) -> bool:
        return bool(self._base_url and self._api_key)

    @property
    def is_running(self) -> bool:
        from backend.jobs import get_running

        return get_running("cloud_sync") is not None

    @property
    def last_synced_at(self) -> Optional[datetime]:
        return self._last_synced_at

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def sync(self) -> SyncResult:
        """Run a full sync cycle, or join the one already running."""
        if not self.is_configured:
            return SyncResult(status="not_configured")
        if self.url_error:
            return SyncResult(status="failed", error=self.url_error)
        return await await_single("cloud_sync", self._sync_impl)

    async def _sync_impl(self) -> SyncResult:
        result = SyncResult(status="started")
        try:
            manifest = await self._get_manifest()
            if manifest is None:
                result.status = "failed"
                result.error = "Could not reach cloud server"
                self._record_state(result)
                return result

            sync_errors: list[str] = []
            result.files_updated = await self._download_files(manifest, sync_errors)

            steps = [
                ("reports", self._upload_pending_reports, "reports_submitted"),
                ("flags", self._upload_unsent_flags, "flags_submitted"),
                ("scan results", self._upload_scan_results, "scan_results_submitted"),
            ]
            if get_pref("shield_sync_dismissals", False):
                steps.append(("dismissals", self._upload_pending_dismissals, "dismissals_submitted"))
            steps.append(("analytics", self._upload_analytics, "analytics_submitted"))
            for label, fn, attr in steps:
                try:
                    setattr(result, attr, await fn())
                except Exception as e:
                    logger.warning("Cloud sync %s failed: %s", label, e)
                    sync_errors.append(f"{label}: {e}")

            self._last_synced_at = datetime.utcnow()
            result.errors = sync_errors or None
            result.status = "partial" if sync_errors else "success"
            result.error = "; ".join(sync_errors) if sync_errors else None
            logger.info(
                "Cloud sync %s: files=%s reports=%d flags=%d scans=%d dismissals=%d analytics=%d",
                result.status, result.files_updated, result.reports_submitted, result.flags_submitted,
                result.scan_results_submitted, result.dismissals_submitted, result.analytics_submitted,
            )
        except Exception as e:
            logger.warning("Cloud sync failed: %s", e)
            result.status = "failed"
            result.error = str(e)
        self._record_state(result)
        return result

    def _record_state(self, result: SyncResult) -> None:
        self._last_sync_status = result.status
        self._last_sync_error = result.error
        self._last_sync_details = None if result.status == "failed" else {
            "files_updated": result.files_updated or [],
            "reports_submitted": result.reports_submitted,
            "flags_submitted": result.flags_submitted,
            "scan_results_submitted": result.scan_results_submitted,
            "dismissals_submitted": result.dismissals_submitted,
            "analytics_submitted": result.analytics_submitted,
        }

        def _mutate(prefs: dict) -> None:
            prefs["shield_last_synced_at"] = self._last_synced_at.isoformat() if self._last_synced_at else None
            prefs["shield_last_sync_status"] = self._last_sync_status
            prefs["shield_last_sync_error"] = self._last_sync_error
            prefs["shield_last_sync_details"] = self._last_sync_details

        try:
            update_prefs(_mutate)
        except Exception:
            logger.exception("Failed to persist cloud sync state")

    # ------------------------------------------------------------------
    # Downloads
    # ------------------------------------------------------------------

    async def _download_files(self, manifest: dict, sync_errors: list) -> list:
        updated: list[str] = []
        files = manifest.get("files", [])
        if not isinstance(files, list):
            sync_errors.append("manifest: 'files' is not a list")
            return updated

        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            fname = str(file_info.get("name", ""))
            endpoint = str(file_info.get("endpoint", ""))
            remote_version = str(file_info.get("version", ""))
            file_type = file_info.get("type", "json")
            try:
                if not _valid_endpoint(endpoint):
                    raise ValueError(f"invalid endpoint {endpoint!r}")
                if file_type == "model_bundle":
                    m = _BUNDLE_NAME_RE.match(fname)
                    if not m:
                        raise ValueError("invalid model bundle name")
                    domain = m.group(1)
                    target_dir = _contained(DATA_DIR / "ml_models" / domain)
                    if remote_version == self._get_local_version(target_dir / "thresholds.json"):
                        continue
                    await self._download_model_bundle(endpoint, domain, remote_version, file_info)
                else:
                    if not _JSON_NAME_RE.match(fname):
                        raise ValueError("file name not allowed")
                    target = _contained(DATA_DIR / fname)
                    if remote_version == self._get_local_version(target):
                        continue
                    raw = await self._download_bytes(endpoint)
                    verify_artifact(raw, file_info, fname)
                    data = json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError("expected a JSON object")
                    atomic_write_text(target, json.dumps(data, indent=2, ensure_ascii=False), 0o644)
                updated.append(fname)
                logger.info("Synced %s → %s", fname, remote_version)
            except Exception as e:
                logger.warning("Skipping manifest entry %r: %s", fname, e)
                sync_errors.append(f"{fname or '?'}: {e}")

        if updated:
            await self._after_download(updated)
        return updated

    async def _after_download(self, updated: list) -> None:
        from backend.scam.pipeline import _load_signatures_file, get_pipeline_manager
        from backend.scam.tools.search_library import invalidate_cache

        mgr = get_pipeline_manager()
        if "scam_pipeline.json" in updated and mgr:
            mgr.reload()
        if "scam_signatures.json" in updated and mgr:
            _load_signatures_file(mgr.get_config())
        if any(f.startswith("ml_models/") for f in updated):
            try:
                from backend.scam.tools.ml_prefilter import reload_ensemble
                await reload_ensemble()
            except ImportError:
                pass
        for fname in updated:
            if fname not in ("scam_pipeline.json", "scam_signatures.json") and not fname.startswith("ml_models/"):
                invalidate_cache(fname)

    async def _download_bytes(self, path: str, timeout: float = 30.0) -> bytes:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{self._base_url}{path}", headers=self._headers)
            _raise_for_status(resp)
            return resp.content

    async def _download_model_bundle(self, path: str, domain: str, version: str, file_info: dict) -> None:
        """Download a bundle, verify it, extract to a temp dir and swap it in."""
        content = await self._download_bytes(path, timeout=120.0)
        if len(content) > _MAX_BUNDLE_BYTES:
            raise ValueError("model bundle too large")
        verify_artifact(content, file_info, f"ml_models/{domain}.zip")
        await asyncio.to_thread(_install_bundle, content, domain, version)
        logger.info("Installed model bundle for domain '%s' (%s)", domain, version)

    # ------------------------------------------------------------------
    # Uploads (snapshot → close session → POST → mark if unchanged)
    # ------------------------------------------------------------------

    async def _post(self, path: str, payload: dict) -> None:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{self._base_url}{path}", json=payload, headers=self._headers)
            _raise_for_status(resp)

    async def _upload_pending_reports(self) -> int:
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
                payloads = await _report_payloads(db, reports)
            if not payloads:
                continue
            await self._post("/sync/reports", {"reports": [p for _, p in payloads]})
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
        async with AsyncSessionLocal() as db:
            report = (await db.execute(
                select(ScamReport).where(ScamReport.report_id == report_id)
            )).scalars().first()
            payloads = await _report_payloads(db, [report]) if report else []
        if not payloads:
            return False
        try:
            await self._post("/sync/reports", {"reports": [payloads[0][1]]})
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
        total = 0
        last_id = 0
        for _ in range(_MAX_BATCHES):
            async with AsyncSessionLocal() as db:
                flags = (await db.execute(
                    select(ScamFlag).where(
                        ScamFlag.synced_to_cloud == False,  # noqa: E712
                        ScamFlag.id > last_id,
                        telegram_flag_condition(),
                    ).order_by(ScamFlag.id).limit(_BATCH)
                )).scalars().all()
                users = await telegram_user_ids(db, [f.identity_id for f in flags])
            if not flags:
                break
            last_id = flags[-1].id
            flags = [f for f in flags if f.identity_id in users]
            if not flags:
                continue
            payload = {"flags": [{
                "telegram_id": users[f.identity_id],
                "source": f.source,
                "status": f.status,
                "confidence": f.confidence,
                "reason": f.reason,
                "category": f.category or "unknown",
                "flagged_message_text": f.flagged_message_text,
                "flagged_at": f.flagged_at.isoformat() if f.flagged_at else None,
                "dismissed_at": f.dismissed_at.isoformat() if f.dismissed_at else None,
            } for f in flags]}
            await self._post("/sync/flags", payload)
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
        total = 0
        cursor: Optional[tuple] = None
        for _ in range(_MAX_BATCHES):
            conditions = [
                telegram_message_condition(),
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
                refs = await _telegram_refs(
                    db, identity_ids=[m.sender_identity_id for m in msgs],
                    conversation_ids=[m.conversation_id for m in msgs],
                )
            if not msgs:
                break
            cursor = (msgs[-1].scam_updated_at, msgs[-1].id)

            scan_results = []
            for m in msgs:
                trace = _trace(m.scam_trace_json)
                scan_results.append({
                    "chat_id": refs.peers.get(m.conversation_id),
                    "tg_message_id": telegram_message_id(m),
                    "sender_id": refs.users.get(m.sender_identity_id),
                    "scam_score": m.scam_score,
                    "scam_reason": m.scam_reason,
                    "scam_category": m.scam_category,
                    "scan_status": m.scam_status or "unknown",
                    "scan_error": m.scam_error,
                    "manual_verdict": m.scam_manual_verdict,
                    "effective_is_scam": bool(m.effective_is_scam),
                    "date": m.date.isoformat() if m.date else None,
                    "updated_at": m.scam_updated_at.isoformat() if m.scam_updated_at else None,
                    "pipeline_version": trace.get("pipeline_version"),
                    "pipeline_input": _extract_pipeline_input(trace),
                    "models_used": _extract_models_used(trace),
                })
            await self._post("/sync/scan-results", {"scan_results": scan_results})

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
            async with AsyncSessionLocal() as db:
                refs = await _telegram_refs(
                    db,
                    identity_ids=[data.get("identity_id") for _, data in parsed],
                    conversation_ids=[data.get("conversation_id") for _, data in parsed],
                    message_ids=[data.get("message_id") for _, data in parsed],
                )

            # Telegram rows only: a sender flag needs its Telegram user, a scan
            # its Telegram conversation.
            scammer_items, scan_items = [], []
            for d, data in parsed:
                if d.dismissal_type == "scammer":
                    if data.get("identity_id") in refs.users:
                        scammer_items.append((d, data))
                elif data.get("conversation_id") in refs.peers:
                    scan_items.append((d, data))

            if scammer_items:
                await self._post("/sync/dismissals", {"dismissals": [
                    {**{k: v for k, v in data.items() if k != "identity_id"},
                     "telegram_id": refs.users[data["identity_id"]]}
                    for _, data in scammer_items
                ]})
                await self._mark_dismissals([d.id for d, _ in scammer_items])
            if scan_items:
                scan_results = []
                for _, data in scan_items:
                    trace = data.get("trace") or {}
                    scan_results.append({
                        "chat_id": refs.peers.get(data.get("conversation_id")),
                        "tg_message_id": refs.messages.get(data.get("message_id")),
                        "sender_id": refs.users.get(data.get("identity_id")),
                        "scam_score": data.get("scam_score"),
                        "scam_reason": data.get("scam_reason"),
                        "scam_category": data.get("scam_category"),
                        "scan_status": "dismissed",
                        "dismissed": True,
                        "pipeline_version": trace.get("pipeline_version") if isinstance(trace, dict) else None,
                        "pipeline_input": _extract_pipeline_input(trace) if isinstance(trace, dict) else None,
                        "models_used": _extract_models_used(trace) if isinstance(trace, dict) else None,
                        "llm_prompt": data.get("llm_prompt"),
                        "llm_response": data.get("llm_response"),
                        "trace": trace or None,
                    })
                await self._post("/sync/scan-results", {"scan_results": scan_results})
                await self._mark_dismissals([d.id for d, _ in scan_items])
            total += len(scammer_items) + len(scan_items)
        return total

    async def _mark_dismissals(self, ids: list) -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(ScamDismissal).where(ScamDismissal.id.in_(ids)).values(synced_at=datetime.utcnow())
            )
            await db.commit()

    async def upload_feedback(self, feedback_id: int) -> bool:
        """Try to upload a single feedback item immediately. Returns True if synced."""
        if not self.is_configured:
            return False
        if self.url_error:
            raise ValueError(self.url_error)
        async with AsyncSessionLocal() as db:
            fb = (await db.execute(select(ScamFeedback).where(ScamFeedback.id == feedback_id))).scalars().first()
            if not fb:
                return False
            refs = await _telegram_refs(
                db, identity_ids=[fb.related_flag_identity_id],
                conversation_ids=[fb.related_conversation_id], message_ids=[fb.related_message_id],
            )
        # Feedback about another channel's rows waits for cloud contract v2
        if ((fb.related_conversation_id and fb.related_conversation_id not in refs.peers)
                or (fb.related_message_id and fb.related_message_id not in refs.messages)
                or (fb.related_flag_identity_id and fb.related_flag_identity_id not in refs.users)):
            return False
        await self._post("/sync/feedback", {"feedback": [{
            "id": fb.id,
            "feedback_type": fb.feedback_type,
            "message": fb.message,
            "related_chat_id": refs.peers.get(fb.related_conversation_id),
            "related_message_id": refs.messages.get(fb.related_message_id),
            "related_flag_telegram_id": refs.users.get(fb.related_flag_identity_id),
            "created_at": fb.created_at.isoformat() if fb.created_at else None,
        }]})
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
        from backend.scam.analytics import get_analytics_tracker
        tracker = get_analytics_tracker()

        periods = await tracker.get_pending_periods()
        if not periods:
            return 0
        await self._post("/sync/analytics", {"periods": periods})
        await tracker.mark_synced([p["period_start"] for p in periods])
        return len(periods)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_manifest(self) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(f"{self._base_url}/sync/manifest", headers=self._headers)
                if resp.is_success:
                    data = resp.json()
                    return data if isinstance(data, dict) else None
                logger.warning("Manifest request failed: %d %s", resp.status_code, resp.text[:200])
                return None
        except Exception as e:
            logger.warning("Manifest request error: %s", e)
            return None

    def _get_local_version(self, path: Path) -> str:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return str(data.get("version", ""))
        except Exception:
            return ""


async def _report_payloads(db, reports: list) -> list[tuple]:
    """[(report, cloud payload)] for the Telegram reports among ``reports``.
    Stored reports carry internal ids (``messages_json`` entries an
    ``identity_id``, local detection scores a ``message_id``); the payload
    gets the Telegram ids the current cloud contract expects."""
    parsed = {r.id: (_json_list(r.messages_json), _json_obj(r.local_detection_json)) for r in reports}
    identity_ids = {r.reported_identity_id for r in reports}
    message_ids: set = set()
    for msgs, detection in parsed.values():
        identity_ids.update(m.get("identity_id") for m in msgs if isinstance(m, dict))
        message_ids.update(s.get("message_id") for s in (detection or {}).get("scores") or [] if isinstance(s, dict))
    refs = await _telegram_refs(
        db, identity_ids=identity_ids, conversation_ids=[r.reported_in_conversation_id for r in reports],
        message_ids=message_ids,
    )

    out = []
    for r in reports:
        if r.reported_in_conversation_id is not None:
            is_telegram = r.reported_in_conversation_id in refs.peers
        else:
            is_telegram = r.reported_identity_id in refs.users
        if not is_telegram:
            continue
        msgs, detection = parsed[r.id]
        cloud_msgs = []
        for m in msgs:
            if isinstance(m, dict):
                m = {**{k: v for k, v in m.items() if k != "identity_id"},
                     "telegram_id": refs.users.get(m.get("identity_id"))}
            cloud_msgs.append(m)
        if detection is not None:
            detection = {**detection, "scores": [
                {**{k: v for k, v in sc.items() if k != "message_id"},
                 "tg_message_id": refs.messages.get(sc.get("message_id"))}
                for sc in detection.get("scores") or [] if isinstance(sc, dict)
            ]}
        out.append((r, {
            "report_id": r.report_id,
            "reported_telegram_id": refs.users.get(r.reported_identity_id),
            "reported_in_chat_id": refs.peers.get(r.reported_in_conversation_id),
            "chat_type": _CLOUD_KIND.get(r.chat_type, r.chat_type),
            "reporter_verdict": r.reporter_verdict,
            "scam_category": r.scam_category,
            "custom_category": r.custom_category,
            "messages_json": json.dumps(cloud_msgs, default=str),
            "local_detection_json": json.dumps(detection, default=str) if detection is not None else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }))
    return out


def _json_list(raw: Optional[str]) -> list:
    try:
        data = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def _json_obj(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _install_bundle(content: bytes, domain: str, version: str) -> None:
    """Extract a model bundle into a temp dir next to the live one, validate
    it, then swap it in by rename (blocking; run in a thread)."""
    if not _DOMAIN_RE.match(domain):
        raise ValueError(f"invalid model domain {domain!r}")
    models_dir = DATA_DIR / "ml_models"
    models_dir.mkdir(parents=True, exist_ok=True)
    target = _contained(models_dir / domain)
    tmp = _contained(models_dir / f".tmp-{domain}-{uuid.uuid4().hex}")
    old = models_dir / f".old-{domain}-{uuid.uuid4().hex}"
    tmp.mkdir()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                if not _BUNDLE_MEMBER_RE.match(name):
                    raise ValueError(f"unexpected file in bundle: {name!r}")
                dest = _contained(tmp / name)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        if not (tmp / "lgbm.joblib").exists():
            raise ValueError("bundle is missing lgbm.joblib")

        thresholds_file = tmp / "thresholds.json"
        thresholds = {}
        if thresholds_file.exists():
            thresholds = json.loads(thresholds_file.read_text(encoding="utf-8"))
            if not isinstance(thresholds, dict):
                raise ValueError("thresholds.json is not an object")
        thresholds["version"] = version
        atomic_write_text(thresholds_file, json.dumps(thresholds, indent=2), 0o644)

        if target.exists():
            target.rename(old)
        tmp.rename(target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        if old.exists() and not target.exists():
            old.rename(target)
        raise
    shutil.rmtree(old, ignore_errors=True)
