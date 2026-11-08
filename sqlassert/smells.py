"""Low-level suggestions: code smells, from the Kossmann et al. 2022 triage (issue 15).

A Smell is not a rewrite: the SQL may be what its author meant. It names a
place where the result depends on something the SQL does not pin down, and
the proof that would have pinned it, which failed:

- `LIMIT` or `OFFSET` whose `ORDER BY` does not strictly determine every
  selected column (or that has no `ORDER BY`): rows tied on the sort keys come
  in an arbitrary order, so which rows are kept may change from run to run
  (Kossmann's "unstable sorting", read the other way round).
- An aggregate over a join that can repeat the rows its argument comes from
  (issue 41): a fan trap when the join is many to many, a chasm trap when it
  is one to many and the argument is on the side with one row per key. Every
  aggregate counts, `min` and `max` included: the join is wrong either way.
  `count(*)` has no argument, so only a many-to-many join is a trap for it.
  Silent when the input whose rows are repeated has no known Unique Set.
- A scalar subquery not proved to return at most one row (issue 38): more
  than one is a runtime error. Uncorrelated, its body must have at most one
  row; correlated, the columns its WHERE binds must reach its Row Identity.

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
from sqlassert.rules.join_cardinality import _at_most_one_match, many_to_one_join, one_to_many_join, unique_join
from sqlassert.rules.uniqueness import _closure, _reaches_row_identity
from sqlassert.sql_parse import ParsedProgram


class Concern(Enum):
    ARBITRARY_LIMIT = "Arbitrary LIMIT"
    FAN_TRAP = "Fan trap"
    CHASM_TRAP = "Chasm trap"
    SCALAR_SUBQUERY = "Scalar subquery"


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
    join_labels = {relation: site.label for site, relation in program.attachment_sites if isinstance(relation, ir.Join)}
    for site, relation in program.attachment_sites:
        node = syntax.get((site.offset, site.trailing))
        if isinstance(relation, ir.Limit) and relation in reachable and isinstance(node, exp.Select):
            found.extend(_arbitrary_limit(site, relation, node))
        aggregate = relation.input if isinstance(relation, ir.Limit) else relation
        if isinstance(aggregate, ir.Aggregate) and aggregate in reachable:
            found.extend(_traps(site, aggregate, join_labels))
    for site, join in program.scalar_subqueries:
        if join in reachable:
            found.extend(_scalar_subquery(site, join))
    return sorted(found, key=lambda item: item.site.offset)


def _scalar_subquery(site: AttachmentSite, join: ir.Join) -> list[Smell]:
    """A scalar subquery that may return more than one row.

    Its join is many to one by construction (more rows fail the query), so
    the question is asked of its body: uncorrelated, under the implied LIMIT
    1, whether the body has at most one row (the empty set is a Unique Set);
    correlated, whether the columns ON binds reach the body's Row Identity."""
    match join.right:
        case ir.Limit(input=body) if join.predicate is None:
            proof = _reaches_row_identity(_closure(body, frozenset(), frozenset()))
            reason = "it is not proved to have at most one row"
        case _:
            proof = _at_most_one_match(join, "right")
            reason = "the columns its WHERE binds to the outer row are not proved to be a Unique Set"
    if proof.value:
        return []
    message = f"this scalar subquery may return more than one row, a runtime error: {reason}"
    return [Smell(site, Concern.SCALAR_SUBQUERY, message, proof)]


# Join traps ------------------------------------------------------------------


def _traps(site: AttachmentSite, aggregate: ir.Aggregate, join_labels: dict[ir.Join, str]) -> list[Smell]:
    """Each aggregate call whose input rows a join may repeat."""
    found: list[Smell] = []
    for call in aggregate.calls:
        inputs = [ir.determinants(argument) for argument in call.arguments]
        if any(columns is None for columns in inputs):
            continue  # An argument whose columns are unknown.
        columns = frozenset(column for columns in inputs if columns is not None for column in columns)
        if not columns:
            # `count(*)`, or a constant: it counts the input's rows.
            for join, proof in _many_to_many_joins(aggregate.input):
                message = (
                    f"{call.description} counts the rows of {_label(join, join_labels)}, which is not proved "
                    "to repeat rows of only one input (a fan trap): rows may be counted more than once"
                )
                found.append(Smell(site, Concern.FAN_TRAP, message, proof))
            continue
        repeated = _repeating_join(aggregate.input, columns)
        if repeated is None:
            continue
        join, proof = repeated
        if unique_join(join).value:
            concern, why = Concern.CHASM_TRAP, "is one to many, and the argument is on the side with one row per key"
        else:
            concern, why = Concern.FAN_TRAP, "is not proved to repeat rows of only one input"
        message = (
            f"{call.description} reads rows that {_label(join, join_labels)} may repeat: the join {why} "
            f"({concern.value.lower()}), so a value may be counted more than once"
        )
        found.append(Smell(site, concern, message, proof))
    return found


def _repeating_join(relation: ir.RelationExpr, columns: frozenset[ir.OutputColumn]) -> tuple[ir.Join, Result] | None:
    """The first join, from `relation` down to the input `columns` come from,
    not proved to keep that input's rows unrepeated, with the failed proof.
    None when every join on the way is proved, or the columns cannot be traced
    to one input of a join."""
    while True:
        match relation:
            case ir.Join(kind="semi" | "anti"):
                side = relation.left  # It never repeats a row.
            case ir.Join():
                upstream = _upstream(relation, columns)
                if upstream is None:
                    return None
                if upstream <= set(relation.left.output_columns):
                    side, proof = relation.left, many_to_one_join(relation)
                elif upstream <= set(relation.right.output_columns):
                    side, proof = relation.right, one_to_many_join(relation)
                else:
                    return None  # The argument reads both inputs.
                if not proof.value and _has_unique_set(side):
                    return relation, proof
            case ir.Filter(input=side) | ir.Alias(source=side) | ir.Project(input=side) | ir.Limit(input=side):
                pass
            case ir.NamedRelation(body=ir.RelationExpr() as side):
                pass
            case _:
                return None  # A base table, or a stage that drops or merges rows.
        upstream = _upstream(relation, columns)
        if upstream is None or not upstream <= set(side.output_columns):
            return None
        relation, columns = side, upstream


def _many_to_many_joins(relation: ir.RelationExpr):
    """Each join under `relation` not proved to repeat rows of at most one
    input, both of whose inputs have a known Unique Set, with the failed proof."""
    match relation:
        case ir.Join(kind="semi" | "anti", left=left):
            yield from _many_to_many_joins(left)
        case ir.Join(left=left, right=right):
            proof = unique_join(relation)
            if not proof.value and _has_unique_set(left) and _has_unique_set(right):
                yield relation, proof
            yield from _many_to_many_joins(left)
            yield from _many_to_many_joins(right)
        case ir.Filter(input=source) | ir.Alias(source=source) | ir.Project(input=source) | ir.Limit(input=source):
            yield from _many_to_many_joins(source)
        case ir.NamedRelation(body=ir.RelationExpr() as source):
            yield from _many_to_many_joins(source)
        case _:
            pass


def _upstream(relation: ir.RelationExpr, columns: frozenset[ir.OutputColumn]) -> frozenset[ir.OutputColumn] | None:
    """The input columns `columns` of `relation` are computed from, if known."""
    found: set[ir.OutputColumn] = set()
    for column in columns:
        inputs = ir.determinants(column.scalar_expr)
        if inputs is None:
            return None
        found.update(inputs)
    return frozenset(found)


def _has_unique_set(relation: ir.RelationExpr) -> bool:
    """Some set of `relation`'s columns is a Unique Set: its rows are distinct."""
    columns = frozenset(relation.output_columns)
    return bool(columns) and _reaches_row_identity(_closure(relation, columns, columns)).value


def _label(join: ir.Join, join_labels: dict[ir.Join, str]) -> str:
    return join_labels.get(join, "a join")


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
