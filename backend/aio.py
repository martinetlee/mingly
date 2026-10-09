"""asyncio primitives that are safe to create before the event loop runs.

On Python 3.9 an ``asyncio.Lock`` / ``Event`` / ``Semaphore`` / ``Queue``
binds to ``asyncio.get_event_loop()`` when it is constructed. The packaged
app imports the backend on the main thread and serves on a new loop in
another thread (``backend/main.py``), so a primitive built at import time —
at module level, or in the ``__init__`` of a module-level singleton — works
until its first contended wait, then raises "got Future attached to a
different loop". A primitive cached the first time it is used has the same
problem once a second loop runs (tests, a server restart in one process).

``LoopLocal`` builds the primitive on first use inside the running loop, and
builds a fresh one when the running loop changes. ``LoopLock`` is the lock
form usable directly as ``async with``. Never construct an asyncio primitive
at import time; ``tests/test_aio.py`` scans the backend for it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Generic, Optional, TypeVar

T = TypeVar("T")


class LoopLocal(Generic[T]):
    """One ``factory()`` value per running event loop (the latest one)."""

    def __init__(self, factory: Callable[[], T]) -> None:
        self._factory = factory
        self._value: Optional[T] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def get(self) -> T:
        """The value for the running loop; call only from inside it."""
        loop = asyncio.get_running_loop()
        if self._value is None or self._loop is not loop:
            self._value = self._factory()
            self._loop = loop
        return self._value

    def current(self) -> Optional[T]:
        """The value built on the running loop, or None (never builds one)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return None
        return self._value if self._loop is loop else None


class LoopLock(LoopLocal[asyncio.Lock]):
    """An ``asyncio.Lock`` that may be created at import time."""

    def __init__(self) -> None:
        super().__init__(asyncio.Lock)

    async def __aenter__(self) -> None:
        await self.get().acquire()

    async def __aexit__(self, *exc: Any) -> None:
        self.get().release()

    def locked(self) -> bool:
        lock = self.current()
        return lock is not None and lock.locked()
