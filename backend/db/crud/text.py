"""SQL text-matching helpers shared by every area."""

from sqlalchemy import func

__all__ = ["name_key", "contains_ci"]


def name_key(value):
    """Case/whitespace-insensitive name key. Mirrors the uq_*_name_ci indexes
    (SQLite lower() is ASCII-only, so always compare in SQL, never in Python)."""
    return func.lower(func.trim(value))


def contains_ci(column, text: str):
    """Case-insensitive substring match with LIKE wildcards in `text` escaped."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return column.ilike(f"%{escaped}%", escape="\\")
