"""
Cloud session lifecycle manager.

Responsibilities:
- On app start (vault unlocked): refresh the JWT silently
- Auto-refresh JWT ~5 minutes before expiry (background task)
- Token rotation: update vault immediately on every refresh
- Offline detection: catch connection errors, set status to 'offline'
- Expired session (>30 days): set status to 'expired', prompt reconnect
"""

import asyncio
import json
import base64
import logging
import time
from typing import Optional

from backend.cloud.client import CloudAPIClient, CloudAPIError
from backend.cloud.auth import (
    get_cloud_credentials,
    save_cloud_credentials,
    clear_cloud_credentials,
    get_cloud_email,
)

log = logging.getLogger(__name__)


def _decode_jwt_exp(token: str) -> Optional[float]:
    """Extract the ``exp`` claim from a JWT without verification.

    Returns the expiry as a Unix timestamp, or None on failure.
    """
    try:
        payload_b64 = token.split(".")[1]
        # Add padding
        padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return float(payload["exp"])
    except Exception:
        return None


class CloudSessionManager:
    """Manages the cloud JWT lifecycle as a background asyncio task."""

    def __init__(self, client: Optional[CloudAPIClient] = None):
        self._client = client or CloudAPIClient()
        self._task: Optional[asyncio.Task] = None
        self._status: str = "disconnected"  # connected | disconnected | offline | expired
        self._access_token: Optional[str] = None
        self._token_exp: Optional[float] = None
        self._stop_event: Optional[asyncio.Event] = None

    def _get_stop_event(self) -> asyncio.Event:
        """Lazy-initialize the stop event within an async context."""
        if self._stop_event is None:
            self._stop_event = asyncio.Event()
        return self._stop_event

    # ── Public properties ─────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._status

    @property
    def access_token(self) -> Optional[str]:
        return self._access_token

    @property
    def email(self) -> Optional[str]:
        return get_cloud_email()

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Call after vault unlock to bootstrap the cloud session."""
        creds = get_cloud_credentials()
        if not creds or not creds.get("refresh_token"):
            self._status = "disconnected"
            return

        # Attempt an initial token refresh
        success = await self._do_refresh(creds["refresh_token"])
        if success:
            self._status = "connected"
            self._start_background_refresh()
        # If refresh failed, status was set inside _do_refresh

    def stop(self) -> None:
        """Stop the background refresh loop (call on app shutdown / vault lock)."""
        self._get_stop_event().set()
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._access_token = None
        self._token_exp = None

    async def on_connect(self, access_token: str) -> None:
        """Called after a successful magic-link connect to activate the session."""
        self._access_token = access_token
        self._token_exp = _decode_jwt_exp(access_token)
        self._status = "connected"
        self._get_stop_event().clear()
        self._start_background_refresh()

    async def on_disconnect(self) -> None:
        """Called after disconnect to tear down the session."""
        self.stop()
        self._status = "disconnected"

    # ── Internal ──────────────────────────────────────────────────────────

    async def _do_refresh(self, refresh_token: str) -> bool:
        """Attempt to refresh tokens.  Returns True on success."""
        try:
            result = await self._client.refresh_tokens(refresh_token)
            new_access = result["access_token"]
            new_refresh = result["refresh_token"]
            # Update vault immediately (token rotation)
            creds = get_cloud_credentials()
            if creds is None:
                # Vault was locked between refresh call and credential read;
                # accept the new tokens in memory but skip vault persistence.
                log.warning("Vault locked during token rotation — skipping vault save")
                self._access_token = new_access
                self._token_exp = _decode_jwt_exp(new_access)
                return True
            save_cloud_credentials(
                access_token=new_access,
                refresh_token=new_refresh,
                email=creds.get("email", ""),
                user_id=creds.get("user_id", ""),
            )
            self._access_token = new_access
            self._token_exp = _decode_jwt_exp(new_access)
            return True
        except CloudAPIError as exc:
            if exc.status_code == 401:
                # Refresh token expired (app closed > 30 days)
                log.warning("Cloud refresh token expired — session marked expired")
                self._status = "expired"
                self._access_token = None
            else:
                log.warning("Cloud refresh failed (API error %s): %s", exc.status_code, exc.detail)
                self._status = "offline"
            return False
        except Exception as exc:
            log.warning("Cloud refresh failed (network): %s", exc)
            self._status = "offline"
            return False

    def _start_background_refresh(self) -> None:
        if self._task and not self._task.done():
            return
        self._get_stop_event().clear()
        self._task = asyncio.ensure_future(self._refresh_loop())

    async def _refresh_loop(self) -> None:
        """Periodically refresh the JWT before it expires."""
        stop = self._get_stop_event()
        while not stop.is_set():
            # Sleep until 5 minutes before expiry, or 50 minutes if unknown
            if self._token_exp:
                wait = max(self._token_exp - time.time() - 300, 60)
            else:
                wait = 50 * 60  # 50 minutes (JWT typically lasts 1 hour)

            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # time to refresh

            creds = get_cloud_credentials()
            if not creds or not creds.get("refresh_token"):
                self._status = "disconnected"
                break

            success = await self._do_refresh(creds["refresh_token"])
            if success:
                self._status = "connected"
            elif self._status == "offline":
                # Retry in 60 seconds when offline
                try:
                    await asyncio.wait_for(stop.wait(), timeout=60)
                    break
                except asyncio.TimeoutError:
                    pass
            else:
                # expired — stop looping
                break


# ── Singleton ─────────────────────────────────────────────────────────────

cloud_session: CloudSessionManager = CloudSessionManager()
