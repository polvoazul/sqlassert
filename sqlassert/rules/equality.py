"""Equality groups: the output columns of a relation that carry one value.

A strict equality -- a `WHERE` or inner-join `ON` conjunct `a = b` -- keeps
only rows on which `a` and `b` are non-null and equal. Equality groups are a
union-find over those equalities, per relation, over its output columns: an
input's groups pass through every column that passes an input column through,
then the relation's own strict equalities merge them. On every row a group's
members are equal, or all NULL where an outer join padded them (padding nulls
a whole side). An outer join's ON never merges: it holds on matched rows only,
and a padded row keeps the preserved side's value.

A functional dependency `a ↔ b` also holds for an injective `a = f(b)`, whose
values differ; a group says the values are the same. Facts about values read
it (Enumerated Domains, Foreign Keys through an equality join); uniqueness
never does.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, analysis, current_arena

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

    conjuncts, clause = _strict_conjuncts(relation)
    for conjunct in conjuncts:
        match conjunct:
            case ir.Equality(left=ir.ColumnRef(column=first), right=ir.ColumnRef(column=second)) if first in outputs and second in outputs and _same_type(first, second):
                merge([*outputs[first], *outputs[second]], Result(True, f"{clause} equality", (conjunct,)))
            case _:
                pass  # Other conjuncts only remove rows.

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


def _same_type(first: ir.OutputColumn, second: ir.OutputColumn) -> bool:
    """Equalities are value identities only for known, matching comparison types."""
    first_type = _comparison_type(first).value
    return first is second or first_type is not None and first_type == _comparison_type(second).value


@analysis(Stratum.EQUALITY_GROUP)
def _comparison_type(column: ir.OutputColumn) -> Result[str | None]:
    relation = column.get_relation(current_arena().program)
    match column.scalar_expr:
        case ir.ColumnRef(column=source) if not isinstance(relation, ir.SetOperation):
            return _comparison_type(source)
        case _:
            return Result(column.comparison_type, "Declared comparison type", (column,))


@analysis(Stratum.EQUALITY_GROUP)
def _identity_comparison(column: ir.OutputColumn) -> Result[bool]:
    """Whether equal values of `column` are identical, from its declared type."""
    relation = column.get_relation(current_arena().program)
    match column.scalar_expr:
        case ir.ColumnRef(column=source) if not isinstance(relation, ir.SetOperation):
            return _identity_comparison(source)
        case _:
            return Result(column.identity_comparison, "Declared type compares by identity", (column,))


@analysis(Stratum.EQUALITY_GROUP)
def exact_numeric(column: ir.OutputColumn) -> Result[bool]:
    """Whether `column` is an integer or decimal, from its declared type."""
    relation = column.get_relation(current_arena().program)
    match column.scalar_expr:
        case ir.ColumnRef(column=source) if not isinstance(relation, ir.SetOperation):
            return exact_numeric(source)
        case _:
            return Result(column.exact_numeric, "Declared an integer or decimal", (column,))


def _inputs(relation: ir.RelationExpr) -> tuple[ir.RelationExpr, ...]:
    """The inputs whose groups hold on `relation`'s pass-through columns.

    Each output row of these takes its pass-through values from one row of
    each listed input (or, for a padded side, NULL in every column).
    """
    match relation:
        case ir.Alias(source=source) | ir.NamedRelation(body=ir.RelationExpr() as source) | ir.Filter(input=source) | ir.Project(input=source) | ir.Limit(input=source) | ir.Aggregate(input=source) | ir.Distinct(input=source) | ir.QualifyByPartition(input=source):
            return (source,)
        case ir.Join(kind="inner" | "cross" | "left" | "right" | "full", left=left, right=right):
            return (left, right)
        case ir.Join(left=left):
            # A semi or anti join keeps left rows; its right columns have no row.
            return (left,)
        case ir.SetOperation(operator="intersect" | "except", left=left):
            # Every output row equals some row of the left input.
            return (left,)
        case ir.SetOperation() | ir.NamedRelation() | ir.OpaqueRelation():
            # A UNION row may come from either branch; the right one is not tracked.
            return ()
        case _:
            raise TypeError(f"Unhandled relation: {type(relation).__name__}")


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
