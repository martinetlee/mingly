"""Scam detection API client abstraction.

Provides a base class and two implementations:
- MockScamAPIClient: local LLM-based scam detection (areFlaggedScams always false)
- ExternalScamAPIClient: real HTTP calls to external service (future)
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Union

from backend.llm.base import ChatMessage, LLMError
from backend.llm.registry import get_registry

logger = logging.getLogger(__name__)

SCAM_DETECTION_PROMPT = """\
You are a scam detection assistant analyzing Telegram messages.
Determine if the messages show signs of being a scam (phishing, social engineering,
impersonation, advance-fee fraud, crypto/investment scams, romance scams, etc.).

Consider:
- Urgency or pressure tactics
- Requests for money, crypto, personal info, or credentials
- Too-good-to-be-true offers
- Impersonation of known entities
- Suspicious links or redirects
- Mismatch between claimed identity and profile data

Respond ONLY with valid JSON:
{"is_scam": <bool>, "confidence": <float 0.0-1.0>, "reason": "<one sentence explanation>"}"""


@dataclass
class ScamCheckResult:
    is_scam: bool
    confidence: float
    reason: str
    raw_response: Optional[dict] = None


@dataclass
class FollowUpRequest:
    """External API requesting additional data."""
    request_id: str
    data_type: str  # e.g. "chat_history", "user_profile"
    params: dict


class ScamAPIClient(ABC):
    @abstractmethod
    async def are_flagged_scams(self, telegram_ids: list[int]) -> dict[int, ScamCheckResult]:
        """Check if telegram users are known scammers. Returns {telegram_id: result}."""

    @abstractmethod
    async def is_this_scam(
        self,
        request_id: str,
        messages: list[dict],
        user_telegram_id: int,
        profiles: list[dict],
        chat_metadata: dict,
    ) -> ScamCheckResult:
        """Analyze messages for scam patterns. request_id is for idempotency."""

    @abstractmethod
    async def get_result(self, request_id: str) -> Union[ScamCheckResult, None]:
        """Poll for the result of an async request. Returns None if still processing."""

    @abstractmethod
    async def respond_to_followup(self, request_id: str, data: Any) -> Union[ScamCheckResult, FollowUpRequest, None]:
        """Respond to an external API follow-up request. May return another follow-up or a final result."""


class MockScamAPIClient(ScamAPIClient):
    """Local mock: areFlaggedScams always false, isThisScam uses local LLM."""

    async def are_flagged_scams(self, telegram_ids: list[int]) -> dict[int, ScamCheckResult]:
        return {}  # No one is flagged in mock mode

    async def is_this_scam(
        self,
        request_id: str,
        messages: list[dict],
        user_telegram_id: int,
        profiles: list[dict],
        chat_metadata: dict,
    ) -> ScamCheckResult:
        registry = get_registry()
        provider = registry.get_any()
        if provider is None:
            logger.debug("No LLM provider available for scam detection")
            return ScamCheckResult(is_scam=False, confidence=0.0, reason="No LLM available")

        # Build context
        parts = []
        if chat_metadata:
            parts.append(f"Chat: {chat_metadata.get('name', 'Unknown')} ({chat_metadata.get('type', 'unknown')})")
        for profile in profiles[:5]:  # limit profiles
            bio = profile.get('bio') or ''
            is_contact = profile.get('is_contact', False)
            parts.append(f"Profile - ID:{profile.get('telegram_id')}, Name:{profile.get('name', '?')}, Bio:{bio}, Is contact:{is_contact}")
        parts.append("\nMessages to analyze:")
        for msg in messages[:20]:  # limit messages
            sender = msg.get("sender_name", "Unknown")
            text = (msg.get("text") or "")[:500]
            parts.append(f"  [{sender}]: {text}")

        llm_messages = [
            ChatMessage(role="system", content=SCAM_DETECTION_PROMPT),
            ChatMessage(role="user", content="\n".join(parts)),
        ]

        try:
            response = await provider.complete(llm_messages, max_tokens=150, temperature=0.1)
            parsed = json.loads(response.content)
            return ScamCheckResult(
                is_scam=bool(parsed.get("is_scam", False)),
                confidence=max(0.0, min(1.0, float(parsed.get("confidence", 0.0)))),
                reason=str(parsed.get("reason", "")),
                raw_response=parsed,
            )
        except (json.JSONDecodeError, LLMError, Exception) as e:
            logger.warning("Scam detection LLM call failed: %s", e)
            return ScamCheckResult(is_scam=False, confidence=0.0, reason=f"Analysis failed: {e}")

    async def get_result(self, request_id: str) -> Union[ScamCheckResult, None]:
        # Mock always returns synchronously, no polling needed
        return None

    async def respond_to_followup(self, request_id: str, data: Any) -> Union[ScamCheckResult, FollowUpRequest, None]:
        # Mock doesn't use follow-ups
        return None


class ExternalScamAPIClient(ScamAPIClient):
    """Real external API client (placeholder for future implementation)."""

    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url
        self._api_key = api_key

    async def are_flagged_scams(self, telegram_ids: list[int]) -> dict[int, ScamCheckResult]:
        # TODO: implement real HTTP call
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/areFlaggedScams",
                json={"telegram_ids": telegram_ids},
                headers={"Authorization": f"Bearer {self._api_key}"},
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

    async def is_this_scam(
        self,
        request_id: str,
        messages: list[dict],
        user_telegram_id: int,
        profiles: list[dict],
        chat_metadata: dict,
    ) -> ScamCheckResult:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/isThisScam",
                json={
                    "request_id": request_id,
                    "messages": messages,
                    "user_telegram_id": user_telegram_id,
                    "profiles": profiles,
                    "chat_metadata": chat_metadata,
                },
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            return ScamCheckResult(
                is_scam=data.get("is_scam", False),
                confidence=data.get("confidence", 0.0),
                reason=data.get("reason", ""),
                raw_response=data,
            )

    async def get_result(self, request_id: str) -> Union[ScamCheckResult, None]:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self._base_url}/result/{request_id}",
                headers={"Authorization": f"Bearer {self._api_key}"},
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
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/followup/{request_id}",
                json={"data": data},
                headers={"Authorization": f"Bearer {self._api_key}"},
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
