"""Background task registry.

asyncio only keeps weak references to tasks, so a bare
``asyncio.create_task(...)`` can be garbage-collected mid-flight, and an
exception inside it is silently dropped. ``spawn`` retains the task until it
finishes and logs any failure; ``on_error`` lets callers flip user-visible
job state (e.g. a download job dict) to "error" instead of hanging forever.
"""

import asyncio
import logging
from typing import Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task] = set()


def spawn(
    coro: Coroutine,
    *,
    name: Optional[str] = None,
    on_error: Optional[Callable[[BaseException], None]] = None,
) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.error("Background task %s failed", t.get_name(), exc_info=exc)
            if on_error is not None:
                try:
                    on_error(exc)
                except Exception:
                    logger.exception("on_error handler for task %s failed", t.get_name())

    task.add_done_callback(_done)
    return task
