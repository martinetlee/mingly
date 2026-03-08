from collections.abc import AsyncIterator

from backend.llm.base import BaseLLMProvider, ChatMessage, CompletionResponse, LLMError


class AnthropicProvider(BaseLLMProvider):
    provider_name = "anthropic"

    def __init__(self, model: str = "claude-opus-4-6"):
        self.model = model
        self.instance_name = ""  # set by registry

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> CompletionResponse:
        # TODO: implement using anthropic SDK
        # from backend.secrets import get_secret
        # api_key = get_secret("anthropic_api_key")
        raise LLMError("Anthropic provider not yet implemented", provider=self.provider_name)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.3,
    ) -> AsyncIterator[str]:
        raise LLMError("Anthropic provider not yet implemented", provider=self.provider_name)
        yield  # make this an async generator
