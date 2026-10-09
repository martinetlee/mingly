"""Scam detection API client abstraction.

Provides a base class and implementations:
- LocalPipelineClient: local LLM + signature-based detection using pipeline config
- ExternalScamAPIClient: real HTTP calls to external service
"""

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Union
from urllib.parse import urlparse


logger = logging.getLogger(__name__)


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
    None), sender_name, text, date, and ``telegram_user_id`` for Telegram
    senders (signature matching and the Telegram-shaped external API).
    ``metadata`` describes the conversation: conversation_id, title, kind
    (direct | group | channel | email_thread) and channel."""
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


def build_message_context(req: ScanRequest, budget: int = PROMPT_CHAR_BUDGET) -> str:
    """Render the request for the LLM. Targets come last, under
    ``Messages to analyze:``, and are never truncated."""
    head = []
    if req.metadata:
        kind = req.metadata.get("kind") or "unknown"
        channel = req.metadata.get("channel") or req.channel
        head.append(
            f"Conversation: {req.metadata.get('title') or 'Unknown'} ({kind}{f' on {channel}' if channel else ''})"
        )
    for profile in req.profiles[:5]:
        bio = profile.get("bio") or ""
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


# ---------------------------------------------------------------------------
# URL extraction helper
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r'https?://[^\s<>"]+|(?:[\w-]+\.)+(?:com|org|net|io|xyz|me|co|app|dev|link|info|biz|cc|ru|cn|tk)\b[/\w.-]*', re.IGNORECASE)


def _extract_domains(text: str) -> set[str]:
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


# ---------------------------------------------------------------------------
# LocalPipelineClient — signature check + local LLM
# ---------------------------------------------------------------------------

class LocalPipelineClient(ScamAPIClient):
    """Local detection using pipeline config: signature pre-check + LLM analysis."""

    def __init__(self, pipeline_config, max_tokens: int = 200):
        from backend.scam.pipeline import PipelineConfig
        self._pipeline: PipelineConfig = pipeline_config
        self._max_tokens = max_tokens

    def update_pipeline(self, pipeline_config) -> None:
        """Hot-swap the pipeline config."""
        self._pipeline = pipeline_config

    async def are_flagged_scams(self, telegram_ids: list[int]) -> dict[int, ScamCheckResult]:
        """Check telegram IDs against signature database."""
        if not self._pipeline.signatures.telegram_ids:
            return {}
        flagged_set = set(self._pipeline.signatures.telegram_ids)
        results = {}
        for tid in telegram_ids:
            if tid in flagged_set:
                results[tid] = ScamCheckResult(
                    is_scam=True, confidence=1.0,
                    reason="Known scammer from signature database",
                    category="impersonation",
                )
        return results

    def _ensure_pipeline(self) -> bool:
        """Ensure pipeline is loaded. Returns True if ready."""
        if self._pipeline is not None and self._pipeline.system_prompt:
            return True
        from backend.scam.pipeline import get_pipeline_manager, init_pipeline_manager, _PIPELINE_FILE
        logger.warning("Pipeline not loaded on client, attempting recovery. File exists: %s", _PIPELINE_FILE.exists())
        mgr = get_pipeline_manager()
        if not mgr:
            logger.warning("Pipeline manager not initialized, initializing now")
            mgr = init_pipeline_manager()
        config = mgr.reload()  # Always force reload from disk
        logger.warning("After reload: system_prompt length=%d, version=%s", len(config.system_prompt), config.version)
        self._pipeline = config
        return bool(self._pipeline.system_prompt)

    async def is_this_scam(self, request_id: str, req: ScanRequest) -> ScamCheckResult:
        if not self._ensure_pipeline():
            return ScamCheckResult(is_scam=False, confidence=0.0, reason="", error="Pipeline not loaded")

        from backend.scam.engine import execute_pipeline
        engine_result = await execute_pipeline(
            self._pipeline, build_message_context(req), req.targets,
            channel=req.channel, conversation_kind=req.conversation_kind,
        )
        return engine_result.scam_result

    async def get_result(self, request_id: str) -> Union[ScamCheckResult, None]:
        return None  # Local client executes synchronously

    async def respond_to_followup(self, request_id: str, data: Any) -> Union[ScamCheckResult, FollowUpRequest, None]:
        return None  # Local client doesn't use follow-ups


# Backward compatibility alias
MockScamAPIClient = LocalPipelineClient


# ---------------------------------------------------------------------------
# ExternalScamAPIClient — HTTP calls to external service
# ---------------------------------------------------------------------------

def _vault_scam_api_key() -> str:
    from backend.api.settings import get_secret_pref
    return get_secret_pref("scam_detection_api_key")


# The external API's v1 contract is Telegram-shaped: message ids only need to
# be consistent within one request (internal ids are), senders are Telegram
# user ids (None for other channels) and "private" means direct. A server that
# advertises contract 2 (GET /capabilities) gets the channel-neutral request
# of docs/multichannel/SHIELD_CLOUD_CONTRACT_V2.md instead.
_EXTERNAL_KIND = {"direct": "private"}


def _external_message(m: dict) -> dict:
    return {
        "message_id": m.get("message_id"),
        "sender_id": m.get("telegram_user_id"),
        "sender_name": m.get("sender_name"),
        "text": m.get("text"),
        "date": m.get("date"),
    }


def _external_profile(p: dict) -> dict:
    return {
        "telegram_id": p.get("telegram_user_id"),
        "name": p.get("name"),
        "bio": p.get("bio") or "",
        "is_contact": bool(p.get("is_contact")),
    }


def _external_metadata(req: ScanRequest) -> dict:
    kind = req.metadata.get("kind")
    return {
        "name": req.metadata.get("title") or "Unknown",
        "type": _EXTERNAL_KIND.get(kind, kind),
        "channel": req.channel,
    }


def _external_v1_request(request_id: str, req: ScanRequest) -> dict:
    return {
        "request_id": request_id,
        "messages": [_external_message(m) for m in req.all_messages()],
        "target_message_ids": req.target_ids,
        "user_telegram_id": 0,
        "profiles": [_external_profile(p) for p in req.profiles],
        "chat_metadata": _external_metadata(req),
    }


async def _external_v2_request(request_id: str, req: ScanRequest) -> dict:
    """Channel-neutral isThisScam request: senders as v2 handles (Telegram
    user id, else the install-salted hash), v2 conversation kinds."""
    from backend.db.database import AsyncSessionLocal
    from backend.scam.channel_ids import cloud_refs, handle_salt

    messages = req.all_messages()
    identity_ids = [m.get("identity_id") for m in messages] + [p.get("identity_id") for p in req.profiles]
    salt = handle_salt()
    async with AsyncSessionLocal() as db:
        refs = await cloud_refs(db, salt, identity_ids=identity_ids)
    return {
        "contract": 2,
        "request_id": request_id,
        "channel": req.channel or req.metadata.get("channel"),
        "conversation": {"kind": req.metadata.get("kind"), "name": req.metadata.get("title") or "Unknown"},
        "messages": [{
            "message_id": m.get("message_id"),
            "sender": refs.handles.get(m.get("identity_id")),
            "sender_name": m.get("sender_name"),
            "text": m.get("text"),
            "date": m.get("date"),
        } for m in messages],
        "target_message_ids": req.target_ids,
        "profiles": [{
            "sender": refs.handles.get(p.get("identity_id")),
            "name": p.get("name"),
            "bio": p.get("bio") or "",
            "is_contact": bool(p.get("is_contact")),
        } for p in req.profiles],
    }


class ExternalScamAPIClient(ScamAPIClient):
    """Real external API client."""

    def __init__(self, base_url: str, api_key: Optional[str] = None,
                 key_provider: Optional[Callable[[], str]] = None):
        """``api_key`` is accepted for old callers and ignored: the key lives
        in the vault and is read on every request, so a lock or vault reset
        revokes it immediately."""
        from backend.scam.cloud_sync import ContractDiscovery

        self._base_url = base_url
        self._key_provider = key_provider or _vault_scam_api_key
        self._contracts = ContractDiscovery("/capabilities")
        self._transport = None  # tests inject an httpx transport

    def _client(self):
        import httpx
        return httpx.AsyncClient(transport=self._transport)

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    async def contract_version(self) -> int:
        """2 when the server advertises it (cached with a TTL), else 1."""
        return await self._contracts.contract(self._base_url, self._headers, self._transport)

    @property
    def _api_key(self) -> str:
        try:
            return self._key_provider() or ""
        except Exception:
            logger.exception("Could not read the scam detection API key")
            return ""

    async def are_flagged_scams(self, telegram_ids: list[int]) -> dict[int, ScamCheckResult]:
        async with self._client() as client:
            resp = await client.post(
                f"{self._base_url}/areFlaggedScams",
                json={"telegram_ids": telegram_ids},
                headers=self._headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            results = {}
            for item in data.get("flagged", []):
                tid = item["telegram_id"]
                results[tid] = ScamCheckResult(
                    is_scam=True,
                    confidence=item.get("confidence", 1.0),
                    reason=item.get("reason", "Flagged by external service"),
                    raw_response=item,
                )
            return results

    async def is_this_scam(self, request_id: str, req: ScanRequest) -> ScamCheckResult:
        headers = self._headers
        if await self.contract_version() >= 2:
            from backend.scam.cloud_sync import CONTRACT_HEADER

            payload = await _external_v2_request(request_id, req)
            headers[CONTRACT_HEADER] = "2"
        else:
            payload = _external_v1_request(request_id, req)
        async with self._client() as client:
            resp = await client.post(
                f"{self._base_url}/isThisScam", json=payload, headers=headers, timeout=60,
            )
            if not resp.is_success:
                detail = resp.text[:500]
                raise Exception(f"HTTP {resp.status_code} from {self._base_url}/isThisScam: {detail}")
            data = resp.json()

            if data.get("status") == "processing":
                import asyncio
                for _ in range(30):
                    await asyncio.sleep(2)
                    poll_result = await self.get_result(request_id)
                    if poll_result is not None:
                        return poll_result
                return ScamCheckResult(
                    is_scam=False, confidence=0.0, reason="",
                    error="Scam check timed out waiting for result",
                )

            return ScamCheckResult(
                is_scam=data.get("is_scam", False),
                confidence=data.get("confidence", 0.0),
                reason=data.get("reason", ""),
                raw_response=data,
            )

    async def get_result(self, request_id: str) -> Union[ScamCheckResult, None]:
        async with self._client() as client:
            resp = await client.get(
                f"{self._base_url}/result/{request_id}",
                headers=self._headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "processing":
                return None
            return ScamCheckResult(
                is_scam=data.get("is_scam", False),
                confidence=data.get("confidence", 0.0),
                reason=data.get("reason", ""),
                raw_response=data,
            )

    async def respond_to_followup(self, request_id: str, data: Any) -> Union[ScamCheckResult, FollowUpRequest, None]:
        async with self._client() as client:
            resp = await client.post(
                f"{self._base_url}/followup/{request_id}",
                json={"data": data},
                headers=self._headers,
                timeout=60,
            )
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") == "need_more_data":
                return FollowUpRequest(
                    request_id=request_id,
                    data_type=result["data_type"],
                    params=result.get("params", {}),
                )
            return ScamCheckResult(
                is_scam=result.get("is_scam", False),
                confidence=result.get("confidence", 0.0),
                reason=result.get("reason", ""),
                raw_response=result,
            )
