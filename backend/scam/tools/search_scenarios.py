"""Search the scenario library for known scam patterns.

Two-mode hybrid approach:
  1. List mode (no scenario_id): returns compact index of ALL scenarios
     so the LLM can semantically match against them.
  2. Detail mode (with scenario_id): returns full details for a specific
     scenario after the LLM has identified a candidate.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from backend.scam.tools import register_tool

logger = logging.getLogger(__name__)

from backend.config import DATA_DIR

_SCENARIOS_FILE_DATA = DATA_DIR / "scenarios.json"
_SCENARIOS_FILE_BUNDLE = Path(__file__).parent.parent / "scenarios.json"
_cached_scenarios: Optional[list[dict]] = None


def _load_scenarios() -> list[dict]:
    global _cached_scenarios
    if _cached_scenarios is not None:
        return _cached_scenarios

    # Try data dir first (app support), then bundle location
    scenarios_file = _SCENARIOS_FILE_DATA if _SCENARIOS_FILE_DATA.exists() else _SCENARIOS_FILE_BUNDLE
    if not scenarios_file.exists():
        # Copy bundle to data dir if available
        if _SCENARIOS_FILE_BUNDLE.exists():
            import shutil
            shutil.copy2(_SCENARIOS_FILE_BUNDLE, _SCENARIOS_FILE_DATA)
            scenarios_file = _SCENARIOS_FILE_DATA

    try:
        data = json.loads(scenarios_file.read_text(encoding="utf-8"))
        _cached_scenarios = data.get("scenarios", [])
        logger.info("Loaded %d scam scenarios", len(_cached_scenarios))
    except Exception as e:
        logger.warning("Failed to load scenarios: %s", e)
        _cached_scenarios = []
    return _cached_scenarios


@register_tool(
    name="search_scenarios",
    description=(
        "Look up known scam scenarios. Two modes:\n"
        "1. Call WITHOUT scenario_id to get a compact list of ALL known scam scenarios "
        "(id, name, one-line description, category). Use this first to see which scenarios "
        "might match the conversation.\n"
        "2. Call WITH scenario_id to get full details (indicators, conversation_pattern, "
        "risk level) for a specific scenario you want to match against."
    ),
    parameters={
        "type": "object",
        "properties": {
            "scenario_id": {
                "type": "string",
                "description": (
                    "Optional. The id of a specific scenario to retrieve full details for "
                    "(e.g. 'fake_reconnection'). Omit to list all scenarios."
                ),
            },
        },
        "required": [],
    },
)
async def search_scenarios(args: dict) -> dict:
    """Hybrid scenario lookup: list all (compact) or get one (full details)."""
    scenarios = _load_scenarios()
    scenario_id = args.get("scenario_id")

    # --- Detail mode: return full scenario by id ---
    if scenario_id:
        for scenario in scenarios:
            if scenario.get("id") == scenario_id:
                return {
                    "mode": "detail",
                    "scenario": {
                        "id": scenario["id"],
                        "name": scenario["name"],
                        "description": scenario["description"],
                        "indicators": scenario.get("indicators", []),
                        "conversation_pattern": scenario.get(
                            "conversation_pattern", []
                        ),
                        "risk": scenario.get("risk", "unknown"),
                        "category": scenario.get("category", "other"),
                    },
                }
        return {
            "mode": "detail",
            "error": f"No scenario found with id '{scenario_id}'",
            "available_ids": [s["id"] for s in scenarios],
        }

    # --- List mode: return compact index of all scenarios ---
    index = []
    for scenario in scenarios:
        # First sentence of description as the one-liner
        desc = scenario.get("description", "")
        one_liner = desc.split(". ")[0] + "." if ". " in desc else desc
        index.append(
            {
                "id": scenario["id"],
                "name": scenario["name"],
                "description": one_liner,
                "category": scenario.get("category", "other"),
                "risk": scenario.get("risk", "unknown"),
            }
        )

    return {
        "mode": "list",
        "total_scenarios": len(index),
        "scenarios": index,
    }
