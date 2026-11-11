"""Functional dependencies, their closure, and the Unique Sets closure proves.

Every relation has a Row Identity that tells its rows apart. A key is a
functional dependency onto it, an equality is a pair of dependencies, and a
constant is a dependency from the empty set, so one question decides
uniqueness: does the closure of a column set reach the Row Identity?

`_functional_dependencies(relation)` holds only the dependencies a relation
introduces itself, over its own output columns and Row Identities. The
dependencies of its inputs are not copied up: `_closure` maps the columns it
has reached into each input, takes the input's closure there, and maps the
result back. Columns an operation drops therefore still take part ("hide,
don't delete"), and each Alias reasons about its own rows even when two
Aliases share one Named Relation.

A strict dependency holds on every pair of rows, NULL agreeing with NULL; a
lax one only where its determinant is non-null. Closure steps through a lax
dependency only when every determinant column is definite: requested as
non-null by the caller, or proved non-null.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import combinations
from typing import assert_never

from sqlassert.diagnostics import quoted
from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, all_of, analysis, any_of, current_arena, unknown
from sqlassert.rules.column_set import Attribute, Closure, RowIdentity
from sqlassert.rules.equality import _identity_comparison, _pins, exact_numeric
from sqlassert.rules.nullability import Padding, _null_sources, _padded_sides, non_null_column, non_null_columns


@dataclass(frozen=True)
class Dependency:
    """`determinant → dependent`; an empty determinant makes the dependent constant."""

    determinant: frozenset[Attribute]
    dependent: Attribute
    strict: bool


type Dependencies = dict[Dependency, Result[bool]]


# Public analyses ---------------------------------------------------------------


@analysis(Stratum.UNIQUE_SET)
def unique_set(columns: frozenset[ir.OutputColumn]) -> Result[bool]:
    relation = current_arena().program.relation_of(columns)
    if relation is None:
        return unknown("A unique set must have columns belonging to one relation", *columns)
    return _reaches_row_identity(_closure(relation, frozenset(columns), frozenset(columns)))


@analysis(Stratum.UNIQUE_SET)
def non_null_unique_set(columns: frozenset[ir.OutputColumn]) -> Result[bool]:
    return unique_set(columns) & non_null_columns(columns)


@analysis(Stratum.CLOSURE)
def functional_dependency(determinant: frozenset[ir.OutputColumn], dependent: frozenset[ir.OutputColumn]) -> Result[bool]:
    relation = current_arena().program.relation_of(determinant | dependent)
    if relation is None:
        return unknown("A functional dependency must have columns belonging to one relation", *determinant, *dependent)
    closure = _closure(relation, frozenset(determinant), frozenset(determinant))
    ordered = [column for column in relation.output_columns if column in dependent]
    return all_of(
        closure.members[column] if column in closure.value.columns
        else unknown(f"{quoted(column.name)} is not determined", column)
        for column in ordered
    )


def _reaches_row_identity(closure: Result[Closure]) -> Result[bool]:
    identity = RowIdentity(closure.value.relation)
    if identity in closure.value.columns:
        return Result(True, "The columns determine the row", (closure.value.relation,), [closure.members[identity]])
    gaps = _uncovered_keys(closure)
    if not gaps:
        return unknown("No known unique set on this relation", closure.value.relation)
    return any_of(gaps)


def _uncovered_keys(closure: Result[Closure]) -> list[Result[bool]]:
    """Each known key the closure misses, naming its missing columns, down
    through every input the closure looked into."""
    relation = closure.value.relation
    identity = RowIdentity(relation)
    gaps: list[Result[bool]] = []
    for dependency, evidence in _functional_dependencies(relation).value.items():
        if dependency.dependent != identity or dependency.determinant <= closure.value.columns:
            continue
        missing = [column for column in relation.output_columns if column in dependency.determinant - closure.value.columns]
        gaps.append(evidence & unknown("Unique-set columns are not covered", *missing))
    for premise in closure.value.inputs:
        if RowIdentity(premise.value.relation) not in premise.value.columns:
            gaps.extend(_uncovered_keys(premise))
    return gaps


# Closure -----------------------------------------------------------------------


@analysis(Stratum.CLOSURE)
def _closure(relation: ir.RelationExpr, columns: frozenset[Attribute], definite: frozenset[Attribute]) -> Result[Closure]:
    """Every attribute of `relation` that `columns` determine, on the pairs of
    rows where every `definite` attribute is non-null.

    Counter-based (Paulley's FD-graph closure): a dependency fires once all of
    its determinant is reached. Each reached attribute's member cites the
    dependency or input that reached it.
    """
    reached: dict[Attribute, Result[bool]] = {}
    queue: list[Attribute] = []

    def reach(attribute: Attribute, evidence: Result[bool]) -> None:
        if attribute in reached:
            reached[attribute] = reached[attribute] | evidence
            return
        reached[attribute] = evidence
        queue.append(attribute)

    def is_definite(attribute: Attribute) -> bool:
        return attribute in definite or _definite(relation, attribute)

    dependencies = _functional_dependencies(relation)
    waiting: dict[Dependency, int] = {}
    uses: dict[Attribute, list[Dependency]] = {}
    for dependency in dependencies.value:
        waiting[dependency] = len(dependency.determinant)
        for attribute in dependency.determinant:
            uses.setdefault(attribute, []).append(dependency)

    def fire(dependency: Dependency) -> None:
        if not dependency.strict and not all(is_definite(attribute) for attribute in dependency.determinant):
            return  # Lax steps need non-null middles (Paulley fd7b).
        premises = [dependencies.value[dependency], *(reached[attribute] for attribute in dependency.determinant)]
        reach(dependency.dependent, Result(True, "Functional dependency", (relation, dependency.dependent), premises))

    for column in columns:
        reach(column, Result(True, "In the requested set", (column,)))
    for dependency, count in waiting.items():
        if count == 0:
            fire(dependency)
    inputs: dict[ir.RelationExpr, Result[Closure]] = {}
    while True:
        while queue:
            attribute = queue.pop()
            if attribute == RowIdentity(relation):
                for column in _row_attributes(relation):
                    reach(column, Result(True, "The row identity determines every column", (relation,), [reached[attribute]]))
            for dependency in uses.get(attribute, ()):
                waiting[dependency] -= 1
                if waiting[dependency] == 0:
                    fire(dependency)
        for delegation in _delegations(relation):
            closure = delegation.run(reached, is_definite)
            if closure is None:
                continue
            inputs[delegation.input] = closure
            for source, attribute in delegation.back.items():
                if source in closure.value.columns and attribute not in reached:
                    reach(attribute, Result(True, delegation.reason, (relation, attribute), [closure.members[source]]))
            for attribute, sources in delegation.computed.items():
                if sources <= closure.value.columns and attribute not in reached:
                    reason = "A deterministic function of determined input columns"
                    reach(attribute, Result(True, reason, (relation, attribute), [closure.members[source] for source in sources]))
        if not queue:
            break
    value = Closure(relation, set(reached), tuple(inputs.values()))
    return Result(value, "Closure", (relation,), list(inputs.values()), members=reached)


def _row_attributes(relation: ir.RelationExpr) -> tuple[Attribute, ...]:
    match relation:
        case ir.Join(kind="semi" | "anti", left=left):
            return (*relation.output_columns, RowIdentity(left))
        case ir.Join(left=left, right=right):
            return (*relation.output_columns, RowIdentity(left), RowIdentity(right))
        case _:
            return relation.output_columns


def _definite(relation: ir.RelationExpr, attribute: Attribute) -> bool:
    """Whether `attribute` is non-null on every row of `relation`."""
    match attribute:
        case RowIdentity(relation=owner) if owner is relation:
            return True
        case RowIdentity(relation=side):
            # An input's identity at a join: NULL on the rows that pad its side.
            return isinstance(relation, ir.Join) and (side is relation.left) not in _padded_sides(relation)
        case ir.OutputColumn():
            return non_null_column(attribute).value


@dataclass(frozen=True)
class _Delegation:
    """How rows of a relation map onto rows of one input, for taking closure there.

    `down` maps the relation's attributes to the input's; `back` maps input
    attributes reached there back up, and `computed` maps each of the
    relation's columns computed from input attributes to them: reaching all of
    them reaches it. `valid` says whether two rows agreeing on the reached
    attributes may be compared as input rows at all.
    """

    input: ir.RelationExpr
    down: dict[Attribute, Attribute]
    back: dict[Attribute, Attribute]
    reason: str
    valid: Callable[[dict[Attribute, Result[bool]], Callable[[Attribute], bool]], bool] | None = None
    computed: dict[Attribute, frozenset[Attribute]] = field(default_factory=dict)

    def run(self, reached: dict[Attribute, Result[bool]], is_definite: Callable[[Attribute], bool]) -> Result[Closure] | None:
        if self.valid is not None and not self.valid(reached, is_definite):
            return None
        columns = frozenset(self.down[attribute] for attribute in reached if attribute in self.down)
        definite = frozenset(self.down[attribute] for attribute in reached if attribute in self.down and is_definite(attribute))
        return _closure(self.input, columns, definite)


def _delegations(relation: ir.RelationExpr) -> list[_Delegation]:
    relation = relation.closed
    match relation:
        case ir.Alias(source=source) | ir.NamedRelation(body=ir.RelationExpr() as source) | ir.Filter(input=source) | ir.Project(input=source) | ir.Limit(input=source) | ir.QualifyByPartition(input=source):
            return [_row_preserving(relation, source)]
        case ir.Join(kind="inner" | "cross" | "left" | "right" | "full"):
            return [_join_side(relation, True), _join_side(relation, False)]
        case ir.Join(kind="semi" | "anti", left=source):
            # Each output row is one left row.
            return [_row_preserving(relation, source)]
        case ir.Aggregate(input=source):
            return [_grouped(relation, source)]
        case ir.Distinct(input=source):
            return [_grouped(relation, source)]
        case ir.SetOperation(operator="intersect" | "except", left=source):
            # Every output row equals some row of the left input.
            return [_grouped(relation, source)]
        case ir.Join() | ir.SetOperation() | ir.NamedRelation() | ir.OpaqueRelation():
            return []
        case _:
            assert_never(relation)


def _pass_through(relation: ir.RelationExpr, source: ir.RelationExpr) -> dict[ir.OutputColumn, ir.OutputColumn]:
    """Each output column that passes an input column through, with that column."""
    mapping: dict[ir.OutputColumn, ir.OutputColumn] = {}
    for column in relation.output_columns:
        match column.scalar_expr:
            case ir.ColumnRef(column=upstream) if upstream in source.output_columns:
                mapping[column] = upstream
            case _:
                pass  # A computed column passes nothing through.
    return mapping


def _one_to_one(relation: ir.RelationExpr, source: ir.RelationExpr) -> dict[ir.OutputColumn, ir.OutputColumn]:
    """Each output column that is an input column, or a one-to-one function
    of an integer or decimal one (`a + 1`), with that column: equal on one,
    equal on the other."""
    passed = _pass_through(relation, source)
    mapping: dict[ir.OutputColumn, ir.OutputColumn] = {}
    for column in relation.output_columns:
        upstream = ir.injective_input(column.scalar_expr)
        if column not in passed and upstream in source.output_columns and exact_numeric(upstream).value:
            mapping[column] = upstream
    return mapping | passed  # Last, so mapping back prefers a pass-through.


def _row_preserving(relation: ir.RelationExpr, source: ir.RelationExpr) -> _Delegation:
    """Each output row is one input row, so every input dependency holds."""
    down: dict[Attribute, Attribute] = {own: upstream for own, upstream in _one_to_one(relation, source).items()}
    down[RowIdentity(relation)] = RowIdentity(source)
    return _Delegation(
        source, down, {upstream: own for own, upstream in down.items()}, "Determined in the input",
        computed=_computed(relation, source),
    )


def _grouped(relation: ir.RelationExpr, source: ir.RelationExpr) -> _Delegation:
    """Each output row stands for input rows that share its pass-through values.

    Two output rows that agree on some of those values have representatives
    that agree on them, so whatever they determine in the input is equal for
    the two representatives. That carries back to an output column only if it
    is a pass-through value (or, for an aggregate's AnyAggregate column, a
    value constant across the group); and to the Row Identity because one
    input row belongs to one output row.
    """
    down: dict[Attribute, Attribute] = {own: upstream for own, upstream in _one_to_one(relation, source).items()}
    back: dict[Attribute, Attribute] = {upstream: own for own, upstream in down.items()}
    back[RowIdentity(source)] = RowIdentity(relation)
    if isinstance(relation, ir.Aggregate):
        grouping = frozenset(down[column] for column in relation.grouping_outputs if column in down)
        constant = _closure(source, grouping, frozenset()).value.columns
        down = {column: upstream for column, upstream in down.items() if column in relation.grouping_outputs}
        for column in relation.output_columns:
            match column.scalar_expr:
                case ir.AnyAggregate(input=ir.ColumnRef(column=upstream)) if upstream in constant:
                    back[upstream] = column
                case _:
                    pass  # Count-sensitive and opaque aggregates are new values.
    # A computed column of an Aggregate that is not a grouping item wraps its
    # value in AnyAggregate, so only grouping items and DISTINCT values remain.
    return _Delegation(source, down, back, "Determined in the input rows", computed=_computed(relation, source))


def _computed(relation: ir.RelationExpr, source: ir.RelationExpr) -> dict[Attribute, frozenset[Attribute]]:
    """Paulley §3.1.4: each column computed from columns of `source` by a pure
    function, with those columns. Only columns compared by identity qualify:
    under NOCASE `'a' = 'A'` while `upper(x) || x` tells them apart, and a
    DOUBLE's `-0.0 = 0.0` while its text does not."""
    computed: dict[Attribute, frozenset[Attribute]] = {}
    for column in relation.output_columns:
        if isinstance(column.scalar_expr, ir.ColumnRef):
            continue  # Pass-through columns map back directly.
        inputs = _identity_inputs(column.scalar_expr, source.output_columns)
        if inputs is not None:
            computed[column] = frozenset(inputs)
    return computed


def _identity_inputs(expression: ir.ScalarExpr, columns: tuple[ir.OutputColumn, ...]) -> tuple[ir.OutputColumn, ...] | None:
    """The columns `expression` is a pure function of, when all are among
    `columns` and compared by identity."""
    inputs = ir.determinants(expression)
    if inputs is None or not all(column in columns and _identity_comparison(column).value for column in inputs):
        return None
    return inputs


def _join_side(join: ir.Join, left: bool) -> _Delegation:
    side = join.left if left else join.right
    offset = 0 if left else len(join.left.output_columns)
    down: dict[Attribute, Attribute] = {
        own: upstream for own, upstream in zip(join.output_columns[offset:offset + len(side.output_columns)], side.output_columns)
    }
    down[RowIdentity(side)] = RowIdentity(side)
    back = {upstream: own for own, upstream in down.items()}
    if left not in _padded_sides(join):
        return _Delegation(side, down, back, "Determined in the joined input")

    def valid(reached, is_definite) -> bool:
        # A padded side's columns are all NULL on padded rows. Comparing two
        # rows as input rows is sound when they are both matched or both
        # padded: when some reached column of the side is NULL only on padded
        # rows (Paulley's η), or when the other side fixes which rows match.
        for attribute in reached:
            if attribute == RowIdentity(side) or (attribute in down and is_definite(attribute)):
                return True
            if attribute in down and isinstance(attribute, ir.OutputColumn):
                if _null_sources(attribute).value <= {Padding(join, left)}:
                    return True
        status = _match_status(join, left)
        return status is not None and status <= set(reached)

    return _Delegation(side, down, back, "Determined in the null-supplying input", valid)


def _match_status(join: ir.Join, padded_left: bool) -> frozenset[Attribute] | None:
    """Attributes of the preserved side that fix whether a row matched, for a
    left or right join; None for a full join, whose sides both pad."""
    if join.kind not in {"left", "right"}:
        return None
    preserved = join.right if padded_left else join.left
    outputs = _join_outputs(join)
    referenced = set()
    for conjunct in join.conjuncts:
        operands = _recognized_operands(conjunct)
        if operands is None:
            # An unrecognized conjunct: only the preserved row itself fixes it.
            return frozenset({RowIdentity(preserved)})
        for operand in operands:
            if isinstance(operand, ir.ColumnRef) and operand.column in preserved.output_columns:
                referenced.add(outputs[operand.column])
    return frozenset(referenced)


def _recognized_operands(conjunct: ir.ScalarExpr) -> tuple[ir.ScalarExpr, ...] | None:
    """A recognized conjunct's operands, when each is a column or a literal."""
    match conjunct:
        case ir.Equality(left=left, right=right):
            operands: tuple[ir.ScalarExpr, ...] = (left, right)
        case ir.IsNull(operand=operand) | ir.IsNotNull(operand=operand):
            operands = (operand,)
        case _:
            return None
    if all(isinstance(operand, ir.ColumnRef | ir.Constant | ir.NullLiteral) for operand in operands):
        return operands
    return None


def _join_outputs(join: ir.Join) -> dict[ir.OutputColumn, ir.OutputColumn]:
    """Each input column of `join` with the output column passing it through."""
    return dict(zip((*join.left.output_columns, *join.right.output_columns), join.output_columns))


# Functional dependencies ---------------------------------------------------------


@analysis(Stratum.FUNCTIONAL_DEPENDENCY)
def _functional_dependencies(relation: ir.RelationExpr) -> Result[Dependencies]:
    """The dependencies `relation` introduces itself, each with its evidence."""
    dependencies: Dependencies = {}

    def add(determinant, dependent: Attribute, strict: bool, evidence: Result[bool]) -> None:
        dependency = Dependency(frozenset(determinant), dependent, strict)
        dependencies[dependency] = dependencies[dependency] | evidence if dependency in dependencies else evidence

    identity = RowIdentity(relation)
    columns = set(relation.output_columns)
    for (name, arguments), accepted in current_arena().accepted.items():
        known = dict(arguments)
        match name:
            case "unique_set" if known["columns"] <= columns:
                add(known["columns"], identity, False, any_of(accepted))
            case "functional_dependency" if known["determinant"] | known["dependent"] <= columns:
                for dependent in known["dependent"]:
                    add(known["determinant"], dependent, False, any_of(accepted))
            case _:
                pass  # Other Knowledge is not a dependency of this relation.

    for column in relation.output_columns:
        match column.scalar_expr:
            case ir.Constant() | ir.NullLiteral():
                add((), column, True, Result(True, "A literal is constant", (column,)))
            case _:
                pass  # Other expressions introduce no dependency here.

    relation = relation.closed

    match relation:
        case ir.Filter(input=source, conjuncts=conjuncts):
            outputs = {upstream: own for own, upstream in _pass_through(relation, source).items()}
            for determinant, dependent, evidence in _equality_dependencies(conjuncts, outputs, "WHERE"):
                add(determinant, dependent, True, evidence)
        case ir.Join():
            for determinant, dependent, strict, evidence in _join_dependencies(relation):
                add(determinant, dependent, strict, evidence)
        case ir.Aggregate(grouping_outputs=grouping):
            add(grouping, identity, True, Result(True, "GROUP BY makes its grouping columns a key", (relation,)))
        case ir.Distinct():
            add(relation.output_columns, identity, True, Result(True, "DISTINCT makes its output columns a key", (relation,)))
        case ir.QualifyByPartition(partition_outputs=partition):
            add(partition, identity, True, Result(True, "One row is kept per partition", (relation,)))
        case ir.SetOperation(distinct=True):
            add(relation.output_columns, identity, True, Result(True, f"{relation.operator.upper()} removes duplicate rows", (relation,)))
        case ir.Limit(count=int() as count) if count <= 1:
            add((), identity, True, Result(True, "LIMIT keeps at most one row", (relation,)))
        case ir.Alias() | ir.NamedRelation() | ir.Project() | ir.Limit() | ir.SetOperation() | ir.OpaqueRelation():
            pass
        case _:
            assert_never(relation)
    return Result(dependencies, "Functional dependencies", (relation,), list(dependencies.values()))


def _equality_dependencies(conjuncts: tuple[ir.ScalarExpr, ...], outputs: dict[ir.OutputColumn, ir.OutputColumn], clause: str):
    """Dependencies of conjuncts that hold on every row: `a = b` both ways,
    `a = literal` and `a IS NULL` from the empty set, and `b = f(x...)` from
    `x...` (Paulley Claim 20) when f is pure and they compare by identity."""
    for conjunct in conjuncts:
        evidence = Result(True, f"{clause} conjunct", (conjunct,))
        match conjunct:
            # An equality that casts, or compares under a collation, relates
            # values that differ: it gives no dependency.
            case ir.Equality(left=ir.ColumnRef(column=first) as left, right=ir.ColumnRef(column=second) as right) if first in outputs and second in outputs:
                if _pins(second, left):
                    yield (outputs[first],), outputs[second], evidence
                if _pins(first, right):
                    yield (outputs[second],), outputs[first], evidence
            case ir.Equality(left=ir.ColumnRef(column=column), right=ir.Constant() as constant) | ir.Equality(left=ir.Constant() as constant, right=ir.ColumnRef(column=column)) if column in outputs:
                if _pins(column, constant):
                    yield (), outputs[column], evidence
            case ir.IsNull(operand=ir.ColumnRef(column=column)) if column in outputs:
                yield (), outputs[column], evidence
            case ir.Equality(left=left, right=right):
                for target, value in ((left, right), (right, left)):
                    if not (isinstance(target, ir.ColumnRef) and target.column in outputs and _identity_comparison(target.column).value):
                        continue
                    inputs = _identity_inputs(value, tuple(outputs))
                    if inputs is not None:
                        yield tuple(outputs[column] for column in inputs), outputs[target.column], evidence
                    # One-to-one: `b = a + 1` also gives b -> a, when both are exact.
                    source = ir.injective_input(value)
                    if source in outputs and exact_numeric(source).value and exact_numeric(target.column).value:
                        yield (outputs[target.column],), outputs[source], evidence
            case _:
                pass  # Other conjuncts only remove rows.


def _join_dependencies(join: ir.Join):
    from sqlassert.rules.join_cardinality import many_to_one_join

    outputs = _join_outputs(join)
    if join.kind == "semi":
        # The matched right row satisfies ON; only left columns are output.
        for determinant, dependent, strict_evidence in _equality_dependencies(join.conjuncts, outputs, "ON"):
            yield determinant, dependent, True, strict_evidence
        return
    if join.kind == "anti":
        return  # No right row satisfies ON.
    left, right = RowIdentity(join.left), RowIdentity(join.right)
    identity = RowIdentity(join)
    yield (left, right), identity, True, Result(True, "A joined row is one row of each input", (join,))
    # Not for a right or full join: its padded rows share one null left row.
    if join.kind in {"inner", "left", "cross"}:
        proof = many_to_one_join(join)
        if proof.value:
            yield (left,), identity, True, proof
            yield (left,), right, True, proof
    if join.scalar_subquery and join.predicate is not None:
        # Which rows a correlated scalar subquery matches depends only on the
        # outer columns its WHERE reads, when that WHERE is a pure function.
        inputs = ir.determinants(join.predicate)
        if inputs is not None:
            outer = tuple(outputs[column] for column in inputs if column in join.left.output_columns)
            yield outer, right, True, Result(True, "A correlated scalar subquery's row depends only on the outer columns its WHERE reads", (join,))
    match join.kind:
        case "inner" | "cross":
            for determinant, dependent, strict_evidence in _equality_dependencies(join.conjuncts, outputs, "ON"):
                yield determinant, dependent, True, strict_evidence
        case "left" | "right":
            yield from _outer_on_dependencies(join, outputs, padded_left=join.kind == "right")
        case _:
            pass  # A full join pads both sides: its ON condition holds on neither.


def _outer_on_dependencies(join: ir.Join, outputs: dict[ir.OutputColumn, ir.OutputColumn], padded_left: bool):
    """Paulley Lemma 9: what an outer join's ON condition still guarantees.

    The preserved row fixes whether a row matched, and a matched row satisfies
    the condition, so the preserved side's ON columns determine every column
    of the padded side the condition binds to them or to a literal. Equalities
    within the padded side stay strict (padded rows are NULL on both sides);
    a cross-side equality is lax from the padded side.
    """
    padded = join.left if padded_left else join.right
    status = _match_status(join, padded_left)
    assert status is not None
    for conjunct in join.conjuncts:
        evidence = Result(True, "ON conjunct of an outer join", (conjunct,))
        match conjunct:
            case ir.Equality(left=first, right=second):
                pairs = ((first, second), (second, first))
            case ir.IsNull(operand=ir.ColumnRef(column=column)) if column in padded.output_columns:
                # NULL on matched rows and on padded rows alike.
                yield (), outputs[column], True, evidence
                continue
            case _:
                continue
        for bound, value in pairs:
            if not (isinstance(bound, ir.ColumnRef) and bound.column in padded.output_columns):
                continue
            match value:
                case ir.ColumnRef(column=other) if other in padded.output_columns:
                    if _pins(bound.column, value):
                        yield (outputs[other],), outputs[bound.column], True, evidence
                case ir.ColumnRef(column=other):
                    if _pins(bound.column, value):
                        yield status, outputs[bound.column], True, evidence
                    if _pins(other, bound):
                        yield (outputs[bound.column],), outputs[other], False, evidence
                case ir.Constant() if _pins(bound.column, value):
                    yield status, outputs[bound.column], True, evidence
                case ir.Constant():
                    pass
                case _:
                    pass  # An expression or NULL binds nothing.



def minimal_keys(relation: ir.RelationExpr, max_size: int = 3) -> list[tuple[ir.OutputColumn, ...]]:
    """The minimal Unique Sets of `relation`, in output order.

    Every one of up to `max_size` columns, smallest first; when none is that
    small, one minimal set found by shrinking all the columns.
    """
    columns = relation.output_columns

    def unique(members: tuple[ir.OutputColumn, ...]) -> bool:
        return RowIdentity(relation) in _closure(relation, frozenset(members), frozenset(members)).value.columns

    if not columns or not unique(columns):
        return []
    keys: list[tuple[ir.OutputColumn, ...]] = []
    for size in range(1, min(max_size, len(columns)) + 1):
        for members in combinations(columns, size):
            if not any(set(key) <= set(members) for key in keys) and unique(members):
                keys.append(members)
    if keys:
        return keys
    members = columns
    for column in columns:
        rest = tuple(other for other in members if other is not column)
        if rest and unique(rest):
            members = rest
    return [members]
