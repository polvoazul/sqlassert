"""Low-level suggestions: code smells, from the Kossmann et al. 2022 triage (issue 15).

A Smell is not a rewrite: the SQL may be what its author meant. It names a
place where the result depends on something the SQL does not pin down, and
the proof that would have pinned it, which failed:

- `LIMIT` or `OFFSET` whose `ORDER BY` does not strictly determine every
  selected column (or that has no `ORDER BY`): rows tied on the sort keys come
  in an arbitrary order, so which rows are kept may change from run to run
  (Kossmann's "unstable sorting", read the other way round).

A smell is reported only when sqlassert sees the whole Select Expression; an
`ORDER BY` key it cannot map to a selected column keeps it silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from sqlglot import exp

from sqlassert import ir
from sqlassert.improvements import Level, _syntax_at_sites
from sqlassert.provenance import AttachmentSite
from sqlassert.python_engine import Arena, Result
from sqlassert.rules.uniqueness import _closure
from sqlassert.sql_parse import ParsedProgram


class Concern(Enum):
    ARBITRARY_LIMIT = "Arbitrary LIMIT"


@dataclass(frozen=True)
class Smell:
    """A place whose result the SQL leaves unspecified, with the failed proof."""

    site: AttachmentSite
    concern: Concern
    message: str
    evidence: Result
    level: ClassVar[Level] = Level.LOW


def smells(ast: ParsedProgram, program: ir.Program, arena: Arena) -> list[Smell]:
    """Every Smell in `program`, in source order. `arena` must be active."""
    syntax = _syntax_at_sites(ast)
    reachable = set(program.nodes())
    found: list[Smell] = []
    for site, relation in program.attachment_sites:
        node = syntax.get((site.offset, site.trailing))
        if isinstance(relation, ir.Limit) and relation in reachable and isinstance(node, exp.Select):
            found.extend(_arbitrary_limit(site, relation, node))
    return sorted(found, key=lambda item: item.site.offset)


def _arbitrary_limit(site: AttachmentSite, limit: ir.Limit, select: exp.Select) -> list[Smell]:
    """A LIMIT whose sort keys leave two different rows tied.

    Rows that agree on every selected column are interchangeable, so the keys
    need only strictly determine the selected columns (NULL equal to NULL, as
    ORDER BY ties them), not a row of the input. Under a projection the keys
    may be input columns it does not select, so the question is asked there.
    """
    if limit.count == 0:
        return []
    order = select.args.get("order")
    items = order.expressions if order is not None else []
    relation = limit.input
    if isinstance(relation, ir.Project):
        scope, needed = relation.input, {column: _needed(column) for column in relation.output_columns}
    else:
        scope, needed = relation, {column: frozenset({column}) for column in relation.output_columns}
    keys = _sort_keys(items, relation.output_columns, scope)
    if keys is None:
        return []
    closure = _closure(scope, frozenset(keys), frozenset())
    loose = [column for column, inputs in needed.items() if inputs is None or not inputs <= closure.value.columns]
    if not loose:
        return []
    names = ", ".join(column.name for column in loose)
    reason = f"the ORDER BY does not determine ({names}), so rows tied on it" if keys else "with no ORDER BY, rows"
    message = f"{reason} come in an arbitrary order and LIMIT may keep different rows on each run: order by a key as well"
    return [Smell(site, Concern.ARBITRARY_LIMIT, message, closure)]


def _needed(column: ir.OutputColumn) -> frozenset[ir.OutputColumn] | None:
    """The input columns a projected column's value is a function of, if known."""
    inputs = ir.determinants(column.scalar_expr)
    return None if inputs is None else frozenset(inputs)


def _sort_keys(
    items: list[exp.Expression], outputs: tuple[ir.OutputColumn, ...], scope: ir.RelationExpr
) -> list[ir.OutputColumn] | None:
    """Each ORDER BY item as the column of `scope` it sorts by: a selected
    column by name or position, or else a column of `scope` by a name only one
    of its columns has. None when an item is none of these (an expression, or
    a name that may belong to several columns)."""
    keys: list[ir.OutputColumn] = []
    for item in items:
        expression = item.this if isinstance(item, exp.Ordered) else item
        match expression:
            case exp.Column(table="") if (selected := _named(outputs, expression.name)) is not None:
                key = _in_scope(selected, scope)
            case exp.Literal(is_int=True) if 1 <= int(expression.this) <= len(outputs):
                key = _in_scope(outputs[int(expression.this) - 1], scope)
            case exp.Column():
                key = _named(scope.output_columns, expression.name)
            case _:
                return None
        if key is None:
            return None
        keys.append(key)
    return keys


def _named(columns: tuple[ir.OutputColumn, ...], name: str) -> ir.OutputColumn | None:
    matches = [column for column in columns if column.name.lower() == name.lower()]
    return matches[0] if len(matches) == 1 else None


def _in_scope(column: ir.OutputColumn, scope: ir.RelationExpr) -> ir.OutputColumn | None:
    """`column` as a column of `scope`: itself, or the input column it projects."""
    if column in scope.output_columns:
        return column
    match column.scalar_expr:
        case ir.ColumnRef(column=upstream) if upstream in scope.output_columns:
            return upstream
        case _:
            return None
