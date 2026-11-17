"""Equality groups: the output columns of a relation that carry one value.

A strict equality -- a `WHERE` or inner-join `ON` conjunct `a = b` -- keeps
only rows on which `a` and `b` are non-null and equal; `a IS NOT DISTINCT
FROM b` those on which they are equal or both NULL. Equality groups are a
union-find over those equalities, per relation, over its output columns: an
input's groups pass through every column that passes an input column through,
then the relation's own strict equalities merge them. On every row a group's
members are equal, or all NULL (an outer join padded them, padding nulling a
whole side, or IS NOT DISTINCT FROM equated NULLs): where one is non-null,
every one equals it. A semi join's ON merges through the columns of the
matched right row, which no output carries. An outer join's ON merges columns on the side it pads together. Cross-side
equalities become strict when a filter excludes the padding.

A functional dependency `a ↔ b` also holds for an injective `a = f(b)`, whose
values differ; a group says the values are the same. Facts about values read
it (Enumerated Domains, Foreign Keys through an equality join); uniqueness
never does.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from collections.abc import Callable, Iterable, Mapping
from typing import assert_never

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, analysis, current_arena
from sqlassert.rules.value_typing import _column_typing, exact_comparison

type _Group = tuple[ir.OutputColumn, ...]


@dataclass(frozen=True)
class EqualityGroups:
    """A partition of `relation`'s output columns into value-equal groups.

    Each group lists its columns in output order, and groups are ordered by
    their first column; a column no equality reaches is a group of its own.
    """

    relation: ir.RelationExpr
    groups: tuple[_Group, ...]


@analysis(Stratum.EQUALITY_GROUP)
def _equal_columns(column: ir.OutputColumn) -> Result[frozenset[ir.OutputColumn]]:
    """Every column of `column`'s relation that carries its value, itself included."""
    groups = _equality_groups(column.get_relation(current_arena().program))
    group, = (group for group in groups.value.groups if column in group)
    return Result(frozenset(group), "Equal columns", (column,), [groups.members[group]])


@analysis(Stratum.EQUALITY_GROUP)
def _equality_groups(relation: ir.RelationExpr) -> Result[EqualityGroups]:
    """`relation`'s equality groups; each group's member cites the equalities that merged it."""
    roots = {column: column for column in relation.output_columns}
    evidence: dict[ir.OutputColumn, list[Result[bool]]] = {column: [] for column in relation.output_columns}

    def find(column: ir.OutputColumn) -> ir.OutputColumn:
        while roots[column] is not column:
            column = roots[column]
        return column

    def merge(columns: list[ir.OutputColumn], reason: Result[bool]) -> None:
        found = list(dict.fromkeys(find(column) for column in columns))
        if len(found) < 2:
            return  # Already one group: the first justification suffices.
        root, *others = found
        for other in others:
            roots[other] = root
            evidence[root].extend(evidence.pop(other))
        evidence[root].append(reason)

    outputs: dict[ir.OutputColumn, list[ir.OutputColumn]] = {}
    for source in _inputs(relation):
        passed = _pass_through(relation, source)
        outputs.update(passed)
        groups = _equality_groups(source)
        for group in groups.value.groups:
            own = [column for upstream in group for column in passed.get(upstream, ())]
            merge(own, Result(True, "Equal in the input", (), [groups.members[group]]))

    if isinstance(relation, ir.Join) and relation.kind == "semi":
        # The matched right row's columns carry values too, though no output
        # does: two left columns equal to one right column are equal.
        right = relation.right
        for column in right.output_columns:
            roots[column], evidence[column], outputs[column] = column, [], [column]
        groups = _equality_groups(right)
        for group in groups.value.groups:
            merge(list(group), Result(True, "Equal in the matched right row", (), [groups.members[group]]))

    conjuncts, clause = _strict_conjuncts(relation)
    for (first, second), reason in _predicate_equalities(conjuncts).items():
        if first in outputs and second in outputs:
            merge([*outputs[first], *outputs[second]], Result(True, f"{clause} equality", (reason,)))

    if isinstance(relation, ir.Join) and relation.kind in {"left", "right"}:
        padded = relation.right if relation.kind == "left" else relation.left
        for (first, second), reason in _predicate_equalities(relation.conjuncts).items():
            if first in padded.output_columns and second in padded.output_columns:
                merge([*outputs[first], *outputs[second]], Result(True, "ON equality within the padded side", (reason,)))

    if isinstance(relation, ir.Filter):
        from sqlassert.rules.nullability import non_null_column

        for (first, second), reason in _lax_equalities(relation).value.items():
            definite = non_null_column(first)
            if definite.value:
                merge([first, second], Result(True, "The ON equality holds without padding", (reason,), [definite]))

    if isinstance(relation, ir.SetOperation) and relation.operator == "union":
        typed = {column: arms for column in relation.output_columns if (arms := _typed_arms(relation, column)) is not None}
        for first, second in combinations(typed, 2):
            arm_groups = [_equal_columns(arm) for arm in typed[first]]
            if all(other in group.value for other, group in zip(typed[second], arm_groups)):
                merge([first, second], Result(True, "Equal in both UNION inputs", (relation,), arm_groups))

    grouped: dict[ir.OutputColumn, list[ir.OutputColumn]] = {}
    for column in relation.output_columns:
        grouped.setdefault(find(column), []).append(column)
    members: dict[_Group, Result[bool]] = {}
    for root, columns in grouped.items():
        group = tuple(columns)
        members[group] = (
            Result(True, "Equal columns", group, evidence[root]) if evidence[root]
            else Result(True, "A column equals itself", group)
        )
    value = EqualityGroups(relation, tuple(members))
    return Result(value, "Equality groups", (relation,), list(members.values()), members=members)


# Ordered pairs of columns an equality relates, each with the conjunct that
# relates them.
type Equalities = dict[tuple[ir.OutputColumn, ir.OutputColumn], ir.ScalarExpr]


def _in_every_branch[Key](disjunction: ir.Or, facts: Callable[[tuple[ir.ScalarExpr, ...]], Mapping[Key, object]]) -> list[Key]:
    """The facts every branch of an OR establishes: one of them holds on each row."""
    first, *rest = (facts(ir.conjuncts(part)) for part in disjunction.parts)
    return [key for key in first if all(key in branch for branch in rest)]


def _predicate_equalities(conjuncts: tuple[ir.ScalarExpr, ...]) -> Equalities:
    """The column equalities every row satisfying `conjuncts` meets."""
    pairs: Equalities = {}
    for conjunct in conjuncts:
        match conjunct:
            case ir.Or():
                pairs.update(dict.fromkeys(_in_every_branch(conjunct, _predicate_equalities), conjunct))
            case (
                ir.Equality(left=ir.ColumnRef(column=first) as left, right=ir.ColumnRef(column=second) as right)
                | ir.NullSafeEquality(left=ir.ColumnRef(column=first) as left, right=ir.ColumnRef(column=second) as right)
            ) if exact_comparison(left, right).value:
                pairs[first, second] = pairs[second, first] = conjunct
    return pairs


@analysis(Stratum.COMPARISON)
def _lax_equalities(relation: ir.RelationExpr) -> Result[Equalities]:
    """Directed pairs: where the first is non-null, it equals the second."""
    pairs: Equalities = {}
    for source in _inputs(relation):
        passed = _pass_through(relation, source)
        for (first, second), reason in _lax_equalities(source).value.items():
            for own in passed.get(first, ()):
                for other in passed.get(second, ()):
                    pairs[own, other] = reason
    if isinstance(relation, ir.Join) and relation.kind in {"left", "right"}:
        padded, preserved = (relation.right, relation.left) if relation.kind == "left" else (relation.left, relation.right)
        padded_outputs = _pass_through(relation, padded)
        preserved_outputs = _pass_through(relation, preserved)
        for (first, second), reason in _predicate_equalities(relation.conjuncts).items():
            for own in padded_outputs.get(first, ()):
                for other in preserved_outputs.get(second, ()):
                    pairs[own, other] = reason
    return Result(pairs, "Lax equalities", (relation,))


def _predicate_constants(conjuncts: tuple[ir.ScalarExpr, ...]) -> dict[ir.OutputColumn, tuple[ir.LiteralValue | None, ir.ScalarExpr]]:
    """The constant (SQL NULL included) each column has on every row
    satisfying `conjuncts`, with the conjunct that fixes it."""
    return {column: (value, conjunct) for (column, value), conjunct in _pinned_values(conjuncts).items()}


def _pinned_values(conjuncts: tuple[ir.ScalarExpr, ...]) -> dict[tuple[ir.OutputColumn, ir.LiteralValue | None], ir.ScalarExpr]:
    pinned: dict[tuple[ir.OutputColumn, ir.LiteralValue | None], ir.ScalarExpr] = {}
    for conjunct in conjuncts:
        match conjunct:
            case ir.Or():
                pinned.update(dict.fromkeys(_in_every_branch(conjunct, _pinned_values), conjunct))
            case ir.Equality(left=left, right=right) | ir.NullSafeEquality(left=left, right=right):
                for column, literal in ((left, right), (right, left)):
                    if isinstance(column, ir.ColumnRef) and isinstance(literal, ir.Constant) and literal.value is not None:
                        if exact_comparison(column, literal).value:
                            pinned[column.column, literal.value] = conjunct
            case ir.IsNull(operand=ir.ColumnRef(column=column)):
                pinned[column, None] = conjunct
    return pinned


@analysis(Stratum.COMPARISON)
def _constant_value(column: ir.OutputColumn) -> Result[tuple[ir.LiteralValue | None] | None]:
    """An exact constant, including SQL NULL, on every row carrying this column."""
    relation = column.get_relation(current_arena().program).closed
    unknown_value: Result[tuple[ir.LiteralValue | None] | None] = Result(None, "No exact constant is known", (column,))
    match relation:
        case ir.SetOperation(operator="union"):
            if (arms := _typed_arms(relation, column)) is None:
                return unknown_value
            left, right = (_constant_value(arm) for arm in arms)
            if left.value is not None and left.value == right.value:
                return Result(left.value, "The same constant in both UNION inputs", (column,), [left, right])
            return unknown_value
        case ir.Filter(conjuncts=conjuncts):
            match column.scalar_expr:
                case ir.ColumnRef(column=source) if (constant := _predicate_constants(conjuncts).get(source)) is not None:
                    value, conjunct = constant
                    return Result((value,), "The WHERE clause fixes the value", (conjunct,))
                case ir.ColumnRef(column=source):
                    return _constant_value(source)
                case _:
                    return unknown_value
        case ir.Project() | ir.Alias() | ir.NamedRelation() | ir.Distinct() | ir.Limit() | ir.QualifyByPartition():
            match column.scalar_expr:
                case ir.ColumnRef(column=source):
                    return _constant_value(source)
                case ir.Constant(value=ir.LiteralValue() as literal):
                    return Result((literal,), "A literal is constant", (column,))
                case ir.NullLiteral():
                    return Result((None,), "A NULL literal is constant", (column,))
                case _:
                    return unknown_value
        case ir.Join() | ir.Aggregate() | ir.SetOperation() | ir.OpaqueRelation():
            # Padding, grouping, and the other set operations are not followed.
            return unknown_value
        case _:
            assert_never(relation)


def _same_set_types(operation: ir.SetOperation, columns: Iterable[ir.OutputColumn]) -> bool:
    """Whether set output columns retain the same types as their inputs."""
    return all(_typed_arms(operation, column) is not None for column in columns)


def _typed_arms(operation: ir.SetOperation, column: ir.OutputColumn) -> tuple[ir.OutputColumn, ir.OutputColumn] | None:
    """`column`'s input columns, when it keeps their type: its values are theirs."""
    arms = operation.arms(column)
    return arms if arms is not None and _column_typing(column).value is not None else None


def _inputs(relation: ir.RelationExpr) -> tuple[ir.RelationExpr, ...]:
    """The inputs whose groups hold on `relation`'s pass-through columns.

    Each output row of these takes its pass-through values from one row of
    each listed input (or, for a padded side, NULL in every column).
    """
    relation = relation.closed
    match relation:
        case ir.Alias(source=source) | ir.NamedRelation(body=ir.RelationExpr() as source) | ir.Filter(input=source) | ir.Project(input=source) | ir.Limit(input=source) | ir.Aggregate(input=source) | ir.Distinct(input=source) | ir.QualifyByPartition(input=source):
            return (source,)
        case ir.Join(kind="inner" | "cross" | "left" | "right" | "full", left=left, right=right):
            return (left, right)
        case ir.Join(left=left):
            # A semi or anti join keeps left rows; its right columns have no row.
            return (left,)
        case ir.SetOperation(operator="intersect" | "except", left=left) if _same_set_types(relation, relation.output_columns):
            # Every output row equals some row of the left input.
            return (left,)
        case ir.SetOperation() | ir.NamedRelation() | ir.OpaqueRelation():
            # A UNION row may come from either branch; the right one is not tracked.
            return ()
        case _:
            assert_never(relation)


def _strict_conjuncts(relation: ir.RelationExpr) -> tuple[tuple[ir.ScalarExpr, ...], str]:
    """The conjuncts true on every row of `relation`, and the clause they come from."""
    match relation:
        case ir.Filter(conjuncts=conjuncts):
            return conjuncts, "WHERE"
        case ir.Join(kind="inner" | "cross" | "semi", conjuncts=conjuncts):
            # A semi join's on each output row with its matched right row.
            return conjuncts, "ON"
        case _:
            # An outer join's ON holds on matched rows only; an anti join's on no output row.
            return (), ""


def _pass_through(relation: ir.RelationExpr, source: ir.RelationExpr) -> dict[ir.OutputColumn, list[ir.OutputColumn]]:
    """Each column of `source` that `relation` passes through, with the columns passing it."""
    passed: dict[ir.OutputColumn, list[ir.OutputColumn]] = {}
    for column in relation.output_columns:
        match column.scalar_expr:
            case ir.ColumnRef(column=upstream) if upstream in source.output_columns:
                passed.setdefault(upstream, []).append(column)
            case _:
                pass  # A computed column starts its own group.
    return passed
