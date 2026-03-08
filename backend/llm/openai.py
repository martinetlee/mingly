from collections.abc import AsyncIterator

from backend.llm.base import BaseLLMProvider, ChatMessage, CompletionResponse, LLMError


class OpenAIProvider(BaseLLMProvider):
    """
    OpenAI and OpenAI-compatible providers (Grok, etc.).
    Uses OpenAI-compatible chat completions API.
    """
    provider_name = "openai"

    def __init__(
        self,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o",
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.instance_name = ""  # set by registry

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> CompletionResponse:
        # TODO: implement using httpx + API key from keychain
        # from backend.secrets import get_secret
        # api_key = get_secret("openai_api_key")
        raise LLMError("OpenAI provider not yet implemented", provider=self.provider_name)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        raise LLMError("OpenAI provider not yet implemented", provider=self.provider_name)
        yield  # make this an async generator
