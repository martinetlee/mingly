"""Scam detection API client abstraction.

Provides a base class and implementations:
- LocalPipelineClient: local LLM + signature-based detection using pipeline config
- ExternalScamAPIClient: real HTTP calls to external service
"""

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional, Union
from urllib.parse import urlparse

from backend.llm.base import ChatMessage, LLMError
from backend.llm.registry import get_registry

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

    async def is_this_scam(
        self,
        request_id: str,
        messages: list[dict],
        user_telegram_id: int,
        profiles: list[dict],
        chat_metadata: dict,
    ) -> ScamCheckResult:
        if not self._ensure_pipeline():
            return ScamCheckResult(is_scam=False, confidence=0.0, reason="", error="Pipeline not loaded")

        # Build message context string for the engine
        parts = []
        if chat_metadata:
            parts.append(f"Chat: {chat_metadata.get('name', 'Unknown')} ({chat_metadata.get('type', 'unknown')})")
        for profile in profiles[:5]:
            bio = profile.get('bio') or ''
            is_contact = profile.get('is_contact', False)
            parts.append(f"Profile - ID:{profile.get('telegram_id')}, Name:{profile.get('name', '?')}, Bio:{bio}, Is contact:{is_contact}")
        parts.append("\nMessages to analyze:")
        for msg in messages[:5]:
            sender = msg.get("sender_name", "Unknown")
            text = (msg.get("text") or "")[:500]
            parts.append(f"  [{sender}]: {text}")
        message_context = "\n".join(parts)

        # Execute pipeline through the DAG engine
        from backend.scam.engine import execute_pipeline
        engine_result = await execute_pipeline(self._pipeline, message_context, messages)
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

class ExternalScamAPIClient(ScamAPIClient):
    """Real external API client."""

    def __init__(self, base_url: str, api_key: str):
        self._base_url = base_url
        self._api_key = api_key

    async def are_flagged_scams(self, telegram_ids: list[int]) -> dict[int, ScamCheckResult]:
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
