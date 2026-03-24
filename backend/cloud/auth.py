"""
Cloud authentication manager — connect / disconnect / status.

Orchestrates the magic-link flow and persists cloud credentials in the
Fernet vault via ``backend.secrets``.
"""

import asyncio
import logging
from typing import Optional

from backend.cloud.client import CloudAPIClient, CloudAPIError
from backend import secrets

log = logging.getLogger(__name__)

# Vault keys for cloud credentials
_KEY_ACCESS_TOKEN = "cloud_access_token"
_KEY_REFRESH_TOKEN = "cloud_refresh_token"
_KEY_EMAIL = "cloud_email"
_KEY_USER_ID = "cloud_user_id"

_POLL_INTERVAL_S = 3
_POLL_TIMEOUT_S = 15 * 60  # 15 minutes


# ── Vault helpers ─────────────────────────────────────────────────────────

def save_cloud_credentials(
    access_token: str,
    refresh_token: str,
    email: str,
    user_id: str,
) -> None:
    """Persist cloud tokens and identity into the Fernet vault."""
    secrets.save_secret(_KEY_ACCESS_TOKEN, access_token)
    secrets.save_secret(_KEY_REFRESH_TOKEN, refresh_token)
    secrets.save_secret(_KEY_EMAIL, email)
    secrets.save_secret(_KEY_USER_ID, user_id)


def clear_cloud_credentials() -> None:
    """Remove all cloud-related fields from the vault."""
    for key in (_KEY_ACCESS_TOKEN, _KEY_REFRESH_TOKEN, _KEY_EMAIL, _KEY_USER_ID):
        secrets.delete_secret(key)


def get_cloud_credentials() -> Optional[dict]:
    """Return cloud credentials dict or None if not connected."""
    access_token = secrets.get_secret(_KEY_ACCESS_TOKEN)
    refresh_token = secrets.get_secret(_KEY_REFRESH_TOKEN)
    email = secrets.get_secret(_KEY_EMAIL)
    user_id = secrets.get_secret(_KEY_USER_ID)
    if not refresh_token:
        return None
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "email": email,
        "user_id": user_id,
    }


def is_connected() -> bool:
    """Check whether cloud credentials exist in the vault."""
    return secrets.get_secret(_KEY_REFRESH_TOKEN) is not None


def get_cloud_email() -> Optional[str]:
    return secrets.get_secret(_KEY_EMAIL)


# ── Cloud status ──────────────────────────────────────────────────────────

def get_cloud_status() -> str:
    """Return 'connected', 'disconnected', or 'expired'.

    Note: actual expiry detection happens during session refresh (see
    ``session.py``).  This helper provides a quick local check.
    """
    if secrets.vault_status() != "unlocked":
        return "disconnected"
    if not is_connected():
        return "disconnected"
    return "connected"


# ── Connect flow ──────────────────────────────────────────────────────────

async def connect(email: str, client: Optional[CloudAPIClient] = None) -> dict:
    """Start the magic-link flow.

    Returns ``{request_id, claim_secret, expires_in}`` from the cloud API.
    """
    c = client or CloudAPIClient()
    return await c.request_magic_link(email)


async def poll_for_completion(
    request_id: str,
    claim_secret: str,
    client: Optional[CloudAPIClient] = None,
) -> dict:
    """Poll the cloud API until the magic link is consumed or times out.

    Returns the claim payload (includes ``access_token``, ``refresh_token``,
    ``email``, ``user_id`` on success).

    Raises ``CloudAPIError`` or ``TimeoutError``.
    """
    c = client or CloudAPIClient()
    elapsed = 0.0
    while elapsed < _POLL_TIMEOUT_S:
        try:
            result = await c.poll_claim(request_id, claim_secret)
        except CloudAPIError:
            raise
        except Exception as exc:
            log.warning("Cloud poll network error: %s", exc)
            await asyncio.sleep(_POLL_INTERVAL_S)
            elapsed += _POLL_INTERVAL_S
            continue

        status = result.get("status")
        if status == "completed":
            # Persist tokens in vault
            save_cloud_credentials(
                access_token=result["access_token"],
                refresh_token=result["refresh_token"],
                email=result.get("email", ""),
                user_id=result.get("user_id", ""),
            )
            return result
        if status == "expired":
            raise TimeoutError("Magic link expired before it was clicked.")

        # status == "pending" — keep polling
        await asyncio.sleep(_POLL_INTERVAL_S)
        elapsed += _POLL_INTERVAL_S

    raise TimeoutError("Polling timed out waiting for magic link confirmation.")


# ── Disconnect ────────────────────────────────────────────────────────────

async def disconnect(client: Optional[CloudAPIClient] = None) -> None:
    """Revoke the cloud session server-side and clear local credentials."""
    creds = get_cloud_credentials()
    if creds and creds.get("access_token"):
        c = client or CloudAPIClient()
        try:
            await c.logout(
                access_token=creds["access_token"],
                refresh_token=creds.get("refresh_token"),
            )
        except Exception as exc:
            log.warning("Cloud logout request failed (proceeding anyway): %s", exc)
    clear_cloud_credentials()
