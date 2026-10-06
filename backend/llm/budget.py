"""Prompt budgeting against a provider's context window.

Token counts are estimated as UTF-8 bytes / 3: ~4 chars/token for English
becomes a safe overestimate, and CJK text (3 bytes/char, ~1 token/char) is
not undercounted 3x as a chars-based estimate would.
"""

from typing import Optional

# Used when a provider reports no context size (hosted models: very large).
UNBOUNDED_PROMPT_TOKENS = 60_000
SAFETY_MARGIN = 0.15
FIXED_OVERHEAD_TOKENS = 256  # chat template / role tokens


def estimate_tokens(text: Optional[str]) -> int:
    if not text:
        return 0
    return len(text.encode("utf-8")) // 3 + 1


def prompt_budget_tokens(context_tokens: Optional[int], max_output_tokens: int) -> int:
    """Tokens available for the prompt: context minus the reserved answer,
    minus a safety margin. Never below 1024."""
    if not context_tokens:
        return UNBOUNDED_PROMPT_TOKENS
    usable = int(context_tokens * (1 - SAFETY_MARGIN)) - max_output_tokens - FIXED_OVERHEAD_TOKENS
    return max(1024, min(UNBOUNDED_PROMPT_TOKENS, usable))


def clip_to_tokens(text: str, tokens: int) -> str:
    """Keep the head of ``text`` within ~``tokens`` (UTF-8 safe)."""
    limit = max(0, tokens * 3)
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="ignore").rstrip() + "\n…(truncated)"
