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

    # ── User / email endpoints ─────────────────────────────────────────────

    async def get_profile(self, access_token: str) -> dict:
        """GET /me — get current user profile with verified emails."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get("/me")
            return await self._handle(resp)

    async def add_email(self, access_token: str, email: str) -> dict:
        """POST /me/emails — add a new email (triggers verification magic link).

        Returns ``{request_id, claim_secret, message, expires_in}``.
        """
        async with self._client(access_token=access_token) as c:
            resp = await c.post("/me/emails", json={"email": email})
            return await self._handle(resp)

    async def remove_email(
        self, access_token: str, email_id: str, confirm: bool = False
    ) -> dict:
        """DELETE /me/emails/{email_id} — remove a verified email.

        When ``confirm=True``, cascading org membership revocations proceed.
        """
        async with self._client(access_token=access_token) as c:
            resp = await c.delete(
                f"/me/emails/{email_id}",
                params={"confirm": str(confirm).lower()},
            )
            return await self._handle(resp)

    async def poll_email_verification(
        self, access_token: str, request_id: str, claim_secret: str
    ) -> dict:
        """Poll the claim endpoint for an email-verification magic link.

        Re-uses the same ``/auth/magic-link/claim/{request_id}`` endpoint
        used for login, but with ``purpose=verify_email``.
        """
        async with self._client(access_token=access_token) as c:
            resp = await c.get(
                f"/auth/magic-link/claim/{request_id}",
                headers={"X-Claim-Secret": claim_secret},
            )
            return await self._handle(resp)

    # ── Org endpoints ──────────────────────────────────────────────────────

    async def list_orgs(self, access_token: str) -> dict:
        """GET /orgs — list orgs the user belongs to."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get("/orgs")
            return await self._handle(resp)

    async def create_org(self, access_token: str, name: str, domain: str) -> dict:
        """POST /orgs — create a new org (requires verified email on domain)."""
        async with self._client(access_token=access_token) as c:
            resp = await c.post("/orgs", json={"name": name, "domain": domain})
            return await self._handle(resp)

    async def get_org(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id} — get org details."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}")
            return await self._handle(resp)

    async def join_org(self, access_token: str, org_id: str) -> dict:
        """POST /orgs/{org_id}/join — join or request to join an org."""
        async with self._client(access_token=access_token) as c:
            resp = await c.post(f"/orgs/{org_id}/join")
            return await self._handle(resp)

    async def list_members(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id}/members — list org members."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}/members")
            return await self._handle(resp)

    async def list_pending(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id}/members/pending — list pending join requests."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}/members/pending")
            return await self._handle(resp)

    async def update_member(
        self, access_token: str, org_id: str, user_id: str, data: dict
    ) -> dict:
        """PATCH /orgs/{org_id}/members/{user_id} — update member role/status."""
        async with self._client(access_token=access_token) as c:
            resp = await c.patch(f"/orgs/{org_id}/members/{user_id}", json=data)
            return await self._handle(resp)

    async def remove_member(
        self, access_token: str, org_id: str, user_id: str
    ) -> dict:
        """DELETE /orgs/{org_id}/members/{user_id} — revoke membership."""
        async with self._client(access_token=access_token) as c:
            resp = await c.delete(f"/orgs/{org_id}/members/{user_id}")
            return await self._handle(resp)

    # ── Shared contacts endpoints ─────────────────────────────────────────

    async def list_shared_contacts(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id}/contacts — list shared contacts for an org."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}/contacts")
            return await self._handle(resp)

    async def create_shared_contact(
        self, access_token: str, org_id: str, data: dict
    ) -> dict:
        """POST /orgs/{org_id}/contacts — contribute a contact to the org.

        ``data`` should contain at minimum ``telegram_id``, ``name``, and
        optionally ``telegram_handle``.  Returns the created contact or
        raises ``CloudAPIError(409)`` if a contact with that telegram_id
        already exists in the org.
        """
        async with self._client(access_token=access_token) as c:
            resp = await c.post(f"/orgs/{org_id}/contacts", json=data)
            return await self._handle(resp)

    async def get_shared_contact(
        self, access_token: str, org_id: str, contact_id: str
    ) -> dict:
        """GET /orgs/{org_id}/contacts/{contact_id} — get shared contact detail."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}/contacts/{contact_id}")
            return await self._handle(resp)

    async def update_shared_contact(
        self, access_token: str, org_id: str, contact_id: str, data: dict
    ) -> dict:
        """PATCH /orgs/{org_id}/contacts/{contact_id} — update shared contact info."""
        async with self._client(access_token=access_token) as c:
            resp = await c.patch(f"/orgs/{org_id}/contacts/{contact_id}", json=data)
            return await self._handle(resp)

    # ── Directory (shared external orgs) ──────────────────────────────────

    async def list_directory(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id}/directory — list shared external orgs."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}/directory")
            return await self._handle(resp)

    async def create_shared_org(
        self, access_token: str, org_id: str, data: dict
    ) -> dict:
        """POST /orgs/{org_id}/directory — create a shared external org.

        ``data`` should contain ``name`` and optionally ``domain``, ``website``.
        """
        async with self._client(access_token=access_token) as c:
            resp = await c.post(f"/orgs/{org_id}/directory", json=data)
            return await self._handle(resp)

    # ── Contact org links ─────────────────────────────────────────────────

    async def create_contact_link(
        self, access_token: str, org_id: str, contact_id: str, data: dict
    ) -> dict:
        """POST /orgs/{org_id}/contacts/{contact_id}/links — link contact to external org.

        ``data`` should contain ``shared_org_id`` and optionally ``position``
        and ``relationship_context`` (client/vendor/partner/prospect).
        """
        async with self._client(access_token=access_token) as c:
            resp = await c.post(
                f"/orgs/{org_id}/contacts/{contact_id}/links", json=data
            )
            return await self._handle(resp)

    async def list_contact_links(
        self, access_token: str, org_id: str, contact_id: str
    ) -> dict:
        """GET /orgs/{org_id}/contacts/{contact_id}/links — list contact org links."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}/contacts/{contact_id}/links")
            return await self._handle(resp)

    # ── Backup endpoints ──────────────────────────────────────────────────

    async def upload_backup(self, access_token: str, encrypted_payload: str) -> dict:
        """PUT /me/backup — upload an encrypted personal backup."""
        async with self._client(access_token=access_token) as c:
            resp = await c.put(
                "/me/backup",
                json={"encrypted_payload": encrypted_payload},
            )
            return await self._handle(resp)

    async def download_backup(self, access_token: str) -> dict:
        """GET /me/backup — download the encrypted personal backup."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get("/me/backup")
            return await self._handle(resp)

    async def delete_backup(self, access_token: str) -> dict:
        """DELETE /me/backup — delete the personal backup from the server."""
        async with self._client(access_token=access_token) as c:
            resp = await c.delete("/me/backup")
            return await self._handle(resp)
