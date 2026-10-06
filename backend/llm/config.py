import copy
import json
import os
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
    num_ctx: Optional[int] = None                       # Ollama context window (tokens); None = provider default


@dataclass
class LLMRegistryConfig:
    instances: list[LLMInstanceConfig] = field(default_factory=list)
    orchestrator_name: str = ""       # which named instance is the orchestrator
    orchestrator_prompt: str = ""     # user's routing instructions


def _config_path() -> Path:
    from backend.config import DATA_DIR
    return DATA_DIR / "llm_config.json"


# (path, mtime_ns, size) -> parsed config; avoids re-reading the file on every LLM call.
_cache: Optional[tuple] = None


def load_registry_config() -> LLMRegistryConfig:
    global _cache
    path = _config_path()
    try:
        st = path.stat()
    except FileNotFoundError:
        return LLMRegistryConfig()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if _cache is not None and _cache[0] == key:
        return copy.deepcopy(_cache[1])
    cfg = _parse(path)
    _cache = (key, cfg)
    return copy.deepcopy(cfg)


def _parse(path: Path) -> LLMRegistryConfig:
    try:
        data = json.loads(path.read_text())
        known = set(LLMInstanceConfig.__dataclass_fields__)
        instances = [
            LLMInstanceConfig(**{k: v for k, v in i.items() if k in known})
            for i in data.get("instances", [])
        ]
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
    # Atomic replace so a crash mid-write can't leave a truncated config.
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
    global _cache
    _cache = None
