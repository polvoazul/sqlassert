"""Explicit statements about what analysis could not do.

A diagnostic reports a program the engine does not support. It is never a
Disproof and never an inference: it says that analysis stopped short, and why.

A warning is a Diagnostic too, reported apart (`Report.warnings`): the program
is supported, but a fact a connection would supply is missing, so analysis
treats it as unknown (ADR 0005). So are a constraint the dialect does not
enforce and an operator whose meaning a setting decides (`dialects.py`).
Warnings never make a Report fail.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlassert.provenance import Origin

CATALOG_METADATA_UNAVAILABLE = "catalog-metadata-unavailable"
AMBIGUOUS_COLUMN = "ambiguous-column"
AMBIGUOUS_OPERATOR = "ambiguous-operator"
CARDINALITY_ON_FILTERING_JOIN = "cardinality-on-filtering-join"
CATALOG_RELATION_NOT_FOUND = "catalog-relation-not-found"
CONSTRAINT_WITHOUT_TABLE = "constraint-without-table"
DUPLICATE_DECLARATION = "duplicate-declaration"
INCOMPLETE_CATALOG_METADATA = "incomplete-catalog-metadata"
INVALID_FOREIGN_KEY_CONSTRAINT = "invalid-foreign-key-constraint"
MULTIPLE_ROOT_SELECTS = "multiple-root-selects"
RECURSIVE_VIEW_DEFINITION = "recursive-view-definition"
SQL_PARSE_FAILED = "sql-parse-failed"
UNANALYZED_ASSERTION = "unanalyzed-assertion"
UNATTACHED_MARKER = "unattached-marker"
UNDECLARED_TABLE = "undeclared-table"
UNENFORCED_CONSTRAINT = "unenforced-constraint"
UNKNOWN_ASSERTED_COLUMN = "unknown-asserted-column"
UNRESOLVED_ASSERTED_COLUMN = "unresolved-asserted-column"
UNKNOWN_REFERENCED_COLUMN = "unknown-referenced-column"
UNRECOGNIZED_MARKER = "unrecognized-marker"
UNSUPPORTED_CREATE_STATEMENT = "unsupported-create-statement"
UNSUPPORTED_NATURAL_JOIN = "unsupported-natural-join"
UNSUPPORTED_STATEMENT = "unsupported-statement"


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    origin: Origin | None = None
