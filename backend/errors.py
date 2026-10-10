"""Domain errors that cross layers. The API layer maps every ``DomainError``
to an HTTP response (``api/router.py``: status ``status_code``, body
``{"detail": message}``, plus ``headers``); nothing below it knows about HTTP.
"""

from typing import Optional


class DomainError(Exception):
    status_code = 500
    default_message = "Error"

    def __init__(self, message: str = ""):
        super().__init__(message or self.default_message)

    @property
    def detail(self) -> str:
        return str(self)

    @property
    def headers(self) -> Optional[dict]:
        return None


class ServiceUnavailable(DomainError):
    """A dependency the request needs (a channel's live client, …) is
    unavailable right now."""
    status_code = 503
    default_message = "Service unavailable"


class NotAuthenticated(DomainError):
    """The channel session exists but is not signed in."""
    status_code = 401
    default_message = "Not authenticated"


class NotFound(DomainError):
    status_code = 404
    default_message = "Not found"


class RateLimited(DomainError):
    """The channel asked us to wait ``retry_after`` seconds."""
    status_code = 429

    def __init__(self, retry_after: int, message: str = ""):
        self.retry_after = int(retry_after)
        super().__init__(message or f"Rate limited — retry in {self.retry_after}s")

    @property
    def headers(self) -> Optional[dict]:
        return {"Retry-After": str(self.retry_after)}
