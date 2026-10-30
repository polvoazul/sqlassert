"""Source locations for semantic elements and diagnostics.

Origins hold plain values so that no SQLGlot node or connection can escape
through reporting.
"""

from __future__ import annotations

from dataclasses import dataclass


SQL = "sql"


@dataclass(frozen=True)
class Origin:
    """A SQL source span or catalog fact that a semantic element arose from."""

    kind: str
    detail: str
    line: int | None = None
