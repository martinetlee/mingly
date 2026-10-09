"""Channel-neutral shapes a connector hands to ``backend.ingest.service``.

A connector translates its channel's objects into these and never writes
conversations, messages or identities itself (Telegram predates this and
writes through ``backend.telegram.ids``; new channels use the service).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


@dataclass
class NormalizedIdentity:
    external_id: str  # channel handle: lowercased email, Signal ACI, E.164 phone …
    display_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    phone: Optional[str] = None
    is_contact: Optional[bool] = None
    is_self: Optional[bool] = None
    is_bot: Optional[bool] = None


@dataclass
class NormalizedAttachment:
    kind: str  # photo | video | voice | audio | gif | document | sticker
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    size: Optional[int] = None
    external_ref: Optional[str] = None  # what the connector needs to fetch it later


@dataclass
class NormalizedMessage:
    external_id: str
    date: datetime  # naive UTC
    seq: Optional[int] = None  # defaults to epoch milliseconds of ``date``
    sender: Optional[NormalizedIdentity] = None
    sender_name: Optional[str] = None  # defaults to the sender's display name
    subject: Optional[str] = None
    text: Optional[str] = None
    text_html: Optional[str] = None  # must already be sanitized by the connector
    is_outgoing: bool = False
    is_edited: bool = False
    kind: str = "message"  # message | service
    reply_to_external_id: Optional[str] = None
    participants: list[tuple[NormalizedIdentity, str]] = field(default_factory=list)  # (identity, to|cc|bcc)
    attachments: list[NormalizedAttachment] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    reactions_json: Optional[str] = None
    delivery_state: str = "sent"


@dataclass
class NormalizedConversation:
    external_id: str
    kind: str  # direct | group | channel | email_thread
    title: Optional[str] = None
    peer: Optional[NormalizedIdentity] = None  # the other party of a one-to-one conversation
    members_count: Optional[int] = None
    unread_count: Optional[int] = None
    is_archived: Optional[bool] = None
    labels: Optional[list[str]] = None
    last_message_at: Optional[datetime] = None
