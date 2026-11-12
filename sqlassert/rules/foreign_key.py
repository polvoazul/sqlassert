"""Foreign Keys: Referential Coverage onto a Unique Set of the referenced relation.

Knowledge -- `REFERENCES` constraints, catalog foreign keys, and
`DECLARE FOREIGN KEY` -- supplies coverage only; the referenced Unique Set is
proved separately, so a declaration never vouches for its target. Under
`MATCH SIMPLE` a row with a null referencing component is exempt, so
nullability never weakens a Foreign Key.

Coverage propagates down both sides. On the referencing side, it holds for
columns each output row takes from one row of one covered input: filters,
projections, aliases, views, CTEs, DISTINCT, grouping keys, and either side of
any join, since a padded side is NULL in every column. A column may stand in
for any column of its Equality Group, so an inner equality join onto the
referenced columns establishes coverage. On the referenced side, coverage of
an input carries over only to a relation that keeps every input row's values;
one that may drop rows (a filter, an inner join) must be covered on its own,
except the left input of a proved Exactly One Join, which keeps every left row.
Anything else stops propagation and names where.
"""

from itertools import product
from typing import assert_never

from sqlassert.diagnostics import quoted, quoted_all
from sqlassert import ir
from sqlassert.properties import ReferentialCoverage
from sqlassert.python_engine import Result, Stratum, analysis, any_of, current_arena, unknown
from sqlassert.rules.equality import _equal_columns
from sqlassert.rules.uniqueness import unique_set

type _Columns = tuple[ir.OutputColumn, ...]


@analysis(Stratum.FOREIGN_KEY)
def foreign_key(columns: _Columns, referenced_columns: _Columns) -> Result[bool]:
    shape = _pairing(columns, referenced_columns)
    if not shape.value:
        return shape
    return referential_coverage(columns, referenced_columns) & _referenced_unique_set(referenced_columns)


@analysis(Stratum.FOREIGN_KEY)
def referential_coverage(columns: _Columns, referenced_columns: _Columns) -> Result[bool]:
    shape = _pairing(columns, referenced_columns)
    if not shape.value:
        return shape
    targets = _targets(referenced_columns)
    for target, widened in targets:
        # Pairs, not lists, are the relationship: (a, b) → (x, y) is (b, a) → (y, x).
        pairs = set(zip(columns, target))
        known = [
            any_of(current_arena().accepted[_key(coverage)]) for coverage in _known()
            if set(zip(coverage.columns, coverage.referenced_columns)) == pairs
        ]
        if known:
            return Result(True, f"Known coverage onto {_describe(target)}", columns, [*widened, *known])
        if all(column is referenced for column, referenced in pairs):
            return Result(True, "Columns cover themselves", columns, widened)
    return any_of(_referencing_side(columns, referenced_columns, targets))


def _targets(referenced_columns: _Columns) -> list[tuple[_Columns, list[Result[bool]]]]:
    """The referenced columns, and each upstream column list whose coverage carries over to them.

    Coverage onto an input carries over only through a relation that keeps
    every input row's values. Narrowing the referenced relation (a filter, an
    inner join) may drop the very rows the referencing columns match, so it
    must be covered on its own: only the referencing side can show that.
    Each entry lists the steps that carry coverage from it to the referenced
    columns.
    """
    target = current_arena().program.relation_of(frozenset(referenced_columns))
    assert target is not None
    match target:
        case ir.SetOperation(operator="union"):
            arms = [target.arms(column) for column in referenced_columns]
            if any(pair is None for pair in arms):
                return [(referenced_columns, [])]
            upstream = list(zip(*(pair for pair in arms if pair is not None)))
            reason = "A UNION keeps every row of each branch"
        case _:
            kept = [_kept(target, column) for column in referenced_columns]
            sources = {source for source, _ in filter(None, kept)}
            if None in kept or len(sources) != 1:
                return [(referenced_columns, [])]
            upstream = [tuple(column for _, column in filter(None, kept))]
            reason = f"{_describe(referenced_columns)} keeps the values of every row of its input"
    matched = [_exactly_one(target)] if isinstance(target, ir.Join) and target.kind == "inner" else []
    step = Result(True, reason, (target,), matched)
    return [(referenced_columns, []), *(
        (columns, [step, *steps]) for branch in upstream for columns, steps in _targets(branch)
    )]


def _referencing_side(
    columns: _Columns, referenced_columns: _Columns, targets: list[tuple[_Columns, list[Result[bool]]]]
) -> list[Result[bool]]:
    relation = current_arena().program.relation_of(frozenset(columns))
    semi = _semi_join(relation, columns, referenced_columns) if isinstance(relation, ir.Join) and relation.kind == "semi" else []
    return [*semi, *_input_rows(columns, referenced_columns, targets)]


def _semi_join(join: ir.Join, columns: _Columns, referenced_columns: _Columns) -> list[Result[bool]]:
    """A semi join keeps a row only when some right row satisfies ON, so
    columns ON equates with right columns take those columns' values: coverage
    of the right columns, as one tuple, covers them (`x IN (SELECT id ...)`)."""
    partners: list[list[tuple[ir.OutputColumn, ir.Equality]]] = []
    for column in columns:
        match column.scalar_expr:
            case ir.ColumnRef(column=upstream):
                pass
            case _:
                raise TypeError(f"Join column {column.name} is not a pass-through")
        found = [
            (other.column, conjunct)
            for conjunct in join.conjuncts if isinstance(conjunct, ir.Equality)
            for own, other in ((conjunct.left, conjunct.right), (conjunct.right, conjunct.left))
            if isinstance(own, ir.ColumnRef) and own.column is upstream
            and isinstance(other, ir.ColumnRef) and other.column in join.right.output_columns
        ]
        if not found:
            return []
        partners.append(found)
    alternatives = []
    for chosen in product(*partners):
        matched = tuple(partner for partner, _ in chosen)
        reason = f"({_names(columns)}) equal ({_names(matched)}) of the right row the semi join matched"
        step = Result(True, reason, (join, *columns), [Result(True, "ON equality", (conjunct,)) for _, conjunct in chosen])
        alternatives.append(step & referential_coverage(matched, referenced_columns))
    return alternatives


def _input_rows(
    columns: _Columns, referenced_columns: _Columns, targets: list[tuple[_Columns, list[Result[bool]]]]
) -> list[Result[bool]]:
    """Coverage of the referencing columns, read from the input rows they come from.

    Each output row's passed-through values come from one row of one input, or
    are all NULL where a join padded that input, which MATCH SIMPLE exempts. A
    column may stand in for any column of its Equality Group: on a row where it
    is non-null, they are equal.
    """
    relation = current_arena().program.relation_of(frozenset(columns))
    assert relation is not None
    if isinstance(relation, ir.SetOperation):
        return [_set_operation(relation, columns, referenced_columns)]
    equal = [_equal_columns(column) for column in columns]
    order = {column: index for index, column in enumerate(relation.output_columns)}
    alternatives: dict[_Columns, Result[bool]] = {}
    for chosen in product(*(sorted(group.value, key=order.__getitem__) for group in equal)):
        substituted = [
            Result(True, f"{quoted(column.name)} equals {quoted(stand_in.name)} on every row where it is non-null", (column, stand_in), [group])
            for column, stand_in, group in zip(columns, chosen, equal) if stand_in is not column
        ]
        for target, widened in targets:
            if all(stand_in is referenced for stand_in, referenced in zip(chosen, target)):
                reason = f"Equal to {_describe(target)} on every row where they are non-null"
                return [Result(True, reason, chosen, [*substituted, *widened])]
        upstream = [_passed(relation, column) for column in chosen]
        sources = {passed[0] for passed in upstream if passed is not None}
        if None in upstream or len(sources) != 1:
            continue
        passed = tuple(column for _, column in filter(None, upstream))
        if passed in alternatives:
            continue
        reason = f"Each row's ({_names(chosen)}) come from one input row, or are all NULL where a join padded it"
        alternatives[passed] = Result(True, reason, (relation, *chosen), substituted) & referential_coverage(passed, referenced_columns)
    if alternatives:
        return list(alternatives.values())
    match relation:
        case ir.NamedRelation(body=None):
            elsewhere = [
                _describe(coverage.referenced_columns) for coverage in _known() if set(coverage.columns) == set(columns)
            ]
            hint = f"; these columns reference {', '.join(elsewhere)}" if elsewhere else ""
            named = dict.fromkeys([_describe(referenced_columns), *(
                _describe(target) for target, _ in targets
                if isinstance(current_arena().program.relation_of(frozenset(target)), ir.NamedRelation)
            )])
            missing = unknown(f"No known Foreign Key from {_describe(columns)} to {' or '.join(named)}{hint}", *columns)
            return [missing, *_narrowed(targets)]
        case ir.OpaqueRelation():
            return [unknown("Foreign Keys do not propagate through an unanalyzed relation", relation, *columns)]
        case _:
            return [unknown(
                f"({_names(columns)}) are not passed through from one input row of this {type(relation).__name__}, "
                "so Foreign Keys stop here",
                relation, *columns,
            )]


def _set_operation(relation: ir.SetOperation, columns: _Columns, referenced_columns: _Columns) -> Result[bool]:
    """Coverage of a set operation's rows, from each branch's columns at the same positions."""
    arms = [relation.arms(column) for column in columns]
    if None in arms:
        return unknown("The set operation's inputs are not paired by position", relation, *columns)
    left = referential_coverage(tuple(arm[0] for arm in filter(None, arms)), referenced_columns)
    right = referential_coverage(tuple(arm[1] for arm in filter(None, arms)), referenced_columns)
    match relation.operator:
        case "union":
            return Result(True, "A UNION row comes from either branch, so both must be covered", (relation,)) & left & right
        case "intersect":
            return Result(True, "An INTERSECT row is in both branches, so one covered branch suffices", (relation,)) & (left | right)
        case "except":
            return Result(True, "An EXCEPT row comes from the left branch", (relation,)) & left
        case _:
            assert_never(relation.operator)


def _narrowed(targets: list[tuple[_Columns, list[Result[bool]]]]) -> list[Result[bool]]:
    """Why coverage of a narrowed referenced relation's input does not carry over to it."""
    notes = []
    for target, _ in targets:
        relation = current_arena().program.relation_of(frozenset(target))
        if relation is not None and _narrows(relation):
            reason = f"{_describe(target)} may drop rows of its input, so coverage of its input does not carry over to it"
            notes.append(unknown(reason, relation))
    return notes


def _passed(relation: ir.RelationExpr, column: ir.OutputColumn) -> tuple[ir.RelationExpr, ir.OutputColumn] | None:
    """The input column `column` repeats, when each output row takes it from one input row or padding."""
    match relation, column.scalar_expr:
        case (
            ir.Alias(source=source) | ir.NamedRelation(body=ir.RelationExpr() as source) | ir.Filter(input=source)
            | ir.Project(input=source) | ir.Limit(input=source) | ir.Distinct(input=source) | ir.QualifyByPartition(input=source),
            ir.ColumnRef(column=upstream),
        ):
            return source, upstream
        case ir.Aggregate(input=source, grouping_outputs=grouping), ir.ColumnRef(column=upstream) if column in grouping:
            return source, upstream
        case ir.Join(left=left, right=right), ir.ColumnRef(column=upstream):
            # Any join kind: a padded side is NULL in every column, which MATCH SIMPLE exempts.
            return (left if upstream in left.output_columns else right), upstream
        case _:
            return None  # A computed or aggregated column, or a relation with no input.


def _kept(relation: ir.RelationExpr, column: ir.OutputColumn) -> tuple[ir.RelationExpr, ir.OutputColumn] | None:
    """The input column whose value on every input row `column` takes on some output row."""
    match relation, column.scalar_expr:
        case (
            ir.Alias(source=source) | ir.NamedRelation(body=ir.RelationExpr() as source) | ir.Project(input=source)
            | ir.Distinct(input=source),
            ir.ColumnRef(column=upstream),
        ):
            return source, upstream
        case ir.Aggregate(input=source, grouping_outputs=grouping), ir.ColumnRef(column=upstream) if column in grouping:
            return source, upstream
        case ir.Join(kind="left" | "full", left=left), ir.ColumnRef(column=upstream) if upstream in left.output_columns:
            return left, upstream
        case ir.Join(kind="right" | "full", right=right), ir.ColumnRef(column=upstream) if upstream in right.output_columns:
            return right, upstream
        case ir.Join(kind="inner", left=left), ir.ColumnRef(column=upstream) if upstream in left.output_columns:
            return (left, upstream) if _exactly_one(relation).value else None
        case _:
            return None  # The relation may drop input rows, or the column is computed.


def _exactly_one(join: ir.Join) -> Result[bool]:
    """Whether `join` matches every left row exactly once, so it keeps every left row."""
    from sqlassert.rules.exactly_one import exactly_one_join  # It builds on Referential Coverage.

    return exactly_one_join(join)


def _narrows(relation: ir.RelationExpr) -> bool:
    """Whether `relation` may drop rows of an input it passes columns from."""
    match relation:
        case ir.Filter() | ir.Limit() | ir.QualifyByPartition() | ir.Join() | ir.SetOperation(operator="intersect" | "except"):
            return True
        case _:
            return False


def _referenced_unique_set(referenced_columns: _Columns) -> Result[bool]:
    relation = current_arena().program.relation_of(frozenset(referenced_columns))
    match relation:
        case ir.NamedRelation(role=ir.RelationRole.TABLE, is_schema_complete=False):
            return unknown(
                f"Referenced relation {relation.name} has no complete declared schema, so no Unique Set is known on it",
                relation,
            )
        case _:
            unique = unique_set(frozenset(referenced_columns))
            reason = f"The referenced columns {_describe(referenced_columns)} must form a Unique Set"
            return Result(unique.value, reason, referenced_columns, [unique], kind="all")


def _pairing(columns: _Columns, referenced_columns: _Columns) -> Result[bool]:
    program = current_arena().program
    if not columns or len(columns) != len(referenced_columns):
        return unknown("A Foreign Key pairs equally many referencing and referenced columns", *columns, *referenced_columns)
    if program.relation_of(frozenset(columns)) is None:
        return unknown("Referencing columns must belong to one relation", *columns)
    if program.relation_of(frozenset(referenced_columns)) is None:
        return unknown("Referenced columns must belong to one relation", *referenced_columns)
    return Result(True, "Columns pair by position", (*columns, *referenced_columns))


def _known() -> list[ReferentialCoverage]:
    return [coverage for coverage in current_arena().knowledge if isinstance(coverage, ReferentialCoverage)]


def _key(coverage: ReferentialCoverage) -> tuple:
    return ("referential_coverage", (("columns", coverage.columns), ("referenced_columns", coverage.referenced_columns)))


def _names(columns: _Columns) -> str:
    return quoted_all(column.name for column in columns)


def _describe(columns: _Columns) -> str:
    relation = current_arena().program.relation_of(frozenset(columns))
    match relation:
        case ir.NamedRelation(name=name) | ir.Alias(name=name):
            return f"{name}({_names(columns)})"
        case _:
            return f"the {type(relation).__name__}'s ({_names(columns)})"
