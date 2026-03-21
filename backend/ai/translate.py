"""LLM-based message translation."""

import json
import re
import logging
from typing import Optional

from backend.llm.base import ChatMessage, LLMError
from backend.llm.registry import get_registry

logger = logging.getLogger(__name__)

TRANSLATE_SYSTEM_PROMPT = """\
Translate the following message into {target_language}.
Preserve the original tone and meaning.
Reply with ONLY the translated text, nothing else. No quotes, no explanation."""


async def translate_message(
    text: str,
    target_language: str,
    instance_name: Optional[str] = None,
) -> str:
    """
    Translate text into target_language using the local LLM.
    Returns the translated text. Raises LLMError on failure.
    """
    registry = get_registry()

    if instance_name:
        provider = registry.get(instance_name)
    else:
        provider = registry.get_any()
        if provider is None:
            raise LLMError("No LLM instances registered", provider="")

    system_prompt = TRANSLATE_SYSTEM_PROMPT.format(target_language=target_language)

    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=text),
    ]

    response = await provider.complete(messages, max_tokens=2000, temperature=0.2)
    result = response.content.strip()

    # Try JSON parsing first (in case model wraps in JSON)
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and "translation" in parsed:
            return str(parsed["translation"])
    except (json.JSONDecodeError, ValueError):
        pass

    # Strip surrounding quotes if present
    if (result.startswith('"') and result.endswith('"')) or \
       (result.startswith("'") and result.endswith("'")):
        result = result[1:-1]

    if not result:
        raise LLMError("Empty translation response", provider=response.provider)

    return result
