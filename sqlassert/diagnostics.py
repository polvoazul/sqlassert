"""Explicit statements about what analysis could not do.

A diagnostic reports a program the engine does not support. It is never a
Disproof and never an inference: it says that analysis stopped short, and why.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlassert.provenance import Origin

DUPLICATE_DECLARATION = "duplicate-declaration"
MULTIPLE_ROOT_SELECTS = "multiple-root-selects"
RECURSIVE_VIEW_DEFINITION = "recursive-view-definition"
SQL_PARSE_FAILED = "sql-parse-failed"
UNANALYZED_ASSERTION = "unanalyzed-assertion"
UNATTACHED_MARKER = "unattached-marker"
UNRECOGNIZED_MARKER = "unrecognized-marker"
UNSUPPORTED_CREATE_STATEMENT = "unsupported-create-statement"
UNSUPPORTED_STATEMENT = "unsupported-statement"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    origin: Origin | None = None
