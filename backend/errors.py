"""Domain errors that cross layers. The API layer maps them to HTTP
responses (``api/router.py``); nothing below it knows about HTTP."""


class ServiceUnavailable(Exception):
    """A dependency the request needs (a channel's live client, …) is
    unavailable right now. The API answers 503 with the message as detail."""

    default_message = "Service unavailable"

    def __init__(self, message: str = ""):
        super().__init__(message or self.default_message)

    @property
    def detail(self) -> str:
        return str(self)
