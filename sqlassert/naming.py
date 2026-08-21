"""Generated constant names for the Clingo program.

A name carries a readable kind prefix and a sanitized hint so that facts stay
inspectable, followed by a deterministic incremental suffix that establishes
identity. The hint is diagnostic only; two elements with the same hint still get
distinct names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


RELATION = "relation"
PLAN = "plan"
INSTANCE = "instance"
COLUMN = "column"
KEY = "key"
JOIN = "join"
EXPRESSION = "expression"
ASSERTION = "assertion"

_UNSAFE = re.compile(r"[^a-z0-9]+")
_ANONYMOUS = "anon"


@dataclass
class ConstantNames:
    """Deterministic name generator, one per analysis."""

    _counts: dict[str, int] = field(default_factory=dict)

    def new(self, kind: str, hint: str = "") -> str:
        self._counts[kind] = self._counts.get(kind, 0) + 1
        return f"{kind}_{_sanitize(hint)}_{self._counts[kind]}"


def _sanitize(hint: str) -> str:
    sanitized = _UNSAFE.sub("_", hint.lower()).strip("_")
    return sanitized or _ANONYMOUS
