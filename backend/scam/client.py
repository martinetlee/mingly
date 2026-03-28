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

        # --- Step 1: Signature pre-check ---
        sig_result = self._check_signatures(messages)
        if sig_result is not None:
            return sig_result

        # --- Step 2: LLM analysis ---
        return await self._llm_analyze(messages, profiles, chat_metadata)

    def _check_signatures(self, messages: list[dict]) -> Optional[ScamCheckResult]:
        """Quick check against signatures. Returns result if match found, None otherwise."""
        sigs = self._pipeline.signatures
        if not sigs.domains and not sigs.wallets and not sigs._compiled_patterns and not sigs.telegram_ids:
            return None

        for msg in messages:
            text = msg.get("text") or ""
            sender_id = msg.get("sender_id")

            # Check sender against known scammer IDs
            if sender_id and sender_id in set(sigs.telegram_ids):
                return ScamCheckResult(
                    is_scam=True, confidence=0.95,
                    reason=f"Sender ID {sender_id} matches known scammer signature",
                    category="impersonation",
                )

            if not text:
                continue

            # Check domains
            if sigs.domains:
                domains = _extract_domains(text)
                for domain in domains:
                    if domain in sigs.domains:
                        return ScamCheckResult(
                            is_scam=True, confidence=0.95,
                            reason=f"Message contains known malicious domain: {domain}",
                            category="malware_link",
                        )

            # Check wallet addresses
            for wallet in sigs.wallets:
                if wallet.lower() in text.lower():
                    return ScamCheckResult(
                        is_scam=True, confidence=0.95,
                        reason=f"Message contains known scam wallet address",
                        category="investment_scam",
                    )

            # Check regex patterns
            for pattern in sigs._compiled_patterns:
                if pattern.search(text):
                    return ScamCheckResult(
                        is_scam=True, confidence=0.85,
                        reason=f"Message matches scam pattern: {pattern.pattern}",
                        category="other",
                    )

        return None

    async def _llm_analyze(
        self,
        messages: list[dict],
        profiles: list[dict],
        chat_metadata: dict,
    ) -> ScamCheckResult:
        """Run LLM analysis using the pipeline config."""
        registry = get_registry()
        provider = registry.get_any()
        if provider is None:
            logger.debug("No LLM provider available for scam detection")
            return ScamCheckResult(is_scam=False, confidence=0.0, reason="", error="No LLM provider available")

        # Build context for the actual messages
        parts = []
        if chat_metadata:
            parts.append(f"Chat: {chat_metadata.get('name', 'Unknown')} ({chat_metadata.get('type', 'unknown')})")
        for profile in profiles[:5]:
            bio = profile.get('bio') or ''
            is_contact = profile.get('is_contact', False)
            parts.append(f"Profile - ID:{profile.get('telegram_id')}, Name:{profile.get('name', '?')}, Bio:{bio}, Is contact:{is_contact}")
        parts.append("\nMessages to analyze:")
        for msg in messages[:20]:
            sender = msg.get("sender_name", "Unknown")
            text = (msg.get("text") or "")[:500]
            parts.append(f"  [{sender}]: {text}")

        user_content = "\n".join(parts)

        # Build LLM messages with few-shot examples
        system_prompt = self._pipeline.format_system_prompt()
        llm_messages: list[ChatMessage] = [
            ChatMessage(role="system", content=system_prompt),
        ]

        # Add few-shot examples as user/assistant turn pairs (limit to 2 to save context)
        for example in self._pipeline.few_shot_examples[:2]:
            ex_parts = []
            for m in example.messages:
                ex_parts.append(f"  [{m.sender}]: {m.text}")
            llm_messages.append(ChatMessage(role="user", content="Messages to analyze:\n" + "\n".join(ex_parts)))
            llm_messages.append(ChatMessage(role="assistant", content=json.dumps({
                "is_scam": example.verdict.is_scam,
                "confidence": example.verdict.confidence,
                "category": example.verdict.category,
                "reason": example.verdict.reason,
            })))

        # Add actual message context
        llm_messages.append(ChatMessage(role="user", content=user_content))

        # Build full prompt string for audit trail
        full_prompt = f"[system]\n{system_prompt}\n"
        for m in llm_messages[1:]:
            full_prompt += f"\n[{m.role}]\n{m.content}\n"

        try:
            response = await provider.complete(llm_messages, max_tokens=self._max_tokens, temperature=0.1)
            raw_content = response.content or ""
            content = raw_content.strip()
            if not content:
                return ScamCheckResult(is_scam=False, confidence=0.0, reason="", error="LLM returned empty response",
                                      llm_prompt=full_prompt, llm_response=raw_content)
            # Strip markdown code fences
            if content.startswith("```"):
                content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            # Try to extract JSON from the response (LLM may include extra text)
            json_start = content.find("{")
            json_end = content.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                content = content[json_start:json_end]
            parsed = json.loads(content)

            # Validate category
            category = parsed.get("category")
            valid_categories = self._pipeline.get_category_ids()
            if category and category not in valid_categories:
                category = "other"

            return ScamCheckResult(
                is_scam=bool(parsed.get("is_scam", False)),
                confidence=max(0.0, min(1.0, float(parsed.get("confidence", 0.0)))),
                reason=str(parsed.get("reason", "")),
                category=category,
                raw_response=parsed,
                llm_prompt=full_prompt,
                llm_response=raw_content,
            )
        except json.JSONDecodeError as e:
            raw = ""
            try:
                raw = (response.content or "")[:200]
            except Exception:
                pass
            logger.warning("Scam detection: LLM returned non-JSON response: %s (raw: %s)", e, raw or "empty")
            return ScamCheckResult(is_scam=False, confidence=0.0, reason="", error="LLM returned an invalid response — try again or use a different model",
                                  llm_prompt=full_prompt, llm_response=raw)
        except (LLMError, Exception) as e:
            raw = ""
            try:
                raw = (response.content or "")[:200]
            except Exception:
                pass
            logger.warning("Scam detection LLM call failed: %s (raw: %s)", e, raw or "empty")
            return ScamCheckResult(is_scam=False, confidence=0.0, reason="", error=f"Scam detection failed: {e}",
                                  llm_prompt=full_prompt, llm_response=raw)

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
