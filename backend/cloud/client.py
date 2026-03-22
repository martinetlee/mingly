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
    """Thin async wrapper around the Mingly Cloud REST API.

    Uses a shared ``httpx.AsyncClient`` instance for connection pooling.
    Call ``aclose()`` on shutdown to release resources.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: float = 30.0):
        self.base_url = (base_url or CLOUD_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._shared_client: Optional[httpx.AsyncClient] = None

    # ── Helpers ───────────────────────────────────────────────────────────

    def _get_shared_client(self) -> httpx.AsyncClient:
        """Return the shared httpx client, creating it lazily."""
        if self._shared_client is None or self._shared_client.is_closed:
            self._shared_client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self._timeout,
            )
        return self._shared_client

    async def aclose(self) -> None:
        """Close the shared HTTP client (call on shutdown)."""
        if self._shared_client and not self._shared_client.is_closed:
            await self._shared_client.aclose()
            self._shared_client = None

    def _auth_headers(self, access_token: Optional[str] = None) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        return headers

    @staticmethod
    async def _handle(resp: httpx.Response) -> dict:
        if resp.is_success:
            return resp.json()
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise CloudAPIError(resp.status_code, detail)

    # ── Internal request wrapper with 401-retry (M3) ─────────────────────

    async def _request(
        self,
        method: str,
        url: str,
        access_token: Optional[str] = None,
        **kwargs,
    ) -> dict:
        """Send a request via the shared client. On 401, attempt one token
        refresh and retry before propagating the error."""
        c = self._get_shared_client()
        extra_headers = kwargs.pop("headers", {})
        headers = {**self._auth_headers(access_token), **extra_headers}
        resp = await c.request(method, url, headers=headers, **kwargs)

        if resp.status_code == 401 and access_token:
            # Attempt one token refresh and retry
            try:
                from backend.cloud.session import cloud_session
                from backend.cloud.auth import get_cloud_credentials
                creds = get_cloud_credentials()
                if creds and creds.get("refresh_token"):
                    refresh_result = await self.refresh_tokens(creds["refresh_token"])
                    new_token = refresh_result.get("access_token")
                    if new_token:
                        cloud_session._access_token = new_token
                        retry_headers = {**self._auth_headers(new_token), **extra_headers}
                        resp = await c.request(method, url, headers=retry_headers, **kwargs)
            except Exception:
                pass  # Fall through to handle original 401

        return await self._handle(resp)

    # ── Auth endpoints ────────────────────────────────────────────────────

    async def request_magic_link(self, email: str) -> dict:
        """POST /auth/magic-link — start the magic-link login flow.

        Returns ``{request_id, claim_secret, expires_in}``.
        """
        return await self._request(
            "POST", "/auth/magic-link", json={"email": email, "purpose": "login"},
        )

    async def poll_claim(self, request_id: str, claim_secret: str) -> dict:
        """GET /auth/magic-link/claim/{request_id} — poll for magic-link completion.

        Returns ``{status: "pending" | "completed" | "expired", ...}``.
        When completed, also includes ``access_token`` and ``refresh_token``.
        """
        return await self._request(
            "GET",
            f"/auth/magic-link/claim/{request_id}",
            headers={"X-Claim-Secret": claim_secret},
        )

    async def refresh_tokens(self, refresh_token: str) -> dict:
        """POST /auth/refresh — exchange a refresh token for new JWT + refresh token.

        Returns ``{access_token, refresh_token}``.
        """
        # Use shared client directly to avoid 401-retry recursion
        c = self._get_shared_client()
        headers = self._auth_headers()
        resp = await c.post("/auth/refresh", headers=headers, json={"refresh_token": refresh_token})
        return await self._handle(resp)

    async def logout(self, access_token: str, refresh_token: Optional[str] = None) -> dict:
        """POST /auth/logout — revoke the current session server-side."""
        body: dict = {}
        if refresh_token:
            body["refresh_token"] = refresh_token
        return await self._request("POST", "/auth/logout", access_token=access_token, json=body)

    # ── User / email endpoints ─────────────────────────────────────────────

    async def get_profile(self, access_token: str) -> dict:
        """GET /me — get current user profile with verified emails."""
        return await self._request("GET", "/me", access_token=access_token)

    async def add_email(self, access_token: str, email: str) -> dict:
        """POST /me/emails — add a new email (triggers verification magic link).

        Returns ``{request_id, claim_secret, message, expires_in}``.
        """
        return await self._request("POST", "/me/emails", access_token=access_token, json={"email": email})

    async def remove_email(
        self, access_token: str, email_id: str, confirm: bool = False
    ) -> dict:
        """DELETE /me/emails/{email_id} — remove a verified email.

        When ``confirm=True``, cascading org membership revocations proceed.
        """
        return await self._request(
            "DELETE",
            f"/me/emails/{email_id}",
            access_token=access_token,
            params={"confirm": str(confirm).lower()},
        )

    async def poll_email_verification(
        self, access_token: str, request_id: str, claim_secret: str
    ) -> dict:
        """Poll the claim endpoint for an email-verification magic link.

        Re-uses the same ``/auth/magic-link/claim/{request_id}`` endpoint
        used for login, but with ``purpose=verify_email``.
        """
        return await self._request(
            "GET",
            f"/auth/magic-link/claim/{request_id}",
            access_token=access_token,
            headers={"X-Claim-Secret": claim_secret},
        )

    # ── Org endpoints ──────────────────────────────────────────────────────

    async def list_orgs(self, access_token: str) -> dict:
        """GET /orgs — list orgs the user belongs to."""
        return await self._request("GET", "/orgs", access_token=access_token)

    async def create_org(self, access_token: str, name: str, domain: str) -> dict:
        """POST /orgs — create a new org (requires verified email on domain)."""
        return await self._request("POST", "/orgs", access_token=access_token, json={"name": name, "domain": domain})

    async def get_org(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id} — get org details."""
        return await self._request("GET", f"/orgs/{org_id}", access_token=access_token)

    async def join_org(self, access_token: str, org_id: str) -> dict:
        """POST /orgs/{org_id}/join — join or request to join an org."""
        return await self._request("POST", f"/orgs/{org_id}/join", access_token=access_token)

    async def list_members(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id}/members — list org members."""
        return await self._request("GET", f"/orgs/{org_id}/members", access_token=access_token)

    async def list_pending(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id}/members/pending — list pending join requests."""
        return await self._request("GET", f"/orgs/{org_id}/members/pending", access_token=access_token)

    async def update_member(
        self, access_token: str, org_id: str, user_id: str, data: dict
    ) -> dict:
        """PATCH /orgs/{org_id}/members/{user_id} — update member role/status."""
        return await self._request("PATCH", f"/orgs/{org_id}/members/{user_id}", access_token=access_token, json=data)

    async def remove_member(
        self, access_token: str, org_id: str, user_id: str
    ) -> dict:
        """DELETE /orgs/{org_id}/members/{user_id} — revoke membership."""
        return await self._request("DELETE", f"/orgs/{org_id}/members/{user_id}", access_token=access_token)

    # ── Shared contacts endpoints ─────────────────────────────────────────

    async def list_shared_contacts(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id}/contacts — list shared contacts for an org."""
        return await self._request("GET", f"/orgs/{org_id}/contacts", access_token=access_token)

    async def create_shared_contact(
        self, access_token: str, org_id: str, data: dict
    ) -> dict:
        """POST /orgs/{org_id}/contacts — contribute a contact to the org.

        ``data`` should contain at minimum ``telegram_id``, ``name``, and
        optionally ``telegram_handle``.  Returns the created contact or
        raises ``CloudAPIError(409)`` if a contact with that telegram_id
        already exists in the org.
        """
        return await self._request("POST", f"/orgs/{org_id}/contacts", access_token=access_token, json=data)

    async def get_shared_contact(
        self, access_token: str, org_id: str, contact_id: str
    ) -> dict:
        """GET /orgs/{org_id}/contacts/{contact_id} — get shared contact detail."""
        return await self._request("GET", f"/orgs/{org_id}/contacts/{contact_id}", access_token=access_token)

    async def update_shared_contact(
        self, access_token: str, org_id: str, contact_id: str, data: dict
    ) -> dict:
        """PATCH /orgs/{org_id}/contacts/{contact_id} — update shared contact info."""
        return await self._request("PATCH", f"/orgs/{org_id}/contacts/{contact_id}", access_token=access_token, json=data)

    # ── Directory (shared external orgs) ──────────────────────────────────

    async def list_directory(self, access_token: str, org_id: str) -> dict:
        """GET /orgs/{org_id}/directory — list shared external orgs."""
        return await self._request("GET", f"/orgs/{org_id}/directory", access_token=access_token)

    async def create_shared_org(
        self, access_token: str, org_id: str, data: dict
    ) -> dict:
        """POST /orgs/{org_id}/directory — create a shared external org.

        ``data`` should contain ``name`` and optionally ``domain``, ``website``.
        """
        return await self._request("POST", f"/orgs/{org_id}/directory", access_token=access_token, json=data)

    # ── Contact org links ─────────────────────────────────────────────────

    async def create_contact_link(
        self, access_token: str, org_id: str, contact_id: str, data: dict
    ) -> dict:
        """POST /orgs/{org_id}/contacts/{contact_id}/links — link contact to external org.

        ``data`` should contain ``shared_org_id`` and optionally ``position``
        and ``relationship_context`` (client/vendor/partner/prospect).
        """
        return await self._request(
            "POST", f"/orgs/{org_id}/contacts/{contact_id}/links",
            access_token=access_token, json=data,
        )

    async def list_contact_links(
        self, access_token: str, org_id: str, contact_id: str
    ) -> dict:
        """GET /orgs/{org_id}/contacts/{contact_id}/links — list contact org links."""
        return await self._request("GET", f"/orgs/{org_id}/contacts/{contact_id}/links", access_token=access_token)

    async def update_contact_link(
        self, access_token: str, org_id: str, contact_id: str, link_id: str, data: dict
    ) -> dict:
        """PATCH /orgs/{org_id}/contacts/{contact_id}/links/{link_id} — update link."""
        async with self._client(access_token=access_token) as c:
            resp = await c.patch(
                f"/orgs/{org_id}/contacts/{contact_id}/links/{link_id}", json=data
            )
            return await self._handle(resp)

    async def delete_contact_link(
        self, access_token: str, org_id: str, contact_id: str, link_id: str
    ) -> dict:
        """DELETE /orgs/{org_id}/contacts/{contact_id}/links/{link_id} — soft-delete link."""
        async with self._client(access_token=access_token) as c:
            resp = await c.delete(
                f"/orgs/{org_id}/contacts/{contact_id}/links/{link_id}"
            )
            return await self._handle(resp)

    # ── Group chat metadata ──────────────────────────────────────────────

    async def list_group_chats(
        self, access_token: str, org_id: str, contact_id: str
    ) -> dict:
        """GET /orgs/{org_id}/contacts/{contact_id}/group-chats — list group chats."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}/contacts/{contact_id}/group-chats")
            return await self._handle(resp)

    # ── Directory detail ─────────────────────────────────────────────────

    async def get_shared_org_detail(
        self, access_token: str, org_id: str, shared_org_id: str
    ) -> dict:
        """GET /orgs/{org_id}/directory/{shared_org_id} — get shared org details + contacts."""
        async with self._client(access_token=access_token) as c:
            resp = await c.get(f"/orgs/{org_id}/directory/{shared_org_id}")
            return await self._handle(resp)

    # ── Backup endpoints ──────────────────────────────────────────────────

    async def upload_backup(self, access_token: str, encrypted_payload: str) -> dict:
        """PUT /me/backup — upload an encrypted personal backup."""
        return await self._request(
            "PUT", "/me/backup", access_token=access_token,
            json={"encrypted_payload": encrypted_payload},
        )

    async def download_backup(self, access_token: str) -> dict:
        """GET /me/backup — download the encrypted personal backup."""
        return await self._request("GET", "/me/backup", access_token=access_token)

    async def delete_backup(self, access_token: str) -> dict:
        """DELETE /me/backup — delete the personal backup from the server."""
        return await self._request("DELETE", "/me/backup", access_token=access_token)
