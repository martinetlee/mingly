"""LLM-based message translation."""

import json
from typing import Optional

from backend.llm.base import ChatMessage, LLMError
from backend.llm.registry import get_registry

TRANSLATE_SYSTEM_PROMPT = """\
You are a translator. Translate the following message into {target_language}.
Preserve the original tone, formatting, and meaning.
If the message is already in {target_language}, return it unchanged.

Respond ONLY with valid JSON: {{"translation": "<translated text>"}}"""


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

    try:
        parsed = json.loads(response.content)
        return str(parsed["translation"])
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        raise LLMError(
            f"Invalid JSON from model: {response.content!r}",
            provider=response.provider,
        ) from e
