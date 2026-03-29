"""Pipeline configuration for local scam detection.

Loads detection config (system prompt, categories, few-shot examples, signatures)
from a JSON file. In the future, this file will be synced from the cloud service.
"""

import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from backend.config import DATA_DIR, BASE_DIR

logger = logging.getLogger(__name__)

_PIPELINE_FILE = DATA_DIR / "scam_pipeline.json"
_DEFAULT_PIPELINE_FILE = Path(__file__).parent / "default_pipeline.json"
_SIGNATURES_FILE = DATA_DIR / "scam_signatures.json"
_DEFAULT_SIGNATURES_FILE = Path(__file__).parent / "default_signatures.json"

# Minimal embedded fallback so detection works even if the JSON file is missing
_FALLBACK_SYSTEM_PROMPT = (
    "You are a scam detection assistant analyzing Telegram messages.\n"
    "Determine if the messages show signs of being a scam.\n\n"
    "Consider:\n"
    "- Urgency or pressure tactics\n"
    "- Requests for money, crypto, personal info, or credentials\n"
    "- Too-good-to-be-true offers\n"
    "- Impersonation of known entities\n"
    "- Suspicious links or redirects\n"
    "- Mismatch between claimed identity and profile data\n\n"
    'Respond ONLY with valid JSON:\n'
    '{"is_scam": <bool>, "confidence": <float 0.0-1.0>, "category": "<category_id or null>", "reason": "<one sentence explanation>"}'
)


@dataclass
class ScamCategory:
    id: str
    label: str
    description: str


@dataclass
class FewShotMessage:
    sender: str
    text: str


@dataclass
class FewShotVerdict:
    is_scam: bool
    confidence: float
    category: Optional[str]
    reason: str


@dataclass
class FewShotExample:
    messages: list[FewShotMessage]
    verdict: FewShotVerdict


@dataclass
class Signatures:
    domains: list[str] = field(default_factory=list)
    wallets: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    telegram_ids: list[int] = field(default_factory=list)
    # Compiled regex patterns (populated at load time)
    _compiled_patterns: list[re.Pattern] = field(default_factory=list, repr=False)


@dataclass
class PipelineNodeConfig:
    id: str
    type: str  # "llm", "script", "classifier"
    config: dict = field(default_factory=dict)
    short_circuit: Optional[dict] = None


@dataclass
class PipelineEdge:
    from_node: str
    to_node: str


@dataclass
class PipelineConfig:
    version: str
    updated_at: str
    system_prompt: str  # kept for v1 compat / fallback
    categories: list[ScamCategory]
    few_shot_examples: list[FewShotExample]  # kept for v1 compat
    signatures: Signatures
    # v2 DAG fields
    context_messages: int = 5  # number of recent messages from same sender to include as context
    nodes: list[PipelineNodeConfig] = field(default_factory=list)
    edges: list[PipelineEdge] = field(default_factory=list)
    output: str = ""

    def get_category_ids(self) -> set[str]:
        return {c.id for c in self.categories}

    def format_system_prompt(self) -> str:
        """Replace {{categories}} placeholder with actual category list."""
        cat_lines = "\n".join(
            f"- {c.id}: {c.label} — {c.description}" for c in self.categories
        )
        return self.system_prompt.replace("{{categories}}", cat_lines)

    def is_v2(self) -> bool:
        return bool(self.nodes and self.edges and self.output)


def _parse_pipeline(data: dict) -> PipelineConfig:
    """Parse raw JSON dict into PipelineConfig dataclass."""
    categories = [
        ScamCategory(id=c["id"], label=c["label"], description=c["description"])
        for c in data.get("categories", [])
    ]

    few_shot_examples = []
    for ex in data.get("few_shot_examples", []):
        msgs = [FewShotMessage(sender=m["sender"], text=m["text"]) for m in ex.get("messages", [])]
        v = ex.get("verdict", {})
        verdict = FewShotVerdict(
            is_scam=v.get("is_scam", False),
            confidence=v.get("confidence", 0.0),
            category=v.get("category"),
            reason=v.get("reason", ""),
        )
        few_shot_examples.append(FewShotExample(messages=msgs, verdict=verdict))

    sig_data = data.get("signatures", {})
    signatures = Signatures(
        domains=sig_data.get("domains", []),
        wallets=sig_data.get("wallets", []),
        patterns=sig_data.get("patterns", []),
        telegram_ids=sig_data.get("telegram_ids", []),
    )

    # Compile regex patterns, skipping invalid ones
    for pattern_str in signatures.patterns:
        try:
            signatures._compiled_patterns.append(re.compile(pattern_str, re.IGNORECASE))
        except re.error as e:
            logger.warning("Invalid scam signature pattern %r: %s", pattern_str, e)

    # Parse v2 DAG fields (nodes, edges, output)
    nodes: list[PipelineNodeConfig] = []
    edges: list[PipelineEdge] = []
    output_node = ""
    system_prompt = data.get("system_prompt", "")

    if "nodes" in data and "edges" in data:
        # v2 format
        for n in data["nodes"]:
            nodes.append(PipelineNodeConfig(
                id=n["id"],
                type=n["type"],
                config=n.get("config", {}),
                short_circuit=n.get("short_circuit"),
            ))
        for e in data["edges"]:
            edges.append(PipelineEdge(from_node=e["from"], to_node=e["to"]))
        output_node = data.get("output", "")
        # Extract system_prompt from the output node's config if not at top level
        if not system_prompt and nodes:
            for n in nodes:
                if n.type == "llm" and n.config.get("prompt"):
                    system_prompt = n.config["prompt"]
                    break
    elif system_prompt:
        # v1 format — auto-convert to single-node DAG
        node_config = {"prompt": system_prompt, "max_tokens": 200}
        if few_shot_examples:
            node_config["few_shot_examples"] = [
                {"input": "Messages to analyze:\n" + "\n".join(f"  [{m.sender}]: {m.text}" for m in ex.messages),
                 "output": json.dumps({"is_scam": ex.verdict.is_scam, "confidence": ex.verdict.confidence,
                                       "category": ex.verdict.category, "reason": ex.verdict.reason})}
                for ex in few_shot_examples
            ]
        nodes = [PipelineNodeConfig(id="scan", type="llm", config=node_config)]
        edges = [PipelineEdge(from_node="$input", to_node="scan")]
        output_node = "scan"
        logger.info("Auto-converted v1 pipeline to single-node DAG")

    config = PipelineConfig(
        version=data.get("version", "0.0.0"),
        updated_at=data.get("updated_at", ""),
        system_prompt=system_prompt,
        categories=categories,
        few_shot_examples=few_shot_examples,
        signatures=signatures,
        context_messages=data.get("context_messages", 5),
        nodes=nodes,
        edges=edges,
        output=output_node,
    )

    # Overlay signatures from separate file if it exists
    _load_signatures_file(config)

    return config


def _parse_signatures(sig_data: dict) -> Signatures:
    """Parse a signatures dict into a Signatures dataclass with compiled patterns."""
    sigs = Signatures(
        domains=sig_data.get("domains", []),
        wallets=sig_data.get("wallets", []),
        patterns=sig_data.get("patterns", []),
        telegram_ids=sig_data.get("telegram_ids", []),
    )
    for pattern_str in sigs.patterns:
        try:
            sigs._compiled_patterns.append(re.compile(pattern_str, re.IGNORECASE))
        except re.error as e:
            logger.warning("Invalid scam signature pattern %r: %s", pattern_str, e)
    return sigs


def _load_signatures_file(config: PipelineConfig) -> None:
    """Load signatures from the separate signatures file."""
    if not _SIGNATURES_FILE.exists():
        # Copy bundled default if available
        if _DEFAULT_SIGNATURES_FILE.exists():
            shutil.copy2(_DEFAULT_SIGNATURES_FILE, _SIGNATURES_FILE)
            logger.info("Copied default signatures to %s", _SIGNATURES_FILE)
        else:
            return
    try:
        data = json.loads(_SIGNATURES_FILE.read_text(encoding="utf-8"))
        config.signatures = _parse_signatures(data)
        logger.info("Loaded signatures from %s: %d domains, %d wallets, %d patterns, %d IDs",
                    _SIGNATURES_FILE, len(config.signatures.domains), len(config.signatures.wallets),
                    len(config.signatures.patterns), len(config.signatures.telegram_ids))
    except Exception as e:
        logger.warning("Failed to load signatures file: %s", e)


def save_signatures(sigs_data: dict) -> None:
    """Save signatures to the separate signatures file."""
    _SIGNATURES_FILE.write_text(
        json.dumps(sigs_data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Saved signatures to %s", _SIGNATURES_FILE)


# ---------------------------------------------------------------------------
# Singleton PipelineManager
# ---------------------------------------------------------------------------

_manager: Optional["PipelineManager"] = None


def get_pipeline_manager() -> Optional["PipelineManager"]:
    if _manager is None:
        logger.warning("get_pipeline_manager() called but _manager is None — pipeline not initialized yet")
    return _manager


class PipelineManager:
    def __init__(self) -> None:
        self._config: Optional[PipelineConfig] = None

    def load(self) -> PipelineConfig:
        """Load pipeline from disk. Copies bundled default if file is missing."""
        if not _PIPELINE_FILE.exists():
            if _DEFAULT_PIPELINE_FILE.exists() and _DEFAULT_PIPELINE_FILE.resolve() != _PIPELINE_FILE.resolve():
                shutil.copy2(_DEFAULT_PIPELINE_FILE, _PIPELINE_FILE)
                logger.info("Copied default pipeline to %s", _PIPELINE_FILE)
            else:
                logger.warning("No pipeline file found at %s, using embedded fallback", _PIPELINE_FILE)
                self._config = _parse_pipeline({
                    "version": "0.0.1-fallback",
                    "system_prompt": _FALLBACK_SYSTEM_PROMPT,
                    "categories": [
                        {"id": "phishing", "label": "Phishing", "description": "Credential theft"},
                        {"id": "investment_scam", "label": "Investment Scam", "description": "Fake investments"},
                        {"id": "fake_giveaway", "label": "Fake Giveaway", "description": "Too-good-to-be-true offers"},
                        {"id": "impersonation", "label": "Impersonation", "description": "Pretending to be someone else"},
                        {"id": "other", "label": "Other", "description": "Other scam type"},
                    ],
                })
                return self._config

        try:
            raw = json.loads(_PIPELINE_FILE.read_text(encoding="utf-8"))
            self._config = _parse_pipeline(raw)
            logger.info("Loaded scam pipeline v%s (%d categories, %d examples, %d signature patterns)",
                        self._config.version, len(self._config.categories),
                        len(self._config.few_shot_examples), len(self._config.signatures.patterns))
        except Exception as e:
            logger.error("Failed to load pipeline: %s", e)
            self._config = _parse_pipeline({})

        return self._config

    def reload(self) -> PipelineConfig:
        """Re-read pipeline from disk."""
        return self.load()

    def sync(self) -> PipelineConfig:
        """Sync pipeline from cloud. Stub: just reloads from disk."""
        # Future: fetch from cloud API, compare versions, write updated file
        return self.reload()

    def get_config(self) -> PipelineConfig:
        """Return the currently loaded pipeline config."""
        if self._config is None:
            return self.load()
        return self._config


def init_pipeline_manager() -> PipelineManager:
    """Create and store the global PipelineManager singleton."""
    global _manager
    _manager = PipelineManager()
    _manager.load()
    return _manager
