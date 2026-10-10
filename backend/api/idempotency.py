"""Request helpers shared by the send/compose routes (chats and mail)."""

import os
import time
from typing import Optional

from backend.jobs import await_single

# Completed idempotent sends: client_nonce → (expires_at, response)
_NONCE_TTL = 600
_completed: dict[str, tuple[float, dict]] = {}


def cached_response(nonce: Optional[str]) -> Optional[dict]:
    """The stored response of a completed send with this nonce, if any."""
    now = time.monotonic()
    for k in [k for k, (exp, _) in _completed.items() if exp < now]:
        _completed.pop(k, None)
    hit = _completed.get(nonce) if nonce else None
    return hit[1] if hit else None


async def idempotent(nonce: Optional[str], factory) -> dict:
    """Run ``factory`` once per client nonce: a retry while the first attempt is
    in flight joins it, a retry after success gets the same response."""
    if not nonce:
        return await factory()
    hit = cached_response(nonce)
    if hit is not None:
        return hit
    result = await await_single(f"compose:{nonce}", factory)
    _completed[nonce] = (time.monotonic() + _NONCE_TTL, result)
    return result


def cleanup_temp_files(paths: list[str]) -> None:
    """Delete uploaded temp files (best effort)."""
    for path in paths:
        try:
            os.unlink(path)
        except OSError:
            pass
