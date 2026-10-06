"""Single entry point for features that need an LLM.

Every feature resolves its provider here instead of reading llm_config.json
or POSTing to an OpenAI-compatible URL itself, so provider choice, auth and
provider-specific options (e.g. Ollama ``think: false`` / ``num_ctx``) live in
one place.

Role resolution: an instance registered under the role's name (e.g.
"briefing", "catchup", "translate") wins; otherwise the configured
orchestrator; otherwise any registered instance.
"""

from typing import Optional, Tuple

from backend.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    LLMError,
    LLMModelMissing,
    LLMTimeout,
    LLMUnavailable,
)
from backend.llm.config import load_registry_config
from backend.llm.registry import get_registry

__all__ = [
    "LLMError", "LLMUnavailable", "LLMModelMissing", "LLMTimeout",
    "get_provider", "get_provider_for_model", "complete_text", "error_kind", "error_message",
]


def get_provider(role: str = "orchestrator") -> BaseLLMProvider:
    registry = get_registry()
    cfg = load_registry_config()
    for name in (role if role != "orchestrator" else "", cfg.orchestrator_name):
        if name:
            try:
                return registry.get(name)
            except KeyError:
                pass
    provider = registry.get_any()
    if provider is None:
        raise LLMUnavailable("No LLM is configured. Set up an AI model in Settings.")
    return provider


def get_provider_for_model(model: Optional[str], role: str) -> Tuple[BaseLLMProvider, Optional[str]]:
    """Provider for a config that may pin a model by name (e.g. a scam
    pipeline node). Returns (provider, warning): an instance serving
    ``model`` if one is registered, else the ``role`` provider with a warning
    saying the pinned model was unavailable. Raises LLMUnavailable."""
    if model:
        pinned = get_registry().get_by_model(model)
        if pinned is not None:
            return pinned, None
        provider = get_provider(role)
        return provider, f"Model '{model}' not available, using '{provider.instance_name}'"
    return get_provider(role), None


def error_kind(exc: BaseException) -> str:
    """Typed failure kind used in SSE events and job status:
    llm_unavailable | model_missing | timeout | llm_error."""
    if isinstance(exc, LLMUnavailable):
        return "llm_unavailable"
    if isinstance(exc, LLMModelMissing):
        return "model_missing"
    if isinstance(exc, LLMTimeout):
        return "timeout"
    return "llm_error"


def error_message(exc: BaseException) -> str:
    kind = error_kind(exc)
    if kind == "llm_unavailable":
        return f"AI model unavailable: {exc}"
    if kind == "model_missing":
        return f"AI model not installed: {exc}"
    if kind == "timeout":
        return "The AI model timed out."
    return f"AI model error: {exc}"


def llm_configured(role: str = "orchestrator") -> bool:
    try:
        get_provider(role)
        return True
    except LLMUnavailable:
        return False


async def complete_text(
    messages: list[ChatMessage],
    *,
    role: str = "orchestrator",
    max_tokens: int = 1024,
    temperature: float = 0.3,
    provider: Optional[BaseLLMProvider] = None,
) -> str:
    """Plain-text completion. Raises LLMUnavailable / LLMModelMissing /
    LLMTimeout / LLMError — never returns a placeholder."""
    provider = provider or get_provider(role)
    resp = await provider.complete(messages, max_tokens=max_tokens, temperature=temperature)
    return (resp.content or "").strip()
