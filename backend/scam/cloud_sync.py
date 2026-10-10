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
* Two upload contracts (docs/multichannel/SHIELD_CLOUD_CONTRACT_V2.md). The
  server's ``contract_versions`` (``GET /sync/capabilities`` or the manifest)
  are cached with a TTL. v1 (the default, and whenever the server does not
  advertise 2) is Telegram-shaped (telegram_id, chat_id, tg_message_id):
  uploads resolve the Telegram ids through ``backend.scam.channel_ids`` and
  skip rows from other channels (they stay unsynced). v2 is channel-neutral
  (``channel``, hashed private handles, opaque refs, per-channel content
  opt-in) and uploads every channel; requests carry ``X-Mingly-Contract: 2``.

This module is the orchestration (``CloudSyncManager``, downloads, the daily
loop); the pieces live beside it: ``cloud_contract`` (URL rules, contract
discovery), ``cloud_assets`` (manifest names, verification, bundle install),
``cloud_payloads`` (upload row selection and v1 / v2 payloads) and
``cloud_upload`` (the upload methods, mixed into the manager).
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import httpx

from backend import secrets as vault
from backend.config import DATA_DIR
from backend.fsutil import atomic_write_text
from backend.jobs import await_single, get_running
from backend.prefs import get_pref, update_prefs
from backend.scam.cloud_assets import (
    BUNDLE_NAME_RE, JSON_NAME_RE, MAX_BUNDLE_BYTES, contained, install_bundle, valid_endpoint, verify_artifact,
)
from backend.scam.cloud_contract import (
    ContractDiscovery, parse_contract_versions, raise_for_status, validate_cloud_url,
)
from backend.scam.cloud_upload import CloudUploads
from backend.scam.pipeline import _load_signatures_file, get_pipeline_manager
from backend.secret_prefs import get_shield_api_key
from backend.tasks import spawn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Contract versions (capability discovery)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# v2 content opt-in
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Which rows each contract carries
# ---------------------------------------------------------------------------


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

    _loop_task = spawn(_sync_loop(), name="cloud_sync_loop")
    return True


def stop_sync_loop() -> None:
    """Stop the loop and abort an upload in flight (vault locked or reset:
    nothing may keep talking to the cloud with the revoked key)."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        _loop_task.cancel()
    _loop_task = None

    running = get_running("cloud_sync")
    if running is not None:
        running.cancel()


def _register_vault_listeners() -> None:

    vault.add_lock_listener(stop_sync_loop)  # reset_vault() locks first
    vault.add_unlock_listener(start_sync_loop)
    if vault.vault_status() == "unlocked":
        start_sync_loop()


def init_cloud_sync_manager() -> "CloudSyncManager":
    global _manager

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


class CloudSyncManager(CloudUploads):
    def __init__(self, key_provider: Optional[Callable[[], str]] = None):
        self._base_url: str = ""
        # The API key lives in the vault and is read on every use (never
        # cached), so locking/resetting the vault revokes it immediately.
        self._key_provider: Callable[[], str] = key_provider or (lambda: "")
        self._last_synced_at: Optional[datetime] = None
        self._last_sync_status: Optional[str] = None  # success, partial, failed
        self._last_sync_error: Optional[str] = None
        self._last_sync_details: Optional[dict] = None
        self._contracts = ContractDiscovery("/sync/capabilities")
        self._transport: Optional[httpx.AsyncBaseTransport] = None  # tests inject one

    def configure(self, base_url: str, api_key: Optional[str] = None):
        """Set the cloud URL. ``api_key`` is accepted for old callers and
        ignored: the key is read from the vault on each use."""
        base_url = base_url.rstrip("/") if base_url else ""
        if base_url != self._base_url:
            self._contracts.reset()
        self._base_url = base_url

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

        return get_running("cloud_sync") is not None

    @property
    def last_sync_outcome(self) -> dict:
        """Status / error / details of the last finished sync."""
        return {
            "last_sync_status": self._last_sync_status,
            "last_sync_error": self._last_sync_error,
            "last_sync_details": self._last_sync_details,
        }

    @property
    def last_synced_at(self) -> Optional[datetime]:
        return self._last_synced_at

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _client(self, timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    async def contract_version(self) -> int:
        """The upload contract to use now: 2 when the server advertises it."""
        if not self._base_url or self.url_error:
            return 1
        return await self._contracts.contract(self._base_url, self._headers, self._transport)

    def cached_contract_version(self) -> int:
        """The last known contract without asking the server (1 if unknown)."""
        versions = self._contracts.cached()
        return 2 if versions and 2 in versions else 1

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

            if "contract_versions" in manifest:
                self._contracts.remember(parse_contract_versions(manifest["contract_versions"]))

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
                if not valid_endpoint(endpoint):
                    raise ValueError(f"invalid endpoint {endpoint!r}")
                if file_type == "model_bundle":
                    m = BUNDLE_NAME_RE.match(fname)
                    if not m:
                        raise ValueError("invalid model bundle name")
                    domain = m.group(1)
                    target_dir = contained(DATA_DIR / "ml_models" / domain, DATA_DIR)
                    if remote_version == self._get_local_version(target_dir / "thresholds.json"):
                        continue
                    await self._download_model_bundle(endpoint, domain, remote_version, file_info)
                else:
                    if not JSON_NAME_RE.match(fname):
                        raise ValueError("file name not allowed")
                    target = contained(DATA_DIR / fname, DATA_DIR)
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
        async with self._client(timeout) as client:
            resp = await client.get(f"{self._base_url}{path}", headers=self._headers)
            raise_for_status(resp)
            return resp.content

    async def _download_model_bundle(self, path: str, domain: str, version: str, file_info: dict) -> None:
        """Download a bundle, verify it, extract to a temp dir and swap it in."""
        content = await self._download_bytes(path, timeout=120.0)
        if len(content) > MAX_BUNDLE_BYTES:
            raise ValueError("model bundle too large")
        verify_artifact(content, file_info, f"ml_models/{domain}.zip")
        await asyncio.to_thread(install_bundle, content, domain, version, DATA_DIR)
        logger.info("Installed model bundle for domain '%s' (%s)", domain, version)

    # ------------------------------------------------------------------
    # Uploads (snapshot → close session → POST → mark if unchanged)
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get_manifest(self) -> Optional[dict]:
        try:
            async with self._client(15.0) as client:
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


# ---------------------------------------------------------------------------
# Payload builders: v1 (Telegram ids) and v2 (channel-neutral)
# ---------------------------------------------------------------------------


