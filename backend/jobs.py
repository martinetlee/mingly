"""Single-flight background jobs.

``run_single`` guarantees at most one running task per key: a second caller
gets the already-running task instead of starting a duplicate. Use it for any
background work that must not overlap (briefings, summary generation, cloud
sync, per-message scans, media downloads).
"""

import asyncio
from typing import Any, Awaitable, Callable, Optional

from backend.tasks import spawn

_running: dict[str, asyncio.Task] = {}


def get_running(key: str) -> Optional[asyncio.Task]:
    task = _running.get(key)
    if task is not None and task.done():
        _running.pop(key, None)
        return None
    return task


def run_single(key: str, factory: Callable[[], Awaitable[Any]]) -> tuple[asyncio.Task, bool]:
    """Return (task, started). ``started`` is False if a run was already in flight."""
    existing = get_running(key)
    if existing is not None:
        return existing, False
    task = spawn(factory(), name=f"job:{key}")
    _running[key] = task
    task.add_done_callback(lambda t: _running.pop(key, None) if _running.get(key) is t else None)
    return task, True


async def await_single(key: str, factory: Callable[[], Awaitable[Any]]) -> Any:
    """Join the in-flight run for ``key`` or start one, and await its result."""
    task, _ = run_single(key, factory)
    return await asyncio.shield(task)
