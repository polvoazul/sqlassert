"""Origins: where a semantic element or piece of knowledge came from.

Origins are the only provenance the report is allowed to expose. They hold
plain values so that no SQLGlot node, connection, or Clingo symbol can escape
through reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field


SQL = "sql"


@dataclass(frozen=True)
class Origin:
    """A SQL source span or catalog fact that a semantic element arose from."""

    kind: str
    detail: str
    line: int | None = None


@dataclass
class OriginRegistry:
    """Assigns stable identifiers to origins so the IR can reference them."""

    _origins: dict[str, Origin] = field(default_factory=dict)

    def register(self, origin: Origin) -> str:
        origin_id = f"origin_{len(self._origins) + 1}"
        self._origins[origin_id] = origin
        return origin_id

    def resolve(self, origin_id: str) -> Origin:
        return self._origins[origin_id]
