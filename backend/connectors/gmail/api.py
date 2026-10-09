"""Thin async client for the Gmail REST API (v1) over httpx.

Access tokens are refreshed on demand from the stored refresh token; a 401
forces one refresh and retry. Errors are classified so the sync loop can
decide between backing off (429 / 5xx / network), re-syncing (404 on a
stale historyId) and asking the user to sign in again (revoked grant).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import httpx

from backend.connectors.gmail import oauth

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


class GmailAuthError(GmailError):
    """The grant is gone (revoked / expired): the user must sign in again."""


class GmailNotFound(GmailError):
    pass


class GmailApi:
    def __init__(self, creds: oauth.Credentials, access_token: Optional[str] = None, expires_in: int = 0):
        self.creds = creds
        self._token = access_token
        self._expires_at = time.monotonic() + expires_in - 60 if access_token else 0.0
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = oauth.http_client(timeout=60.0)
        return self._client

    async def _access_token(self, force: bool = False) -> str:
        async with self._lock:
            if force or not self._token or time.monotonic() >= self._expires_at:
                try:
                    token, ttl = await oauth.refresh(self.creds)
                except oauth.OAuthUnreachable as e:
                    raise GmailError(str(e)) from e
                except oauth.OAuthError as e:
                    raise GmailAuthError(str(e), 401) from e
                self._token = token
                self._expires_at = time.monotonic() + ttl - 60
            return self._token

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        url = path if path.startswith("http") else BASE + path
        for attempt in (0, 1):
            token = await self._access_token(force=attempt == 1)
            try:
                r = await self._http().request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
            except httpx.HTTPError as e:
                raise GmailError(f"Network error: {e}") from e
            if r.status_code == 401 and attempt == 0:
                continue
            break
        if r.status_code == 401:
            raise GmailAuthError("Google rejected the access token", 401)
        if r.status_code == 404:
            raise GmailNotFound("Not found", 404)
        if r.status_code >= 400:
            retry_after = None
            if r.headers.get("retry-after", "").isdigit():
                retry_after = float(r.headers["retry-after"])
            try:
                detail = r.json().get("error", {}).get("message") or r.text
            except ValueError:
                detail = r.text
            # 403 rateLimitExceeded / userRateLimitExceeded behave like 429
            status = 429 if r.status_code == 403 and "rate" in (detail or "").lower() else r.status_code
            raise GmailError(f"Gmail API {r.status_code}: {detail[:200]}", status, retry_after)
        if not r.content:
            return {}
        return r.json()

    # -- endpoints -------------------------------------------------------------

    async def profile(self) -> dict:
        return await self.request("GET", "/profile")

    async def labels(self) -> list[dict]:
        return (await self.request("GET", "/labels")).get("labels", [])

    async def list_threads(self, q: str, page_token: Optional[str] = None, max_results: int = 100) -> dict:
        params = {"q": q, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return await self.request("GET", "/threads", params=params)

    async def get_thread(self, thread_id: str) -> dict:
        return await self.request("GET", f"/threads/{thread_id}", params={"format": "full"})

    async def get_message(self, message_id: str) -> dict:
        return await self.request("GET", f"/messages/{message_id}", params={"format": "full"})

    async def get_attachment(self, message_id: str, attachment_id: str) -> dict:
        return await self.request("GET", f"/messages/{message_id}/attachments/{attachment_id}")

    async def history(self, start_history_id: str, page_token: Optional[str] = None) -> dict:
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        return await self.request("GET", "/history", params=params)

    async def modify_thread(self, thread_id: str, add: list[str], remove: list[str]) -> dict:
        return await self.request(
            "POST", f"/threads/{thread_id}/modify", json={"addLabelIds": add, "removeLabelIds": remove},
        )

    async def send(self, raw: str, thread_id: Optional[str]) -> dict:
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return await self.request("POST", "/messages/send", json=body)
