"""What a scam check is: the request / result shapes, the client interface
every detection backend implements, and the prompt rendering of a request.

Shared by the queue, the service, the DAG engine and the tools; the concrete
clients live in ``backend.scam.client``.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional, Union
from urllib.parse import urlparse


@dataclass
class ScamCheckResult:
    is_scam: bool
    confidence: float
    reason: str
    category: Optional[str] = None
    raw_response: Optional[dict] = None
    error: Optional[str] = None
    llm_prompt: Optional[str] = None
    llm_response: Optional[str] = None
    trace_json: Optional[str] = None


@dataclass
class FollowUpRequest:
    """External API requesting additional data."""
    request_id: str
    data_type: str  # e.g. "chat_history", "user_profile"
    params: dict


@dataclass
class ScanRequest:
    """One scam check: the messages being judged (``targets``) plus read-only
    ``context``. Only targets are scored, only target senders are flagged and
    signature short-circuits look only at targets.

    Message dicts carry message_id (internal), identity_id (the sender, or
    None), sender_name, text, date, ``telegram_user_id`` for Telegram user
    senders and, in Telegram conversations, ``telegram_message_id`` and
    ``telegram_sender_id`` (the user, or the chat posting as itself).
    Profiles carry ``telegram_sender_id`` likewise. ``metadata`` describes the
    conversation: conversation_id, title, kind (direct | group | channel |
    email_thread), channel and, on Telegram, ``telegram_peer_id``. The
    Telegram ids feed what still speaks them: the external API's v1 request,
    the local prompt and known-scammer signatures."""
    conversation_id: int
    targets: list
    channel: Optional[str] = None
    context: list = field(default_factory=list)
    profiles: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def target_ids(self) -> list:
        return [m["message_id"] for m in self.targets]

    @property
    def target_identity_ids(self) -> list:
        return list(dict.fromkeys(m["identity_id"] for m in self.targets if m.get("identity_id")))

    @property
    def conversation_kind(self) -> Optional[str]:
        return self.metadata.get("kind")

    def all_messages(self) -> list:
        """Context then targets, chronological — for APIs that take one list."""
        return list(self.context) + list(self.targets)


# Character budget for the message section of the prompt. Targets are always
# included in full; context fills whatever is left, newest first.
PROMPT_CHAR_BUDGET = 6000


_CONTEXT_MSG_CHARS = 500


TARGETS_HEADER = "Messages to analyze:"


# Telegram's v1 chat types ("private" is v2's "direct").
EXTERNAL_KIND = {"direct": "private"}


def is_telegram_request(req: ScanRequest) -> bool:
    return "telegram_peer_id" in req.metadata


def telegram_chat_metadata(req: ScanRequest) -> dict:
    """The pre-multichannel ``chat_metadata`` of a Telegram request."""
    kind = req.metadata.get("kind") or "direct"
    return {
        "chat_id": req.metadata["telegram_peer_id"],
        "name": req.metadata.get("title") or "Unknown",
        "type": EXTERNAL_KIND.get(kind, kind),
    }


def build_message_context(req: ScanRequest, budget: int = PROMPT_CHAR_BUDGET) -> str:
    """Render the request for the LLM. Targets come last, under
    ``Messages to analyze:``, and are never truncated."""
    head = []
    telegram = is_telegram_request(req)
    if telegram:
        # Telegram keeps the pre-multichannel header (Telegram chat types and
        # ids), so its detection input is unchanged.
        meta = telegram_chat_metadata(req)
        head.append(f"Chat: {meta['name']} ({meta['type']})")
    elif req.metadata:
        kind = req.metadata.get("kind") or "unknown"
        channel = req.metadata.get("channel") or req.channel
        head.append(
            f"Conversation: {req.metadata.get('title') or 'Unknown'} ({kind}{f' on {channel}' if channel else ''})"
        )
    for profile in req.profiles[:5]:
        bio = profile.get("bio") or ""
        if telegram:
            head.append(
                f"Profile - ID:{profile.get('telegram_sender_id')}, Name:{profile.get('name', '?')}, "
                f"Bio:{bio}, Is contact:{profile.get('is_contact', False)}"
            )
        else:
            head.append(
                f"Profile - Name:{profile.get('name') or '?'}, "
                f"Bio:{bio}, Is contact:{profile.get('is_contact', False)}"
            )
    target_lines = [TARGETS_HEADER] + [
        f"  [{m.get('sender_name') or 'Unknown'}]: {m.get('text') or ''}" for m in req.targets
    ]

    remaining = budget - sum(len(x) + 1 for x in head) - sum(len(x) + 1 for x in target_lines)
    context_lines: list[str] = []
    for m in reversed(req.context):  # newest context first until the budget runs out
        line = f"  [{m.get('sender_name') or 'Unknown'}]: {(m.get('text') or '')[:_CONTEXT_MSG_CHARS]}"
        if remaining - (len(line) + 1) < 0:
            break
        remaining -= len(line) + 1
        context_lines.insert(0, line)

    parts = list(head)
    if context_lines:
        parts.append("\nEarlier messages from the same sender (context only, do not judge these):")
        parts.extend(context_lines)
    parts.append("")
    parts.extend(target_lines)
    return "\n".join(parts)


class ScamAPIClient(ABC):
    @abstractmethod
    async def are_flagged_scams(self, telegram_ids: list[int]) -> dict[int, ScamCheckResult]:
        """Check if Telegram users (by Telegram user id — the signature and
        cloud contract) are known scammers. Returns {telegram_user_id: result}.
        The service translates identities to user ids and back."""

    @abstractmethod
    async def is_this_scam(self, request_id: str, req: ScanRequest) -> ScamCheckResult:
        """Judge ``req.targets`` (with ``req.context`` as background). request_id is for idempotency."""

    @abstractmethod
    async def get_result(self, request_id: str) -> Union[ScamCheckResult, None]:
        """Poll for the result of an async request. Returns None if still processing."""

    @abstractmethod
    async def respond_to_followup(self, request_id: str, data: Any) -> Union[ScamCheckResult, FollowUpRequest, None]:
        """Respond to an external API follow-up request. May return another follow-up or a final result."""


_URL_RE = re.compile(r'https?://[^\s<>"]+|(?:[\w-]+\.)+(?:com|org|net|io|xyz|me|co|app|dev|link|info|biz|cc|ru|cn|tk)\b[/\w.-]*', re.IGNORECASE)


def extract_domains(text: str) -> set[str]:
    """Extract domain names from URLs in text."""
    domains = set()
    for match in _URL_RE.findall(text):
        try:
            parsed = urlparse(match if match.startswith("http") else f"http://{match}")
            if parsed.hostname:
                domains.add(parsed.hostname.lower())
        except Exception:
            pass
    return domains


# Backoff before a failed scan is retried automatically; after the last step
# the message is left 'failed' until the user rescans it.
RETRY_BACKOFF = [timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=12)]
MAX_AUTO_ATTEMPTS = len(RETRY_BACKOFF) + 1
