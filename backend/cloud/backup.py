"""
Encrypted personal backup — client-side encryption and sync.

Key derivation (spec 6.1):
  Master password -> PBKDF2 (100k iterations, SHA-256,
                              salt="mingly-backup-v1" + user_id)
                  -> 256-bit backup key

Encryption (spec 6.2):
  AES-256-GCM encrypt(contact_data_json) -> ciphertext -> base64

Sync flow (spec 7.5):
  - enable_backup: derive key, encrypt all contacts, upload
  - sync_backup: re-encrypt and upload (debounced, every 30 min if dirty)
  - restore_backup: download, derive key, decrypt, return contacts
  - on_password_change: re-derive key, re-encrypt, upload
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from backend import secrets
from backend.cloud.auth import get_cloud_credentials
from backend.cloud.client import CloudAPIError
from backend.cloud.session import cloud_session

log = logging.getLogger(__name__)

_VAULT_KEY_BACKUP_ENABLED = "cloud_backup_enabled"
_VAULT_KEY_BACKUP_LAST_SYNC = "cloud_backup_last_sync"
_VAULT_KEY_BACKUP_KEY = "cloud_backup_key"

_SYNC_INTERVAL_S = 30 * 60  # 30 minutes


# ── Key derivation (spec 6.1) ────────────────────────────────────────────────

def derive_backup_key(master_password: str, user_id: str) -> bytes:
    """Derive a 256-bit AES key from the master password.

    Uses PBKDF2-HMAC-SHA256 with 100k iterations.
    Salt: "mingly-backup-v1" + user_id

    NOTE: The salt is deterministic (uses public user_id) by design.
    This allows the user to re-derive the key on any device without
    storing the salt separately — only the master password is needed
    for recovery.
    """
    salt = f"mingly-backup-v1{user_id}".encode("utf-8")
    key = hashlib.pbkdf2_hmac(
        "sha256",
        master_password.encode("utf-8"),
        salt,
        iterations=100_000,
        dklen=32,
    )
    return key


# ── Encryption / Decryption (spec 6.2) ───────────────────────────────────────

def encrypt_backup(data: dict, key: bytes) -> str:
    """AES-256-GCM encrypt contact data dict -> base64 string."""
    plaintext = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    # Prepend nonce to ciphertext for storage
    payload = nonce + ciphertext
    return base64.b64encode(payload).decode("ascii")


def decrypt_backup(b64_payload: str, key: bytes) -> dict:
    """Decrypt a base64-encoded AES-256-GCM payload back to a dict."""
    raw = base64.b64decode(b64_payload)
    nonce = raw[:12]
    ciphertext = raw[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext)


# ── Contact data serialization ───────────────────────────────────────────────

async def _get_all_contacts_data() -> dict:
    """Fetch all local contacts + positions and serialize for backup."""
    from backend.db.database import AsyncSessionLocal
    from backend.db.models import Contact, ContactPosition
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        # Contacts
        result = await db.execute(select(Contact))
        contacts = result.scalars().all()

        contact_list = []
        for c in contacts:
            contact_list.append({
                "telegram_id": c.telegram_id,
                "first_name": c.first_name,
                "last_name": c.last_name,
                "username": c.username,
                "phone": c.phone,
                "twitter_handle": c.twitter_handle,
                "linkedin_url": c.linkedin_url,
                "notes": c.notes,
                "is_important": c.is_important,
                "relationship_status": c.relationship_status,
                "relationship_status_manual": c.relationship_status_manual,
                "last_interaction_at": c.last_interaction_at.isoformat() if c.last_interaction_at else None,
                "last_outgoing_at": c.last_outgoing_at.isoformat() if c.last_outgoing_at else None,
                "last_incoming_at": c.last_incoming_at.isoformat() if c.last_incoming_at else None,
            })

        # Positions
        result = await db.execute(select(ContactPosition))
        positions = result.scalars().all()

        position_list = []
        for p in positions:
            position_list.append({
                "telegram_id": p.telegram_id,
                "organization": p.organization,
                "title": p.title,
                "is_active": p.is_active,
                "ended_at": p.ended_at.isoformat() if p.ended_at else None,
            })

    return {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "contacts": contact_list,
        "positions": position_list,
    }


async def _import_contacts_from_backup(data: dict) -> dict:
    """Import contacts and positions from a decrypted backup payload into the local DB.

    Upserts by telegram_id for contacts and by (telegram_id, organization) for positions.
    Returns a summary dict with counts: new, updated, skipped (for both contacts and positions).
    """
    from backend.db.database import AsyncSessionLocal
    from backend.db.models import Contact, ContactPosition
    from sqlalchemy import select

    contacts_list = data.get("contacts", [])
    positions_list = data.get("positions", [])

    # Fields that we update on contacts (excluding telegram_id which is the match key)
    _CONTACT_FIELDS = [
        "first_name", "last_name", "username", "phone",
        "twitter_handle", "linkedin_url", "notes", "is_important",
        "relationship_status", "relationship_status_manual",
        "last_interaction_at", "last_outgoing_at", "last_incoming_at",
    ]
    _DATETIME_FIELDS = {"last_interaction_at", "last_outgoing_at", "last_incoming_at", "ended_at"}

    contacts_new = 0
    contacts_updated = 0
    contacts_skipped = 0
    positions_new = 0
    positions_updated = 0
    positions_skipped = 0

    async with AsyncSessionLocal() as db:
        # ── Import contacts ──────────────────────────────────────────────
        for c_data in contacts_list:
            telegram_id = c_data.get("telegram_id")
            if not telegram_id:
                contacts_skipped += 1
                continue

            try:
                result = await db.execute(
                    select(Contact).where(Contact.telegram_id == telegram_id)
                )
                existing = result.scalar_one_or_none()

                if existing:
                    # Update existing contact fields
                    changed = False
                    for field in _CONTACT_FIELDS:
                        if field not in c_data:
                            continue
                        value = c_data[field]
                        # Parse datetime strings back to datetime objects
                        if field in _DATETIME_FIELDS and isinstance(value, str):
                            value = datetime.fromisoformat(value)
                        if getattr(existing, field) != value:
                            setattr(existing, field, value)
                            changed = True
                    if changed:
                        contacts_updated += 1
                    else:
                        contacts_skipped += 1
                else:
                    # Create new contact
                    kwargs = {"telegram_id": telegram_id}
                    for field in _CONTACT_FIELDS:
                        if field not in c_data:
                            continue
                        value = c_data[field]
                        if field in _DATETIME_FIELDS and isinstance(value, str):
                            value = datetime.fromisoformat(value)
                        kwargs[field] = value
                    db.add(Contact(**kwargs))
                    contacts_new += 1
            except Exception as exc:
                log.warning("Failed to import contact telegram_id=%s: %s", telegram_id, exc)
                contacts_skipped += 1

        # ── Import positions ─────────────────────────────────────────────
        for p_data in positions_list:
            telegram_id = p_data.get("telegram_id")
            organization = p_data.get("organization")
            if not telegram_id or not organization:
                positions_skipped += 1
                continue

            try:
                result = await db.execute(
                    select(ContactPosition).where(
                        ContactPosition.telegram_id == telegram_id,
                        ContactPosition.organization == organization,
                    )
                )
                existing = result.scalar_one_or_none()

                if existing:
                    changed = False
                    for field in ("title", "is_active", "ended_at"):
                        if field not in p_data:
                            continue
                        value = p_data[field]
                        if field in _DATETIME_FIELDS and isinstance(value, str):
                            value = datetime.fromisoformat(value)
                        if getattr(existing, field) != value:
                            setattr(existing, field, value)
                            changed = True
                    if changed:
                        positions_updated += 1
                    else:
                        positions_skipped += 1
                else:
                    kwargs = {
                        "telegram_id": telegram_id,
                        "organization": organization,
                    }
                    for field in ("title", "is_active", "ended_at"):
                        if field not in p_data:
                            continue
                        value = p_data[field]
                        if field in _DATETIME_FIELDS and isinstance(value, str):
                            value = datetime.fromisoformat(value)
                        kwargs[field] = value
                    db.add(ContactPosition(**kwargs))
                    positions_new += 1
            except Exception as exc:
                log.warning("Failed to import position telegram_id=%s org=%s: %s", telegram_id, organization, exc)
                positions_skipped += 1

        await db.commit()

    summary = {
        "contacts": {"new": contacts_new, "updated": contacts_updated, "skipped": contacts_skipped},
        "positions": {"new": positions_new, "updated": positions_updated, "skipped": positions_skipped},
    }
    log.info("Backup import complete: %s", summary)
    return summary


# ── BackupManager ─────────────────────────────────────────────────────────────

class BackupManager:
    """Manages encrypted backup lifecycle: enable, sync, restore, key change."""

    def __init__(self):
        self._dirty: bool = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    def _get_stop_event(self) -> asyncio.Event:
        """Lazy-initialize the stop event within an async context."""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        return self._stop_event

    # ── Properties ─────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        val = secrets.get_secret(_VAULT_KEY_BACKUP_ENABLED)
        return val == "true"

    @property
    def last_sync(self) -> Optional[str]:
        return secrets.get_secret(_VAULT_KEY_BACKUP_LAST_SYNC)

    # ── Dirty tracking ────────────────────────────────────────────────────

    def mark_dirty(self) -> None:
        """Called when contacts change to flag that a sync is needed.

        NOTE: This is not yet wired into contact mutation paths (create,
        update, delete).  For now, auto-sync only works with manual trigger
        via the /backup/sync endpoint.  Wire into contact CRUD when ready.
        """
        self._dirty = True

    # ── Enable / Disable ──────────────────────────────────────────────────

    async def enable_backup(self, master_password: str) -> dict:
        """Enable backup: derive key, encrypt all contacts, upload.

        Returns status dict.
        """
        creds = get_cloud_credentials()
        if not creds or not creds.get("user_id"):
            raise RuntimeError("Not connected to cloud")

        user_id = creds["user_id"]
        token = cloud_session.access_token
        if not token:
            raise RuntimeError("Cloud session not active")

        # Derive key and store it (not the password) in the vault
        key = derive_backup_key(master_password, user_id)
        secrets.save_secret(_VAULT_KEY_BACKUP_KEY, base64.b64encode(key).decode("ascii"))
        secrets.save_secret(_VAULT_KEY_BACKUP_ENABLED, "true")

        # Best-effort password clearing (Python strings are immutable, so the
        # original object may remain in memory until GC; this only rebinds the name)
        master_password = ""  # noqa: F841

        # Encrypt and upload
        contact_data = await _get_all_contacts_data()
        encrypted = encrypt_backup(contact_data, key)

        client = cloud_session.client
        await client.upload_backup(token, encrypted)

        now = datetime.now(timezone.utc).isoformat()
        secrets.save_secret(_VAULT_KEY_BACKUP_LAST_SYNC, now)
        self._dirty = False

        # Start background sync
        self._start_sync_task()

        return {"status": "enabled", "last_sync": now}

    async def disable_backup(self, delete_remote: bool = False) -> dict:
        """Disable backup sync. Optionally delete the remote backup."""
        self._stop_sync_task()

        if delete_remote:
            token = cloud_session.access_token
            if token:
                try:
                    client = cloud_session.client
                    await client.delete_backup(token)
                except Exception as exc:
                    log.warning("Failed to delete remote backup: %s", exc)

        secrets.delete_secret(_VAULT_KEY_BACKUP_KEY)
        secrets.delete_secret(_VAULT_KEY_BACKUP_ENABLED)
        secrets.delete_secret(_VAULT_KEY_BACKUP_LAST_SYNC)
        self._dirty = False

        return {"status": "disabled"}

    # ── Sync ──────────────────────────────────────────────────────────────

    async def sync_backup(self) -> dict:
        """Re-encrypt and upload current contacts."""
        if not self.enabled:
            return {"status": "not_enabled"}

        key_b64 = secrets.get_secret(_VAULT_KEY_BACKUP_KEY)
        if not key_b64:
            return {"status": "error", "detail": "Backup key not found in vault"}

        creds = get_cloud_credentials()
        if not creds or not creds.get("user_id"):
            return {"status": "error", "detail": "Not connected to cloud"}

        token = cloud_session.access_token
        if not token:
            return {"status": "error", "detail": "Cloud session not active"}

        key = base64.b64decode(key_b64)
        contact_data = await _get_all_contacts_data()
        encrypted = encrypt_backup(contact_data, key)

        client = cloud_session.client
        await client.upload_backup(token, encrypted)

        now = datetime.now(timezone.utc).isoformat()
        secrets.save_secret(_VAULT_KEY_BACKUP_LAST_SYNC, now)
        self._dirty = False

        return {"status": "synced", "last_sync": now}

    # ── Restore ───────────────────────────────────────────────────────────

    async def restore_backup(self, master_password: str) -> dict:
        """Download encrypted backup, decrypt, and import contacts into the local DB.

        Upserts contacts by telegram_id (inserts new, updates existing).
        Returns a summary with counts of new, updated, and skipped contacts.
        """
        creds = get_cloud_credentials()
        if not creds or not creds.get("user_id"):
            raise RuntimeError("Not connected to cloud")

        token = cloud_session.access_token
        if not token:
            raise RuntimeError("Cloud session not active")

        user_id = creds["user_id"]
        key = derive_backup_key(master_password, user_id)

        client = cloud_session.client
        result = await client.download_backup(token)
        encrypted_payload = result.get("encrypted_payload")
        if not encrypted_payload:
            return {"status": "no_backup"}

        try:
            data = decrypt_backup(encrypted_payload, key)
        except Exception:
            return {"status": "decrypt_failed", "detail": "Wrong password or corrupted backup"}

        # Cache derived key in vault for future syncs
        secrets.save_secret(_VAULT_KEY_BACKUP_KEY, base64.b64encode(key).decode("ascii"))
        secrets.save_secret(_VAULT_KEY_BACKUP_ENABLED, "true")

        # Best-effort password clearing (Python strings are immutable, so the
        # original object may remain in memory until GC; this only rebinds the name)
        master_password = ""  # noqa: F841

        # Import contacts into local database
        import_summary = await _import_contacts_from_backup(data)

        # Start background sync now that backup is enabled
        self._start_sync_task()

        return {"status": "restored", "import_summary": import_summary}

    # ── Password change (spec 6.5) ────────────────────────────────────────

    async def on_password_change(
        self, old_password: str, new_password: str
    ) -> dict:
        """Re-derive key with new password, re-encrypt, upload."""
        if not self.enabled:
            return {"status": "not_enabled"}

        creds = get_cloud_credentials()
        if not creds or not creds.get("user_id"):
            return {"status": "error", "detail": "Not connected to cloud"}

        token = cloud_session.access_token
        if not token:
            return {"status": "error", "detail": "Cloud session not active"}

        user_id = creds["user_id"]

        # Derive new key
        new_key = derive_backup_key(new_password, user_id)

        # Clear passwords from memory
        old_password = ""  # noqa: F841
        new_password = ""  # noqa: F841

        # Re-encrypt current contacts with new key (no need to decrypt old backup)
        contact_data = await _get_all_contacts_data()
        encrypted = encrypt_backup(contact_data, new_key)

        client = cloud_session.client
        await client.upload_backup(token, encrypted)

        # Update cached derived key in vault
        secrets.save_secret(_VAULT_KEY_BACKUP_KEY, base64.b64encode(new_key).decode("ascii"))

        now = datetime.now(timezone.utc).isoformat()
        secrets.save_secret(_VAULT_KEY_BACKUP_LAST_SYNC, now)

        return {"status": "rekeyed", "last_sync": now}

    # ── Background sync task ──────────────────────────────────────────────

    def _start_sync_task(self) -> None:
        if self._task and not self._task.done():
            return
        stop = self._get_stop_event()
        stop.clear()
        self._task = asyncio.ensure_future(self._sync_loop())

    def _stop_sync_task(self) -> None:
        stop = self._get_stop_event()
        stop.set()
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _sync_loop(self) -> None:
        """Periodically sync if contacts are dirty."""
        stop = self._get_stop_event()
        while not stop.is_set():
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=_SYNC_INTERVAL_S
                )
                break  # stop event set
            except asyncio.TimeoutError:
                pass  # time to check

            if self._dirty and self.enabled:
                try:
                    await self.sync_backup()
                    log.info("Background backup sync completed")
                except Exception as exc:
                    log.warning("Background backup sync failed: %s", exc)

    def start_if_enabled(self) -> None:
        """Call on app start (after vault unlock) to resume sync if enabled."""
        if self.enabled:
            self._start_sync_task()

    def stop(self) -> None:
        """Call on app shutdown or vault lock."""
        self._stop_sync_task()

    # ── Status ────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "last_sync": self.last_sync,
            "dirty": self._dirty,
        }


# ── Singleton ─────────────────────────────────────────────────────────────────

backup_manager = BackupManager()
