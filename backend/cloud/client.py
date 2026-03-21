"""
HTTP client for the Mingly Cloud API.

All methods are async and use httpx.  The base URL is configurable via the
MINGLY_CLOUD_URL environment variable (defaults to https://cloud.mingly.io).
"""

import os
from typing import Optional

import httpx


_DEFAULT_BASE_URL = "https://cloud.mingly.io"

CLOUD_BASE_URL: str = os.getenv("MINGLY_CLOUD_URL", _DEFAULT_BASE_URL)


class CloudAPIError(Exception):
    """Raised when the cloud API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Cloud API error {status_code}: {detail}")


class CloudAPIClient:
    """Thin async wrapper around the Mingly Cloud REST API."""

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        self.base_url = (base_url or CLOUD_BASE_URL).rstrip("/")
        self._timeout = timeout

    # ── Helpers ───────────────────────────────────────────────────────────

    def _client(self, access_token: Optional[str] = None) -> httpx.AsyncClient:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=self._timeout,
        )

    @staticmethod
    async def _handle(resp: httpx.Response) -> dict:
        if resp.is_success:
            return resp.json()
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise CloudAPIError(resp.status_code, detail)

    # ── Auth endpoints ────────────────────────────────────────────────────

    async def request_magic_link(self, email: str) -> dict:
        """POST /auth/magic-link — start the magic-link login flow.

        Returns ``{request_id, claim_secret, expires_in}``.
        """
        async with self._client() as c:
            resp = await c.post("/auth/magic-link", json={"email": email, "purpose": "login"})
            return await self._handle(resp)

    async def poll_claim(self, request_id: str, claim_secret: str) -> dict:
        """GET /auth/magic-link/claim/{request_id} — poll for magic-link completion.

        Returns ``{status: "pending" | "completed" | "expired", ...}``.
        When completed, also includes ``access_token`` and ``refresh_token``.
        """
        async with self._client() as c:
            resp = await c.get(
                f"/auth/magic-link/claim/{request_id}",
                headers={"X-Claim-Secret": claim_secret},
            )
            return await self._handle(resp)

    async def refresh_tokens(self, refresh_token: str) -> dict:
        """POST /auth/refresh — exchange a refresh token for new JWT + refresh token.

        Returns ``{access_token, refresh_token}``.
        """
        async with self._client() as c:
            resp = await c.post("/auth/refresh", json={"refresh_token": refresh_token})
            return await self._handle(resp)

    async def logout(self, access_token: str, refresh_token: Optional[str] = None) -> dict:
        """POST /auth/logout — revoke the current session server-side."""
        async with self._client(access_token=access_token) as c:
            body: dict = {}
            if refresh_token:
                body["refresh_token"] = refresh_token
            resp = await c.post("/auth/logout", json=body)
            return await self._handle(resp)
