"""Channel-neutral shapes a connector hands to ``backend.ingest.service``.

A connector translates its channel's objects into these and never writes
conversations, messages or identities itself (Telegram predates this and
writes through ``backend.telegram.ids``; new channels use the service).

``None`` in an Optional field means "not known in this delivery": the stored
value is kept (an edit or receipt that doesn't carry reactions, attachments
or recipients leaves them alone). To clear something, pass the empty value
explicitly: ``attachments=[]``, ``participants=[]``, ``meta={}``, or
``NormalizedConversation.clear_peer``.
"""

import json
from collections import Counter
from dataclasses import dataclass
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
    # (identity, to|cc|bcc); None = keep the stored recipients
    participants: Optional[list[tuple[NormalizedIdentity, str]]] = None
    attachments: Optional[list[NormalizedAttachment]] = None  # None = keep the stored files
    meta: Optional[dict[str, Any]] = None  # None = keep; {} = clear
    reactions_json: Optional[str] = None  # None = keep
    delivery_state: str = "sent"


@dataclass
class NormalizedConversation:
    external_id: str
    kind: str  # direct | group | channel | email_thread
    title: Optional[str] = None
    peer: Optional[NormalizedIdentity] = None  # the other party of a one-to-one conversation (None = keep)
    clear_peer: bool = False  # no longer one-to-one: drop the stored peer
    members_count: Optional[int] = None
    unread_count: Optional[int] = None
    is_archived: Optional[bool] = None
    labels: Optional[list[str]] = None
    last_message_at: Optional[datetime] = None


def reaction_summary(by_reactor: dict) -> Optional[str]:
    """``reactions_json`` for channels that track one reaction per reactor
    (``{reactor: emoji}``, e.g. Signal, SMS tapbacks): ``[{emoji, count}]``,
    most used first; None without reactions."""
    if not by_reactor:
        return None
    counts = Counter(by_reactor.values())
    return json.dumps([{"emoji": e, "count": n} for e, n in counts.most_common()], ensure_ascii=False)
