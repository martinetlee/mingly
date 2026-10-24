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

import asyncio
import enum
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, Optional

from backend.aio import LoopLocal


class Capability(str, enum.Enum):
    SEND = "send"
    EDIT = "edit"
    DELETE = "delete"
    REACT = "react"
    REPLY = "reply"
    PIN = "pin"
    FORWARD = "forward"
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
    MEMBERS = "members"  # a group's member list can be listed


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
    kind: str  # fields | qr | oauth_browser | progress | done | error
    title: Optional[str] = None
    message: Optional[str] = None
    fields: list[AuthField] = field(default_factory=list)
    qr_payload: Optional[str] = None  # e.g. sgnl://linkdevice?…
    auth_url: Optional[str] = None  # consent page to open in the system browser
    poll: bool = False  # continue_auth with {} until the step changes
    progress: Optional[float] = None  # 0–1 for a `progress` step (a setup download)
    submit_label: Optional[str] = None  # button of a `fields` step; "Continue" when None
    # The step's background work (a setup download) keeps going when the user
    # leaves the flow; this labels the button that stops it instead
    # (``abandon_flow(stop=True)``). None: nothing to stop.
    stop_label: Optional[str] = None
    # The secondary button of a `fields` step ("Cancel" when None), e.g.
    # "Skip" for an optional step after signing in
    cancel_label: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SetupStatus:
    """One-time setup a channel runs before its first sign-in (Signal's
    signal-cli download), shown next to the channel while no flow is open."""
    state: str  # running | cancelling | ready | failed
    progress: Optional[float] = None  # 0–1 while running
    message: Optional[str] = None

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


class UnsupportedOperation(Exception):
    """The connector can't do this (the API answers 409 unsupported_channel)."""


@dataclass
class OutgoingFile:
    path: str  # local temp file, owned by the caller
    filename: str
    mime_type: Optional[str] = None


@dataclass
class OutgoingMessage:
    text: str = ""
    reply_to_message_id: Optional[int] = None  # internal id, same conversation
    topic_id: Optional[int] = None  # forum topic (channels with FORUM_TOPICS)
    subject: Optional[str] = None  # email
    # Email recipients (handles). A non-empty ``to`` is the complete To list;
    # empty = the channel's default (a reply goes to the sender / Reply-To).
    to: list[str] = field(default_factory=list)
    cc: list[str] = field(default_factory=list)
    files: list[OutgoingFile] = field(default_factory=list)


@dataclass
class SentFiles:
    """Result of ``send_files``: the stored message ids (in order) and one
    report per file: ``{"filename", "status": "sent"|"failed", "message_id"?,
    "error"?}``."""
    message_ids: list[int]
    files: list[dict]

    @property
    def all_sent(self) -> bool:
        return all(f["status"] == "sent" for f in self.files)


class SendFailed(Exception):
    """No file of a ``send_files`` call was sent; ``files`` is the report."""

    def __init__(self, message: str, files: list[dict]):
        super().__init__(message)
        self.files = files


@dataclass
class SyncResult:
    """Outcome of one history fetch (``HistorySync``)."""
    # changed | unchanged | skipped | unavailable | error | not_found
    status: str
    conversation_id: int
    fetched: int = 0
    added: int = 0
    updated: int = 0
    deleted: int = 0
    exhausted: bool = False
    flood_wait: int = 0
    error: Optional[str] = None
    added_ids: list = field(default_factory=list)  # internal message ids

    @property
    def changed(self) -> bool:
        return self.status == "changed"

    @property
    def completed(self) -> bool:
        return self.status in ("changed", "unchanged")

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("added_ids", None)
        return d



class Connector(ABC):
    channel: ClassVar[str]
    display_name: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    # Several accounts of this channel can be connected (Gmail); the manager
    # then creates one connector per ``accounts`` row.
    multi_account: ClassVar[bool] = False

    # Bumped by every stop request; a start that began before it spawns nothing.
    _stop_generation: int = 0

    def __init__(self, account_id: Optional[int] = None) -> None:
        # The ``accounts`` row this connector serves; None until sign-in
        # created it.
        self.account_id = account_id

    def _lifecycle(self) -> asyncio.Lock:
        """The lock that serialises this connector's start / stop / restart
        (built on the running loop: connectors may be created at import)."""
        cell: Optional[LoopLocal[asyncio.Lock]] = getattr(self, "_lifecycle_lock", None)
        if cell is None:
            cell = self._lifecycle_lock = LoopLocal(asyncio.Lock)
        return cell.get()

    @property
    def account_key(self) -> str:
        """Stable id of the account this connector serves."""
        return f"{self.channel}:{self.account_id}" if self.account_id is not None else f"{self.channel}:new"

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

    async def on_reauthorized(self) -> None:
        """A sign-in flow renewed this registered account's credentials (the
        flow may have run on a provisional connector). Default: nothing."""

    @abstractmethod
    async def logout(self) -> None:
        """Sign the account out and forget its stored session."""

    async def abandon_flow(self, flow_id: str, *, stop: bool = False) -> None:
        """A sign-in flow was left (closed by the user, or evicted by the
        manager): release what it holds (e.g. a helper process). Background
        work it started (a setup download) keeps going unless ``stop``, the
        user's "stop" on a step with ``stop_label``. The default does nothing."""
        return None

    @classmethod
    def setup_status(cls) -> Optional[SetupStatus]:
        """The channel's one-time setup, when one is running or just ended.
        Default: the channel needs none."""
        return None

    async def close_flows(self) -> None:
        """Abandon every unfinished sign-in flow (vault lock, shutdown)."""
        return None

    async def remove(self) -> None:
        """Disconnect for good: log out and forget credentials. The manager
        then deletes the account row (and with it its conversations)."""
        await self.logout()

    async def send(self, conversation: Any, draft: OutgoingMessage) -> list[int]:
        """Send a message into ``conversation``; persist it like a received
        one and return the stored message ids."""
        raise UnsupportedOperation(f"{self.display_name} can't send messages")

    async def send_files(self, conversation: Any, draft: OutgoingMessage) -> SentFiles:
        """Send ``draft.files`` (with ``draft.text`` as caption / body).
        Default: one message carrying every file (e.g. one email)."""
        message_ids = await self.send(conversation, draft)
        first = message_ids[0] if message_ids else None
        return SentFiles(
            message_ids=message_ids,
            files=[{"filename": f.filename, "status": "sent", "message_id": first} for f in draft.files],
        )

    async def fetch_attachment(self, attachment: Any, message: Any, conversation: Any) -> str:
        """Download an attachment; returns its path relative to DATA_DIR
        (the connector also records it via crud.set_attachment_local_path)."""
        raise UnsupportedOperation(f"{self.display_name} can't download attachments")

    async def react(self, conversation: Any, message: Any, emoji: Optional[str]) -> None:
        """Set (or with None remove) the user's reaction on a message."""
        raise UnsupportedOperation(f"{self.display_name} can't react to messages")

    async def mark_read(self, conversation: Any) -> None:
        """Tell the channel the conversation was read (read receipts).
        Best-effort; the default does nothing."""
        return None

    async def sync_now(self) -> None:
        """Pull anything new right away (e.g. after the user opens the app)."""
        return None

    def account_actions(self) -> list[dict[str, Any]]:
        """Extra things the user can do with this account in Settings ›
        Connections, each an auth flow started with ``params``:
        ``[{"id", "label", "params"}]``."""
        return []

    def history_note(self) -> Optional[str]:
        """What part of a conversation's history Mingly holds, when that's
        less than everything (Signal: from linking; SMS / Gmail: a backfill
        window). Shown on empty conversations and to MCP agents. None = the
        channel's history is complete, or fetched on demand (Telegram)."""
        return None

    def conversation_capabilities(self, conversation: Any) -> frozenset[Capability]:
        """What can be done in one conversation (e.g. no sending in a
        broadcast channel you don't admin). Defaults to the channel's."""
        return self.capabilities

    def describe(self) -> dict[str, Any]:
        return {
            "account_key": self.account_key,
            "account_id": self.account_id,
            "channel": self.channel,
            "display_name": self.display_name,
            "capabilities": sorted(c.value for c in self.capabilities),
            "configured": self.is_configured(),
            "status": self.status().to_dict(),
            "actions": self.account_actions() if self.is_configured() else [],
        }
