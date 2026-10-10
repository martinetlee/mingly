"""Server-sent event responses (the HTTP side of ``briefing.events``)."""

import asyncio
import logging
from typing import AsyncIterator

from fastapi.responses import StreamingResponse

from backend.briefing.events import error_payload, sse

logger = logging.getLogger(__name__)


def sse_response(events: AsyncIterator[str]) -> StreamingResponse:
    """Wrap an SSE generator so ANY exception ends the stream with an
    ``event: error`` instead of a truncated response."""

    async def guarded() -> AsyncIterator[str]:
        try:
            async for chunk in events:
                yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("SSE stream failed")
            yield sse("error", error_payload(exc))

    return StreamingResponse(guarded(), media_type="text/event-stream")
