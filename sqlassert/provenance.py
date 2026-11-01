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
