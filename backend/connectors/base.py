"""The contract every messaging channel (Telegram, Gmail, Signal, …) implements.

A connector owns one account's network session: its lifecycle, its sign-in
flow and what it can do. Everything a channel stores goes through the shared
persistence + ``backend.ingest.fanout`` path, so features never need to know
which channel a message came from.

Sign-in is a small state machine driven by the frontend: ``begin_auth``
returns the first ``AuthStep`` and each ``continue_auth`` answers the current
step with the next one, until a ``done`` (or ``error``) step. One generic UI
renders every channel's flow — form fields (Telegram phone/code/2FA), a QR
code to scan (Signal device linking) or a browser consent page (Google OAuth).
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, Optional


class Capability(str, enum.Enum):
    SEND = "send"
    EDIT = "edit"
    DELETE = "delete"
    REACT = "react"
    REPLY = "reply"
    MEDIA_SEND = "media_send"
    READ_RECEIPTS = "read_receipts"
    FORUM_TOPICS = "forum_topics"
    ADMIN_MODERATION = "admin_moderation"
    HISTORY_BACKFILL = "history_backfill"
    SUBJECT = "subject"
    HTML_BODY = "html_body"
    LABELS = "labels"
    TYPING = "typing"
    PRESENCE = "presence"


@dataclass
class AuthField:
    name: str
    label: str
    kind: str = "text"  # text | password | phone | code
    required: bool = True
    placeholder: Optional[str] = None


@dataclass
class AuthStep:
    flow_id: str
    kind: str  # fields | qr | oauth_browser | done | error
    title: Optional[str] = None
    message: Optional[str] = None
    fields: list[AuthField] = field(default_factory=list)
    qr_payload: Optional[str] = None  # e.g. sgnl://linkdevice?…
    auth_url: Optional[str] = None  # consent page to open in the system browser
    poll: bool = False  # continue_auth with {} until the step changes

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConnectorStatus:
    state: str  # stopped | starting | unauthorized | syncing | ready | reconnecting | error
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuthFlowError(Exception):
    """An auth flow id that is unknown or already finished."""


class Connector(ABC):
    channel: ClassVar[str]
    display_name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]

    @property
    def account_key(self) -> str:
        """Stable id of the account this connector serves. One account per
        channel until accounts are stored; then the account row id."""
        return f"{self.channel}:default"

    @abstractmethod
    def is_configured(self) -> bool:
        """Whether credentials exist, so ``start`` has something to start."""

    @abstractmethod
    async def start(self) -> bool:
        """Idempotently bring the session up. True when signed in and live,
        False when the account still needs ``begin_auth``. Must return quickly
        when not configured; long syncs run in the background."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background work and drop the network session."""

    @abstractmethod
    def status(self) -> ConnectorStatus: ...

    @abstractmethod
    async def begin_auth(self, params: dict[str, Any]) -> AuthStep: ...

    @abstractmethod
    async def continue_auth(self, flow_id: str, data: dict[str, Any]) -> AuthStep:
        """Raises AuthFlowError for an unknown or finished flow."""

    @abstractmethod
    async def logout(self) -> None:
        """Sign the account out and forget its stored session."""

    def describe(self) -> dict[str, Any]:
        return {
            "account_key": self.account_key,
            "channel": self.channel,
            "display_name": self.display_name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "configured": self.is_configured(),
            "status": self.status().to_dict(),
        }
