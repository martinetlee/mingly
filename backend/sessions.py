"""Session tokens: the in-memory set of tokens issued on vault unlock.

Every vault unlock/init issues a new token and ADDS it to the set of valid
tokens, so a second app window doesn't invalidate the first. Each token
expires after an idle period (prefs ``session_idle_timeout_minutes``, default
12h, 0 = never). Locking or resetting the vault revokes every token.

Transport-neutral: the HTTP dependency (``api/dependencies``) and the
WebSocket manager (``ws/manager``) both validate against this store. Code that
holds connections tied to a token (WebSockets) registers a revocation
listener, so this module never imports them.
"""

import hmac
import logging
import secrets
import time
from typing import Callable, Dict, List, Optional

from backend import prefs
from backend import secrets as vault

logger = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_MINUTES = 720
MAX_SESSIONS = 16

# token -> last activity (time.monotonic())
_sessions: Dict[str, float] = {}
_latest_token: Optional[str] = None

# Called (synchronously) whenever tokens were dropped: expired, evicted or revoked.
_revocation_listeners: List[Callable[[], None]] = []


def add_revocation_listener(fn: Callable[[], None]) -> None:
    if fn not in _revocation_listeners:
        _revocation_listeners.append(fn)


def _notify_revoked() -> None:
    for fn in list(_revocation_listeners):
        try:
            fn()
        except Exception:
            logger.exception("session revocation listener failed")


def get_idle_timeout_minutes() -> int:
    raw = prefs.get_pref("session_idle_timeout_minutes", DEFAULT_IDLE_TIMEOUT_MINUTES)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_IDLE_TIMEOUT_MINUTES
    return max(0, value)


def _expired(last_seen: float, now: float, timeout_s: float) -> bool:
    return timeout_s > 0 and now - last_seen > timeout_s


def _prune(now: float) -> bool:
    """Drop idle-expired tokens. Returns True if any were removed."""
    timeout_s = get_idle_timeout_minutes() * 60
    stale = [t for t, seen in _sessions.items() if _expired(seen, now, timeout_s)]
    for t in stale:
        _sessions.pop(t, None)
    return bool(stale)


def _lookup(token: Optional[str]) -> Optional[str]:
    """Constant-time match of *token* against the active set."""
    if not token:
        return None
    match = None
    for t in _sessions:
        if hmac.compare_digest(t, token):
            match = t
    return match


def generate_session_token() -> str:
    """Issue a new session token (called when the vault is unlocked/initialized).

    Existing tokens stay valid; the least recently used is evicted beyond
    MAX_SESSIONS.
    """
    global _latest_token
    now = time.monotonic()
    pruned = _prune(now)
    token = secrets.token_urlsafe(32)
    _sessions[token] = now
    evicted = False
    while len(_sessions) > MAX_SESSIONS:
        oldest = min(_sessions, key=_sessions.get)  # type: ignore[arg-type]
        _sessions.pop(oldest, None)
        evicted = True
    _latest_token = token
    if pruned or evicted:
        _notify_revoked()
    return token


def is_valid_session_token(token: Optional[str], touch: bool = False) -> bool:
    """True if *token* is active and not idle-expired. ``touch`` records activity."""
    match = _lookup(token)
    if match is None:
        return False
    now = time.monotonic()
    if _expired(_sessions[match], now, get_idle_timeout_minutes() * 60):
        _sessions.pop(match, None)
        return False
    if touch:
        _sessions[match] = now
    return True


def has_sessions() -> bool:
    return bool(_sessions)


def get_session_token() -> Optional[str]:
    """Return the most recently issued token if it is still valid, else None."""
    if _latest_token is not None and is_valid_session_token(_latest_token):
        return _latest_token
    return None


def active_session_count() -> int:
    _prune(time.monotonic())
    return len(_sessions)


def clear_session_token() -> None:
    """Revoke every session (vault lock/reset); revocation listeners run."""
    global _latest_token
    had_sessions = bool(_sessions)
    _sessions.clear()
    _latest_token = None
    if had_sessions:
        _notify_revoked()


def revoke(token: Optional[str] = None) -> None:
    """Drop one session (*token*) or all of them. Unlike ``clear_session_token``
    this does not run the listeners: the async caller closes sockets itself."""
    global _latest_token
    if token is None:
        _sessions.clear()
        _latest_token = None
        return
    match = _lookup(token)
    if match is not None:
        _sessions.pop(match, None)
        if match == _latest_token:
            _latest_token = None


# Locking or resetting the vault ends every session.
vault.add_lock_listener(clear_session_token)
