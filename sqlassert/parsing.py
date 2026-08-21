"""Parse SQL text into a SQL Program: Create Statements plus at most one Root
Select, with Unique Join Assertion markers preserved.

Markers are rewritten so that SQLGlot attaches them to the Join node they mark,
carrying their source line with them. SQLGlot objects stop at this boundary;
only the binder reads them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import Token, TokenType, Tokenizer

from sqlassert import diagnostics as diag
from sqlassert.diagnostics import Diagnostic
from sqlassert.provenance import SQL, Origin

MARKER = re.compile(re.escape("/**unique**/"), flags=re.IGNORECASE)
_ATTACHED = re.compile(r"^\*unique@(\d+)\*$", flags=re.IGNORECASE)


@dataclass(frozen=True)
class ParsedProgram:
    create_statements: tuple[exp.Expression, ...] = ()
    root_select: exp.Query | None = None
    diagnostics: tuple[Diagnostic, ...] = ()


def parse_program(sql: str, dialect: str) -> ParsedProgram:
    marked, reported = _attach_markers(sql, dialect)
    reported = list(reported)

    try:
        statements = [statement for statement in sqlglot.parse(marked, read=dialect) if statement]
    except SqlglotError as error:
        reported.append(Diagnostic(diag.SQL_PARSE_FAILED, f"SQL could not be parsed: {error}"))
        return ParsedProgram(diagnostics=tuple(reported))

    creates: list[exp.Expression] = []
    selects: list[exp.Query] = []

    for statement in statements:
        if isinstance(statement, exp.Create):
            creates.append(statement)
        elif isinstance(statement, exp.Query):
            selects.append(statement)
        else:
            reported.append(
                Diagnostic(
                    diag.UNSUPPORTED_STATEMENT,
                    f"statement is not a create statement or a root select: {statement.sql(dialect=dialect)}",
                )
            )

    if len(selects) > 1:
        reported.append(
            Diagnostic(
                diag.MULTIPLE_ROOT_SELECTS,
                f"a SQL program allows at most one root select, found {len(selects)}",
            )
        )
        return ParsedProgram(tuple(creates), None, tuple(reported))

    return ParsedProgram(tuple(creates), selects[0] if selects else None, tuple(reported))


def assertion_line(join: exp.Join) -> int | None:
    """The source line asserting this join is unique, if it is asserted at all."""
    for comment in join.comments or []:
        match = _ATTACHED.match(comment.strip())
        if match:
            return int(match.group(1))
    return None


def join_origin(join: exp.Join, dialect: str) -> Origin:
    """A readable origin for an asserted join, with the marker comment removed."""
    unmarked = join.copy()
    unmarked.comments = [comment for comment in unmarked.comments or [] if not _ATTACHED.match(comment.strip())]
    return Origin(SQL, unmarked.sql(dialect=dialect), assertion_line(join))


def _attach_markers(sql: str, dialect: str) -> tuple[str, tuple[Diagnostic, ...]]:
    """Move each marker to just after the JOIN keyword it marks.

    SQLGlot attaches a comment written before JOIN to the preceding expression,
    but attaches one written after JOIN to the Join node itself. A marker never
    reaches across a statement boundary: rather than silently asserting an
    unrelated join, an unattached marker is reported.
    """
    tokens = Tokenizer(dialect=dialect).tokenize(sql)
    joins = [token for token in tokens if token.token_type is TokenType.JOIN]
    boundaries = [token.start for token in tokens if token.token_type is TokenType.SEMICOLON]

    edits: list[tuple[int, int, str]] = []
    unattached: list[Diagnostic] = []

    for marker in MARKER.finditer(sql):
        line = sql.count("\n", 0, marker.start()) + 1
        join = _marked_join(joins, boundaries, marker.end())
        if join is None:
            unattached.append(
                Diagnostic(
                    diag.UNATTACHED_MARKER,
                    f"the unique join marker on line {line} does not mark a join",
                    Origin(SQL, marker.group(0), line),
                )
            )
            continue
        edits.append((marker.start(), marker.end(), ""))
        edits.append((join.end + 1, join.end + 1, f" /**UNIQUE@{line}**/"))

    for start, end, replacement in sorted(edits, reverse=True):
        sql = f"{sql[:start]}{replacement}{sql[end:]}"
    return sql, tuple(unattached)


def _marked_join(joins: list[Token], boundaries: list[int], after: int) -> Token | None:
    """The first join following an offset within the same statement."""
    return next(
        (
            join
            for join in joins
            if join.start >= after and not any(after <= boundary < join.start for boundary in boundaries)
        ),
        None,
    )
