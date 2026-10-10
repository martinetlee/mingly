"""The HTTP contract with the Shield cloud: which URLs may be used, error
bodies, and which upload contract versions the server speaks
(``GET /sync/capabilities``, cached with a TTL)."""

import logging
import time
from typing import Callable, Iterable, Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def raise_for_status(resp: httpx.Response) -> None:
    """Like resp.raise_for_status() but includes the response body in the error."""
    if resp.is_success:
        return
    try:
        detail = resp.json()
    except Exception:
        detail = resp.text[:500]
    raise httpx.HTTPStatusError(
        f"{resp.status_code} {resp.reason_phrase} — {detail}",
        request=resp.request,
        response=resp,
    )


def validate_cloud_url(url: str) -> Optional[str]:
    """Return an error message if ``url`` may not be used for cloud sync."""
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return None
    if parsed.scheme == "http" and parsed.hostname in _LOCAL_HOSTS:
        return None
    return "Cloud URL must use https (plain http is only allowed for localhost)"


CONTRACT_HEADER = "X-Mingly-Contract"


CAPABILITIES_TTL = 3600.0  # a server's answer (including "no such endpoint")


CAPABILITIES_RETRY_TTL = 300.0  # after an error: v1 until then, then ask again


def parse_contract_versions(value) -> frozenset:
    """``contract_versions`` as advertised; anything unusable means v1."""
    versions = frozenset(
        v for v in (value if isinstance(value, list) else [])
        if isinstance(v, int) and not isinstance(v, bool) and v > 0
    )
    return versions or frozenset({1})


class ContractDiscovery:
    """Which upload contract versions a server speaks, cached with a TTL.

    ``GET <base><path>`` → ``{"contract_versions": [1, 2]}``. A server
    without the endpoint (404/405/501) is a v1 server; any other failure
    also means v1, re-asked sooner. Nothing is ever retried within a call.
    """

    def __init__(self, path: str, *, clock: Callable[[], float] = time.monotonic):
        self._path = path
        self._clock = clock
        self._versions: Optional[frozenset] = None
        self._expires_at = 0.0

    def reset(self) -> None:
        self._versions = None
        self._expires_at = 0.0

    def remember(self, versions: Iterable[int], ttl: float = CAPABILITIES_TTL) -> None:
        self._versions = frozenset(versions)
        self._expires_at = self._clock() + ttl

    def cached(self) -> Optional[frozenset]:
        if self._versions is not None and self._clock() < self._expires_at:
            return self._versions
        return None

    async def versions(self, base_url: str, headers: dict, transport=None) -> frozenset:
        cached = self.cached()
        if cached is not None:
            return cached
        ttl = CAPABILITIES_RETRY_TTL
        versions = frozenset({1})
        try:
            async with httpx.AsyncClient(timeout=10.0, transport=transport) as client:
                resp = await client.get(f"{base_url}{self._path}", headers=headers)
            if resp.is_success:
                data = resp.json()
                versions = parse_contract_versions(data.get("contract_versions") if isinstance(data, dict) else None)
                ttl = CAPABILITIES_TTL
            elif resp.status_code in (404, 405, 501):
                ttl = CAPABILITIES_TTL
            else:
                logger.warning("Capabilities request failed: %d", resp.status_code)
        except Exception as e:
            logger.warning("Capabilities request error: %s", e)
        self.remember(versions, ttl)
        return versions

    async def contract(self, base_url: str, headers: dict, transport=None) -> int:
        """2 when the server advertises it, else 1."""
        return 2 if 2 in await self.versions(base_url, headers, transport) else 1
