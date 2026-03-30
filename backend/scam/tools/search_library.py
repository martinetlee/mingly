"""Generic knowledge base library search script.

Searches JSON library files with two modes:
  1. List mode: returns compact index of all entries
  2. Detail mode: returns full details for a specific entry by ID

This is a SCRIPT (generic, reusable). TOOLS are created from it in the
pipeline JSON by specifying which file to search.
"""

import json
import logging
import shutil
from pathlib import Path
from typing import Optional

from backend.config import DATA_DIR
from backend.scam.tools import register_script

logger = logging.getLogger(__name__)

_BUNDLE_DIR = Path(__file__).parent.parent  # backend/scam/

# Cache per library file
_cache: dict[str, list[dict]] = {}


def _load_library(filename: str) -> list[dict]:
    """Load a JSON library file. Tries DATA_DIR first, then bundle dir."""
    if filename in _cache:
        return _cache[filename]

    data_file = DATA_DIR / filename
    bundle_file = _BUNDLE_DIR / filename

    if not data_file.exists() and bundle_file.exists():
        shutil.copy2(bundle_file, data_file)
        logger.info("Copied default %s to %s", filename, data_file)

    target = data_file if data_file.exists() else bundle_file

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        entries = data.get("scenarios") or data.get("patterns") or data.get("entries") or []
        _cache[filename] = entries
        logger.info("Loaded %d entries from %s", len(entries), filename)
    except Exception as e:
        logger.warning("Failed to load %s: %s", filename, e)
        _cache[filename] = []

    return _cache[filename]


def invalidate_cache(filename: Optional[str] = None):
    """Clear cached entries. Called after cloud sync updates a file."""
    if filename:
        _cache.pop(filename, None)
    else:
        _cache.clear()


@register_script("search_library")
async def search_library(args: dict) -> dict:
    """Generic two-mode search: list all (compact) or get one by ID (full details).

    Expected args:
      - filename: which JSON file to search (injected by tool's script_params)
      - scenario_id / pattern_id / id: entry ID for detail mode (from LLM call)
    """
    filename = args.get("filename", "scenarios.json")
    entries = _load_library(filename)

    # Check for entry ID (support multiple param names)
    entry_id = args.get("scenario_id") or args.get("pattern_id") or args.get("id")

    # --- Detail mode ---
    if entry_id:
        for entry in entries:
            if entry.get("id") == entry_id:
                return {
                    "mode": "detail",
                    "entry": {
                        "id": entry["id"],
                        "name": entry.get("name", ""),
                        "description": entry.get("description", ""),
                        "indicators": entry.get("indicators", []),
                        "conversation_pattern": entry.get("conversation_pattern", []),
                        "risk": entry.get("risk", "unknown"),
                        "category": entry.get("category", "other"),
                    },
                }
        return {
            "mode": "detail",
            "error": f"No entry found with id '{entry_id}'",
            "available_ids": [e["id"] for e in entries],
        }

    # --- List mode ---
    index = []
    for entry in entries:
        desc = entry.get("description", "")
        one_liner = desc.split(". ")[0] + "." if ". " in desc else desc
        index.append({
            "id": entry.get("id", ""),
            "name": entry.get("name", ""),
            "description": one_liner,
            "category": entry.get("category", "other"),
            "risk": entry.get("risk", "unknown"),
        })

    return {
        "mode": "list",
        "total": len(index),
        "entries": index,
    }
