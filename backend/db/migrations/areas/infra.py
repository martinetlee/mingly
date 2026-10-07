"""Infrastructure migrations. Versions 110-119 are reserved for this area.

Same rules as runner.MIGRATIONS: append-only (version, description, [sql]).
"""

MIGRATIONS: list[tuple[int, str, list[str]]] = []
