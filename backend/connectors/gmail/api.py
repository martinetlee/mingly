"""Thin async client for the Gmail REST API (v1) — and Google Calendar (v3),
which rides on the same grant — over httpx.

Access tokens are refreshed on demand from the stored refresh token; a 401
forces one refresh and retry. Errors are classified so the sync loop can
decide between backing off (429 / 5xx / network / a per-minute quota, also
at the token endpoint), re-syncing (404 on a stale historyId) and asking the
user to sign in again (only a revoked grant or a deleted client).

Gmail meters each user in quota units per minute (threads.get with full
bodies costs 120 of a 6,000 budget), so every Gmail request first takes its
cost from a ``QuotaBudget``; background sync leaves headroom for user actions.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import quote

import httpx

from backend.connectors.gmail import oauth

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"

# Gmail's per-user limit for projects created since May 2026 (older projects
# have more; pacing to this one is safe for all), and quota units per method
# (developers.google.com/gmail/api/reference/quota).
QUOTA_PER_MINUTE = 6000
BACKGROUND_SHARE = 0.8
QUOTA_WINDOW = 60.0
COST_THREAD_FULL = 120
COST_MESSAGE_FULL = 60
COST_LIST_THREADS = 10
COST_HISTORY = 2
COST_ATTACHMENT = 20
COST_LABELS = 1
COST_PROFILE = 1
COST_MODIFY = 10
COST_SEND = 100
# Google's reasons for a per-minute quota hit; it answers them with 403 or 429
QUOTA_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
QUOTA_MESSAGE = "Gmail's per-minute limit was reached; Mingly continues in a minute"


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


class QuotaBudget:
    """Quota units spent in the last minute. ``take`` waits until a
    request's cost fits; background requests stop at ``BACKGROUND_SHARE`` of
    the limit so an interactive one (send, open an attachment) never queues
    behind a sync."""

    def __init__(
        self, per_minute: int = QUOTA_PER_MINUTE, *, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ):
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._spent: deque[tuple[float, int]] = deque()

    def used(self) -> int:
        cutoff = self._clock() - QUOTA_WINDOW
        while self._spent and self._spent[0][0] <= cutoff:
            self._spent.popleft()
        return sum(cost for _, cost in self._spent)

    async def take(self, cost: int, *, background: bool) -> None:
        limit = self.per_minute * (BACKGROUND_SHARE if background else 1.0)
        while True:
            used = self.used()
            # An empty window always admits one request, whatever its cost
            if not self._spent or used + cost <= limit:
                self._spent.append((self._clock(), cost))
                return
            await self._sleep(max(self._spent[0][0] + QUOTA_WINDOW - self._clock(), 0.05))


class GmailApi:
    def __init__(
        self, creds: oauth.Credentials, access_token: Optional[str] = None, expires_in: int = 0,
        budget: Optional[QuotaBudget] = None,
    ):
        self.creds = creds
        self.budget = budget or QuotaBudget()
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
                    raise GmailError(str(e)) from e  # retryable
                except oauth.OAuthError as e:
                    if e.grant_lost:
                        raise GmailAuthError(str(e), 401) from e
                    raise GmailError(str(e), 400) from e
                self._token = token
                self._expires_at = time.monotonic() + ttl - 60
            return self._token

    async def request(self, method: str, path: str, *, cost: int = 0, background: bool = False, **kwargs: Any) -> Any:
        """One API call; ``cost`` is its Gmail quota units (Calendar is a
        separate quota and passes none), ``background`` marks sync traffic."""
        url = path if path.startswith("http") else BASE + path
        if cost:
            await self.budget.take(cost, background=background)
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
                error = r.json().get("error") or {}
            except ValueError:
                error = {}
            if not isinstance(error, dict):
                error = {}
            detail = str(error.get("message") or r.text)
            service = "Google Calendar API" if url.startswith(CALENDAR_BASE) else "Gmail API"
            if r.status_code in (403, 429) and _is_rate_limit(error):
                # The quota is per minute: wait for the window when Google names no time
                message = QUOTA_MESSAGE if service == "Gmail API" else f"{service}: per-minute limit reached"
                raise GmailError(message, 429, retry_after or QUOTA_WINDOW)
            raise GmailError(f"{service} {r.status_code}: {detail[:200]}", r.status_code, retry_after)
        if not r.content:
            return {}
        return r.json()

    # -- endpoints -------------------------------------------------------------

    async def profile(self) -> dict:
        return await self.request("GET", "/profile", cost=COST_PROFILE, background=True)

    async def labels(self) -> list[dict]:
        return (await self.request("GET", "/labels", cost=COST_LABELS, background=True)).get("labels", [])

    async def list_threads(self, q: str, page_token: Optional[str] = None, max_results: int = 50) -> dict:
        params = {"q": q, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        return await self.request("GET", "/threads", params=params, cost=COST_LIST_THREADS, background=True)

    async def get_thread(self, thread_id: str, *, background: bool = False) -> dict:
        return await self.request(
            "GET", f"/threads/{thread_id}", params={"format": "full"}, cost=COST_THREAD_FULL, background=background,
        )

    async def get_message(self, message_id: str) -> dict:
        return await self.request("GET", f"/messages/{message_id}", params={"format": "full"}, cost=COST_MESSAGE_FULL)

    async def get_attachment(self, message_id: str, attachment_id: str) -> dict:
        return await self.request("GET", f"/messages/{message_id}/attachments/{attachment_id}", cost=COST_ATTACHMENT)

    async def history(self, start_history_id: str, page_token: Optional[str] = None) -> dict:
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": ["messageAdded", "messageDeleted", "labelAdded", "labelRemoved"],
            "maxResults": 500,
        }
        if page_token:
            params["pageToken"] = page_token
        return await self.request("GET", "/history", params=params, cost=COST_HISTORY, background=True)

    async def modify_thread(self, thread_id: str, add: list[str], remove: list[str]) -> dict:
        return await self.request(
            "POST", f"/threads/{thread_id}/modify", json={"addLabelIds": add, "removeLabelIds": remove},
            cost=COST_MODIFY,
        )

    # -- Google Calendar (v3), same grant ------------------------------------------

    async def calendar_list(self) -> list[dict]:
        items: list[dict] = []
        page: Optional[str] = None
        while True:
            params: dict[str, Any] = {"maxResults": 250}
            if page:
                params["pageToken"] = page
            resp = await self.request("GET", f"{CALENDAR_BASE}/users/me/calendarList", params=params)
            items += resp.get("items") or []
            page = resp.get("nextPageToken")
            if not page:
                return items

    async def list_events(self, calendar_id: str, params: dict) -> dict:
        return await self.request("GET", f"{CALENDAR_BASE}/calendars/{quote(calendar_id, safe='')}/events", params=params)

    async def get_event(self, calendar_id: str, event_id: str) -> dict:
        return await self.request(
            "GET", f"{CALENDAR_BASE}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
        )

    async def calendar_colors(self) -> dict:
        """Google's colour palettes: ``{"calendar": {id: {background, …}}, "event": {…}}``."""
        return await self.request("GET", f"{CALENDAR_BASE}/colors")

    async def send(self, raw: str, thread_id: Optional[str]) -> dict:
        body: dict[str, Any] = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return await self.request("POST", "/messages/send", json=body, cost=COST_SEND)


def _is_rate_limit(error: dict) -> bool:
    """A per-minute quota hit (Google's ``errors[].reason`` / ``status``),
    as opposed to a permission problem or an exhausted daily quota."""
    reasons = {e.get("reason") for e in error.get("errors") or [] if isinstance(e, dict)}
    for d in error.get("details") or []:
        if isinstance(d, dict) and d.get("reason"):
            reasons.add(d["reason"])
    if reasons & QUOTA_REASONS or "RATE_LIMIT_EXCEEDED" in reasons:
        return True
    return error.get("status") == "RESOURCE_EXHAUSTED" or "per minute" in str(error.get("message") or "").lower()
