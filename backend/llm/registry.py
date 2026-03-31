import asyncio
from typing import Optional

from backend.llm.base import BaseLLMProvider
from backend.llm.config import LLMInstanceConfig


class LLMRegistry:
    """Holds multiple named provider instances simultaneously."""

    def __init__(self):
        self._instances: dict[str, BaseLLMProvider] = {}
        self._configs: dict[str, LLMInstanceConfig] = {}

    def add(self, cfg: LLMInstanceConfig) -> None:
        """Build and register a named provider instance. Replaces existing."""
        provider = _build_provider(cfg)
        provider.instance_name = cfg.name
        self._instances[cfg.name] = provider
        self._configs[cfg.name] = cfg

    def remove(self, name: str) -> None:
        self._instances.pop(name, None)
        self._configs.pop(name, None)

    def get(self, name: str) -> BaseLLMProvider:
        """Raises KeyError if name not registered."""
        if name not in self._instances:
            raise KeyError(f"LLM instance {name!r} not registered")
        return self._instances[name]

    def get_by_model(self, model_name: str) -> Optional[BaseLLMProvider]:
        """Find an instance by its model name (e.g., 'qwen2.5:7b'). Returns None if not found."""
        for cfg_name, cfg in self._configs.items():
            if cfg.model == model_name:
                return self._instances.get(cfg_name)
        return None

    def get_any(self) -> Optional[BaseLLMProvider]:
        """Return any registered instance, or None."""
        for instance in self._instances.values():
            return instance
        return None

    def list_instances(self) -> list[dict]:
        return [
            {
                "name": cfg.name,
                "provider": cfg.provider,
                "model": cfg.model,
                "base_url": cfg.base_url,
                "system_prompt": cfg.system_prompt,
            }
            for cfg in self._configs.values()
        ]

    async def health_check_all(self) -> dict[str, bool]:
        """Parallel health checks for all instances."""
        results = await asyncio.gather(
            *[inst.health_check() for inst in self._instances.values()],
            return_exceptions=True,
        )
        return {
            name: (result is True)
            for name, result in zip(self._instances.keys(), results)
        }

    def __len__(self) -> int:
        return len(self._instances)


def _build_provider(cfg: LLMInstanceConfig) -> BaseLLMProvider:
    if cfg.provider == "ollama":
        from backend.llm.ollama import OllamaProvider
        return OllamaProvider(base_url=cfg.base_url, model=cfg.model)
    elif cfg.provider == "anthropic":
        from backend.llm.anthropic import AnthropicProvider
        return AnthropicProvider(model=cfg.model)
    elif cfg.provider == "openai":
        from backend.llm.openai import OpenAIProvider
        return OpenAIProvider(base_url=cfg.base_url, model=cfg.model)
    else:
        raise ValueError(f"Unknown provider: {cfg.provider!r}")


# Module-level singleton
_registry = LLMRegistry()


def get_registry() -> LLMRegistry:
    return _registry


def get_instance(name: str) -> BaseLLMProvider:
    return _registry.get(name)
