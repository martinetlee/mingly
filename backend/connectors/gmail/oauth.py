"""Google OAuth for the Gmail connector: bring-your-own Desktop client,
authorization code + PKCE (S256) through the system browser, loopback
redirect to ``/api/connectors/gmail/oauth/callback``.

A sign-in creates a ``PendingAuth`` keyed by its random ``state``; the
callback route exchanges the code and parks the tokens on it, and the
connector's polling ``continue_auth`` picks them up. Credentials end up in
the vault under keys namespaced by account id (``vault_key``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode

import httpx

from backend import secrets as vault

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
)
CALLBACK_PATH = "/api/connectors/gmail/oauth/callback"
PENDING_TTL = 15 * 60  # seconds a consent page stays answerable
MAX_PENDING = 16

# Tests swap in an httpx.MockTransport; production uses the default.
transport: Optional[httpx.AsyncBaseTransport] = None


def http_client(timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout, transport=transport)


# Token-endpoint ``error`` codes meaning the grant or the client is gone: only
# signing in again (or fixing the client) helps.
GRANT_LOST_ERRORS = frozenset({"invalid_grant", "invalid_client", "unauthorized_client"})


class OAuthError(Exception):
    """Google refused the request; ``code`` is the token endpoint's ``error``."""

    def __init__(self, message: str, code: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def grant_lost(self) -> bool:
        """The refresh token or the OAuth client no longer works."""
        return self.code in GRANT_LOST_ERRORS


class OAuthUnreachable(OAuthError):
    """Google could not be reached or is unavailable (network, 5xx, 429); worth retrying."""


def redirect_uri() -> str:
    from backend.config import API_PORT
    return f"http://127.0.0.1:{API_PORT}{CALLBACK_PATH}"


def vault_key(account_id: int, name: str) -> str:
    """``gmail:<account_id>:client_id`` | ``client_secret`` | ``refresh_token``."""
    return f"gmail:{account_id}:{name}"


@dataclass
class Credentials:
    client_id: str
    client_secret: str
    refresh_token: Optional[str] = None

    @classmethod
    def load(cls, account_id: int) -> Optional["Credentials"]:
        cid = vault.get_secret(vault_key(account_id, "client_id"))
        secret = vault.get_secret(vault_key(account_id, "client_secret"))
        if not cid or not secret:
            return None
        return cls(cid, secret, vault.get_secret(vault_key(account_id, "refresh_token")))

    def save(self, account_id: int) -> None:
        vault.save_secret(vault_key(account_id, "client_id"), self.client_id)
        vault.save_secret(vault_key(account_id, "client_secret"), self.client_secret)
        if self.refresh_token:
            vault.save_secret(vault_key(account_id, "refresh_token"), self.refresh_token)


def forget(account_id: int, *, keep_client: bool) -> None:
    vault.delete_secret(vault_key(account_id, "refresh_token"))
    if not keep_client:
        vault.delete_secret(vault_key(account_id, "client_id"))
        vault.delete_secret(vault_key(account_id, "client_secret"))


# -- pending browser consents --------------------------------------------------

@dataclass
class PendingAuth:
    flow_id: str
    client_id: str
    client_secret: str
    verifier: str
    state: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    created: float = field(default_factory=time.monotonic)
    refresh_token: Optional[str] = None
    access_token: Optional[str] = None
    expires_in: int = 0
    error: Optional[str] = None
    # The one code exchange of this consent; later callbacks (a reload, a
    # second tab) wait for it instead of exchanging or failing it again.
    exchange: Optional["asyncio.Future[None]"] = field(default=None, repr=False, compare=False)

    @property
    def finished(self) -> bool:
        return self.access_token is not None or self.error is not None

    def auth_url(self) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": self.state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        })


_pending: dict[str, PendingAuth] = {}  # state -> consent


def start(flow_id: str, client_id: str, client_secret: str) -> PendingAuth:
    _expire()
    p = PendingAuth(flow_id, client_id, client_secret, verifier=secrets.token_urlsafe(64))
    _pending[p.state] = p
    while len(_pending) > MAX_PENDING:
        _pending.pop(next(iter(_pending)))
    return p


def by_flow(flow_id: str) -> Optional[PendingAuth]:
    _expire()
    return next((p for p in _pending.values() if p.flow_id == flow_id), None)


def discard(p: PendingAuth) -> None:
    _pending.pop(p.state, None)


def _expire() -> None:
    now = time.monotonic()
    for state in [s for s, p in _pending.items() if now - p.created > PENDING_TTL]:
        _pending.pop(state, None)


async def complete(state: Optional[str], code: Optional[str], error: Optional[str]) -> Optional[PendingAuth]:
    """The browser came back from Google. Returns the consent it answers
    (with tokens or an error recorded), or None for an unknown state."""
    _expire()
    p = _pending.get(state or "")
    if p is None:
        return None
    if p.exchange is None and not p.finished:
        p.exchange = asyncio.ensure_future(_answer(p, code, error))  # set before any await
    if p.exchange is not None:
        # Shielded: a callback request that goes away doesn't cancel the exchange.
        await asyncio.shield(p.exchange)
    return p


async def _answer(p: PendingAuth, code: Optional[str], error: Optional[str]) -> None:
    """Record the browser's answer on ``p``: tokens, or an error (never
    both from different callbacks; only the first callback runs this)."""
    if error or not code:
        p.error = "Access was denied in the browser" if error == "access_denied" else f"Google sign-in failed ({error or 'no code'})"
        return
    try:
        tokens = await _token_request({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": p.verifier,
            "client_id": p.client_id,
            "client_secret": p.client_secret,
            "redirect_uri": redirect_uri(),
        })
    except OAuthError as e:
        p.error = str(e)
        return
    except Exception as e:  # never leave the consent unanswered (the dialog would poll forever)
        p.error = f"Google sign-in failed ({e!r})"
        return
    p.access_token = tokens["access_token"]
    p.refresh_token = tokens.get("refresh_token")
    p.expires_in = int(tokens.get("expires_in", 3600))
    if not p.refresh_token:
        p.error = "Google did not return a refresh token; remove Mingly's access in your Google account and try again"


# -- tokens --------------------------------------------------------------------

async def _token_request(data: dict) -> dict:
    try:
        async with http_client() as client:
            r = await client.post(TOKEN_URL, data=data)
    except httpx.HTTPError as e:
        raise OAuthUnreachable(f"Could not reach Google: {e}") from e
    try:
        body = r.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if r.status_code != 200 or "access_token" not in body:
        code = body.get("error") if isinstance(body.get("error"), str) else None
        if r.status_code >= 500 or r.status_code == 429:
            raise OAuthUnreachable(f"Google's sign-in service is unavailable (HTTP {r.status_code})", code)
        err = body.get("error_description") or code or f"HTTP {r.status_code}"
        raise OAuthError(f"Google rejected the request: {err}", code)
    return body


async def refresh(creds: Credentials) -> "tuple[str, int]":
    """A fresh access token and its lifetime (seconds)."""
    if not creds.refresh_token:
        raise OAuthError("Not signed in", "invalid_grant")
    body = await _token_request({
        "grant_type": "refresh_token",
        "refresh_token": creds.refresh_token,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
    })
    return body["access_token"], int(body.get("expires_in", 3600))


async def revoke(token: str) -> None:
    """Best effort: tell Google to drop the grant."""
    try:
        async with http_client(timeout=10.0) as client:
            await client.post(REVOKE_URL, data={"token": token})
    except httpx.HTTPError:
        pass
