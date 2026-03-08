import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


@dataclass
class LLMInstanceConfig:
    name: str                                           # unique role name, e.g. "orchestrator"
    provider: str                                       # "ollama" | "anthropic" | "openai"
    model: str                                          # e.g. "qwen2.5:7b"
    base_url: str = "http://localhost:11434/v1"
    system_prompt: str = ""                             # optional per-instance system prompt


@dataclass
class LLMRegistryConfig:
    instances: list[LLMInstanceConfig] = field(default_factory=list)
    orchestrator_name: str = ""       # which named instance is the orchestrator
    orchestrator_prompt: str = ""     # user's routing instructions


def _config_path() -> Path:
    from backend.config import DATA_DIR
    return DATA_DIR / "llm_config.json"


def load_registry_config() -> LLMRegistryConfig:
    path = _config_path()
    if not path.exists():
        return LLMRegistryConfig()
    try:
        data = json.loads(path.read_text())
        instances = [LLMInstanceConfig(**i) for i in data.get("instances", [])]
        return LLMRegistryConfig(
            instances=instances,
            orchestrator_name=data.get("orchestrator_name", ""),
            orchestrator_prompt=data.get("orchestrator_prompt", ""),
        )
    except Exception:
        return LLMRegistryConfig()


def save_registry_config(config: LLMRegistryConfig) -> None:
    path = _config_path()
    data = {
        "instances": [asdict(i) for i in config.instances],
        "orchestrator_name": config.orchestrator_name,
        "orchestrator_prompt": config.orchestrator_prompt,
    }
    path.write_text(json.dumps(data, indent=2))
