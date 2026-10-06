"""Typed access to DML result metadata.

``AsyncSession.execute`` is typed as returning ``Result``, but for
INSERT/UPDATE/DELETE it is a ``CursorResult`` carrying ``rowcount``.
"""

from typing import Any, cast

from sqlalchemy.engine import CursorResult, Result


def affected_rows(result: "Result[Any]") -> int:
    return cast("CursorResult[Any]", result).rowcount or 0
