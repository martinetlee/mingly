"""Single owner of ``prefs.json``: cached reads, atomic writes, corruption safety.

Reads are served from an in-memory copy that is revalidated with one
``stat()`` (so an external edit is still picked up) instead of re-parsing the
file on every getter call. Saves write atomically and refresh the cache.

A corrupt file is never silently discarded: the first read that fails to
parse copies it to ``prefs.json.corrupt-<timestamp>`` and logs an error, and
only then do defaults ({}) apply. The next save can overwrite the original
because the backup preserves its contents.

Read-modify-write: callers must not ``await`` between ``load_prefs()`` and
``save_prefs()``; the single event loop is what keeps those cycles atomic.
``update_prefs(fn)`` does the cycle under a lock for code that may also run
in worker threads.
"""

import copy
import json
import logging
import os
import shutil
import threading
from datetime import datetime
from typing import Any, Callable, Optional, Tuple

from backend.config import DATA_DIR
from backend.fsutil import atomic_write_text

logger = logging.getLogger(__name__)

PREFS_FILE = DATA_DIR / "prefs.json"

_lock = threading.RLock()
_cache: Optional[dict] = None
_cache_sig: Optional[Tuple[int, int, int]] = None


def _signature() -> Optional[Tuple[int, int, int]]:
    try:
        st = os.stat(PREFS_FILE)
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _backup_corrupt(reason: str) -> None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = PREFS_FILE.with_name(f"{PREFS_FILE.name}.corrupt-{stamp}")
    try:
        shutil.copy2(PREFS_FILE, backup)
        os.chmod(backup, 0o600)
        logger.error("prefs.json is corrupt (%s); backed up to %s and using defaults", reason, backup)
    except OSError:
        logger.exception("prefs.json is corrupt (%s) and could not be backed up", reason)


def _read_current() -> dict:
    """Return the cached prefs dict, reloading if the file changed. Caller holds _lock."""
    global _cache, _cache_sig
    sig = _signature()
    if _cache is not None and sig == _cache_sig:
        return _cache
    data: dict = {}
    if sig is not None:
        try:
            parsed = json.loads(PREFS_FILE.read_text())
            if isinstance(parsed, dict):
                data = parsed
            else:
                _backup_corrupt(f"top level is {type(parsed).__name__}, not an object")
        except (ValueError, OSError) as exc:
            _backup_corrupt(str(exc))
    _cache, _cache_sig = data, sig
    return data


def load_prefs() -> dict:
    """Return a private copy of all prefs (safe for the caller to mutate)."""
    with _lock:
        return copy.deepcopy(_read_current())


def get_pref(key: str, default: Any = None) -> Any:
    """Read one pref without copying the whole dict."""
    with _lock:
        return copy.deepcopy(_read_current().get(key, default))


def save_prefs(data: dict) -> None:
    global _cache, _cache_sig
    with _lock:
        atomic_write_text(PREFS_FILE, json.dumps(data), 0o600)
        _cache, _cache_sig = copy.deepcopy(data), _signature()


def update_prefs(mutate: Callable[[dict], None]) -> dict:
    """Atomically load, mutate in place, and save. Returns the saved dict."""
    with _lock:
        data = copy.deepcopy(_read_current())
        mutate(data)
        save_prefs(data)
        return data


def invalidate_cache() -> None:
    global _cache, _cache_sig
    with _lock:
        _cache = _cache_sig = None
