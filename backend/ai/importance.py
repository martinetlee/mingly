import json
from typing import Optional

from backend.llm.base import ChatMessage, LLMError
from backend.llm.registry import get_registry

IMPORTANCE_SYSTEM_PROMPT = """\
You are an assistant that rates the importance of Telegram messages.
Rate the message on a scale from 0.0 to 1.0:
- 0.0–0.3: Low (casual chat, memes, noise, greetings)
- 0.3–0.6: Medium (informational, worth reading, general updates)
- 0.6–1.0: High (action needed, time-sensitive, important decisions, direct mentions)

Respond ONLY with valid JSON: {"score": <float 0.0-1.0>, "reason": "<one sentence>"}"""


async def score_message_importance(
    text: str,
    sender_name: Optional[str] = None,
    chat_name: Optional[str] = None,
    instance_name: Optional[str] = None,
) -> tuple[float, str]:
    """
    Returns (score, reason). Raises LLMError if provider fails.
    Caller is responsible for persisting results to DB.

    instance_name: which registered LLM instance to use.
                   If None, uses the first available instance.
    """
    registry = get_registry()

    if instance_name:
        provider = registry.get(instance_name)
    else:
        provider = registry.get_any()
        if provider is None:
            raise LLMError("No LLM instances registered", provider="")

    context_parts = []
    if chat_name:
        context_parts.append(f"Chat: {chat_name}")
    if sender_name:
        context_parts.append(f"From: {sender_name}")
    context_parts.append(f"Message: {text}")

    messages = [
        ChatMessage(role="system", content=IMPORTANCE_SYSTEM_PROMPT),
        ChatMessage(role="user", content="\n".join(context_parts)),
    ]

    response = await provider.complete(messages, max_tokens=100, temperature=0.1)

    try:
        parsed = json.loads(response.content)
        score = float(parsed["score"])
        reason = str(parsed["reason"])
        return max(0.0, min(1.0, score)), reason
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        raise LLMError(
            f"Invalid JSON from model: {response.content!r}",
            provider=response.provider,
        ) from e
