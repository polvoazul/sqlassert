"""Source locations for semantic elements and diagnostics.

Origins hold plain values so that no SQLGlot node or connection can escape
through reporting.
"""

from __future__ import annotations

from dataclasses import dataclass


SQL = "sql"


@dataclass(frozen=True, order=True)
class Span:
    """A region of SQL text: where a Property Marker is written. Spans order by position.

    Lines and columns count from 1, columns in characters; `end_column` is
    one past the region's last character, as SARIF's `endColumn`.
    """

    line: int
    column: int
    end_line: int
    end_column: int

    @classmethod
    def of(cls, sql: str, start: int, end: int) -> Span:
        """The region of `sql[start:end]`."""
        line, column = _position(sql, start)
        end_line, end_column = _position(sql, end)
        return cls(line, column, end_line, end_column)


def _position(sql: str, offset: int) -> tuple[int, int]:
    return sql.count("\n", 0, offset) + 1, offset - sql.rfind("\n", 0, offset)


@dataclass(frozen=True)
class Origin:
    """A SQL source span or catalog fact that a semantic element arose from.

    `line` is the first source line, when known; `span` the exact region,
    when the element is a Property Marker (its `line` is then `line`).
    """

    kind: str
    detail: str
    line: int | None = None
    span: Span | None = None

    @classmethod
    def marked(cls, detail: str, span: Span) -> Origin:
        """The Origin of something a Property Marker at `span` asserts or reports."""
        return cls(SQL, detail, span.line, span)


@dataclass(frozen=True)
class AttachmentSite:
    """Where a Property Marker for one JOIN or Select Expression is written.

    `offset` indexes the SQL text: a join marker is inserted there, before the
    join's first keyword; a trailing column marker is appended there, after
    the Select Expression's last token. `label` names what the site marks
    (`JOIN users AS u`, `view v`, `CTE c`, `subquery s`, `Root Select`);
    anonymous Select Expressions share a label and are told apart by `line`.
    """

    label: str
    line: int
    offset: int
    trailing: bool
