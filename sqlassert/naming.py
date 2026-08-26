"""Generated constant names for the Clingo program.

A name carries a readable kind prefix and a sanitized hint so that facts stay
inspectable, followed by a deterministic incremental suffix that establishes
identity. The hint is for readability only; two elements with the same hint still
get distinct names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


RELATION = "rel"
COLUMN = "col"
KEY = "key"
JOIN = "join"
EXPRESSION = "expr"
ASSERTION = "assert"

_UNSAFE = re.compile(r"[^a-z0-9]+")


@dataclass
class NameGiver:
    """Deterministic name generator, one per Clingo encoding.

    Uniqueness comes from the shared counter used for encoded nodes and Unique
    Sets together.
    """

    _next_id: int = 0

    def new(self, kind: str, hint: str = "") -> str:
        self._next_id += 1
        sanitized = _sanitize(hint)
        if sanitized:
            return f"{kind}_{sanitized}_{self._next_id}"
        return f"{kind}_{self._next_id}"


def _sanitize(hint: str) -> str:
    sanitized = _UNSAFE.sub("_", hint.lower()).strip("_")
    return sanitized
