"""Hazard suggestions: results the SQL leaves unspecified, from the Kossmann et al. 2022 triage (issue 15).

A Hazard is not a rewrite: the SQL may be what its author meant. It names a
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
- A join equality between text and another declared type: the text is cast,
  so different strings may match one value (`'01'` and `'1'` both equal 1),
  and no cardinality is proved across it. Write the cast explicitly.
- A scalar subquery not proved to return at most one row (issue 38): more
  than one is a runtime error. Uncorrelated, its body must have at most one
  row; correlated, the columns its WHERE binds must reach its Row Identity.
- A query that can never return a row: a `NOT EXISTS` every row has a match
  for (a foreign key's orphan check), a `WHERE x IS NULL` on a column proved
  non-null (the `LEFT JOIN ... IS NULL` form of it), an `EXCEPT` whose left
  rows all appear on the right. Whether the key is enforced is not asked:
  sqlassert trusts every key the SQL states.

A hazard is reported only when sqlassert sees the whole Select Expression; an
`ORDER BY` key it cannot map to a selected column keeps it silent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from sqlglot import exp

from sqlassert import ir
from sqlassert.rewrites import SuggestType, _syntax_at_sites
from sqlassert.provenance import AttachmentSite
from sqlassert.python_engine import Arena, Result
from sqlassert.rules.equality import _binary_text, _comparison_type, _exact_arithmetic, _identity_comparison
from sqlassert.rules.join_cardinality import _at_most_one_match, many_to_one_join, one_to_many_join, unique_join
from sqlassert.properties import NonNullColumn, NonNullColumns, ReferentialCoverage
from sqlassert.rules.exactly_one import _cross_equalities
from sqlassert.rules.uniqueness import _closure, _reaches_row_identity, minimal_keys
from sqlassert.sql_parse import ParsedProgram, assertion_span


class Concern(Enum):
    ARBITRARY_LIMIT = "Arbitrary LIMIT"
    FAN_TRAP = "Fan trap"
    CHASM_TRAP = "Chasm trap"
    SCALAR_SUBQUERY = "Scalar subquery"
    IMPLICIT_CAST = "Implicit cast"
    ALWAYS_EMPTY = "Always empty"
    MANY_TO_MANY = "Many-to-many join"


@dataclass(frozen=True)
class Hazard:
    """A place whose result the SQL leaves unspecified, with the failed proof."""

    site: AttachmentSite
    concern: Concern
    message: str
    evidence: Result
    suggest_type: ClassVar[SuggestType] = SuggestType.HAZARD


def hazards(
    ast: ParsedProgram, program: ir.Program, arena: Arena, wanted: Callable[[AttachmentSite], bool] = lambda site: True,
) -> list[Hazard]:
    """Every Hazard in `program` at a site `wanted` accepts, in source order. `arena` must be active."""
    syntax = _syntax_at_sites(ast)
    reachable = set(program.nodes())
    found: list[Hazard] = []
    join_labels = {relation: site.label for site, relation in program.attachment_sites if isinstance(relation, ir.Join)}
    deduplicated = _deduplicated_joins(program)
    sort_keys = dict(program.sort_keys)
    for site, relation in program.attachment_sites:
        if not wanted(site):
            continue
        node = syntax.get((site.offset, site.trailing))
        if isinstance(relation, ir.Limit) and relation in reachable and isinstance(node, exp.Select):
            found.extend(_arbitrary_limit(site, relation, sort_keys.get(site)))
        if isinstance(relation, ir.Join) and relation in reachable and not site.trailing:
            found.extend(_implicit_casts(site, relation))
            if isinstance(node, exp.Join) and assertion_span(node) is None and relation not in deduplicated:
                found.extend(_many_to_many(site, relation))
        aggregate = relation.input if isinstance(relation, ir.Limit) else relation
        if isinstance(aggregate, ir.Aggregate) and aggregate in reachable:
            found.extend(_traps(site, aggregate, join_labels))
        if site.trailing and aggregate in reachable:
            found.extend(_always_empty(site, aggregate, arena))
    for site, operation in program.set_operations:
        if operation in reachable and wanted(site) and operation.operator == "except":
            found.extend(_empty_except(site, operation, arena))
    for site, join in program.scalar_subqueries:
        if join in reachable and wanted(site):
            found.extend(_scalar_subquery(site, join))
    return sorted(found, key=lambda item: item.site.offset)


def _always_empty(site: AttachmentSite, relation: ir.RelationExpr, arena: Arena) -> list[Hazard]:
    """A filter of this Select that keeps no row: an anti join every row
    matches, or `x IS NULL` on a non-null x. A scalar aggregate still returns
    its one row, so the walk stops there."""
    while True:
        match relation:
            case ir.Aggregate(grouping_outputs=()):
                return []
            case ir.Project(input=source) | ir.Aggregate(input=source) | ir.Distinct(input=source) | ir.QualifyByPartition(input=source) | ir.Join(kind="semi", left=source):
                relation = source
            case ir.Join(kind="anti", left=source):
                proof = _every_row_matches(relation, arena)
                if proof.value:
                    message = (
                        "every row has a match in its NOT EXISTS subquery, which therefore removes every row: this "
                        "query never returns one (an orphan check a foreign key already guarantees)"
                    )
                    return [Hazard(site, Concern.ALWAYS_EMPTY, message, proof)]
                relation = source
            case ir.Filter(input=source):
                for conjunct in relation.conjuncts:
                    match conjunct:
                        case ir.IsNull(operand=ir.ColumnRef(column=column)):
                            proof = arena.prove(NonNullColumn(column=column))
                            if proof.value:
                                message = f"{conjunct.origin.detail} is never TRUE ({column.name} is never NULL), so this query never returns a row"
                                return [Hazard(site, Concern.ALWAYS_EMPTY, message, proof)]
                        case _:
                            pass
                relation = source
            case _:
                return []


def _every_row_matches(join: ir.Join, arena: Arena) -> Result[bool]:
    """Every left row of `join` has a right row its ON holds on: ON is only
    equalities between the inputs, the left columns are non-null and covered
    by the right ones (the at-least-one half of an Exactly One Join)."""
    pairs, others = _cross_equalities(join)
    if not pairs or others:
        return Result(False, "Only an ON of equalities between the inputs is known to match", (join,), kind="unknown")
    lefts, rights = tuple(left for left, _ in pairs), tuple(right for _, right in pairs)
    return arena.prove(ReferentialCoverage(columns=lefts, referenced_columns=rights)) & arena.prove(NonNullColumns(columns=frozenset(lefts)))


def _empty_except(site: AttachmentSite, operation: ir.SetOperation, arena: Arena) -> list[Hazard]:
    """`A EXCEPT B` where every row of A, non-null, appears in B."""
    left, right = operation.left.output_columns, operation.right.output_columns
    if operation.by_name or len(left) != len(right):
        return []
    proof = arena.prove(ReferentialCoverage(columns=left, referenced_columns=right)) & arena.prove(NonNullColumns(columns=frozenset(left)))
    if not proof.value:
        return []
    message = "every row of the left input appears in the right one, so this EXCEPT never returns a row"
    return [Hazard(site, Concern.ALWAYS_EMPTY, message, proof)]


def _many_to_many(site: AttachmentSite, join: ir.Join) -> list[Hazard]:
    """A join with no marker that is not proved to repeat rows of at most one
    input, both of whose inputs have a known Unique Set: it may repeat rows of
    both. A many-to-many join is rare on purpose; an explicit CROSS JOIN is
    one, so it is left alone, and a marker leaves the cardinality to `check`."""
    if join.kind in ("semi", "anti", "cross"):
        return []
    proof = unique_join(join)
    if proof.value or not (_has_unique_set(join.left) and _has_unique_set(join.right)):
        return []
    if _equates_a_key(join, join.left, join.right) or _equates_a_key(join, join.right, join.left):
        return []  # A join on a key whose column types are unknown: a missing fact, not a hazard.
    message = (
        "this join is not proved to repeat rows of only one input: it may be many to many, repeating rows of both. "
        "Join on a key of one input, or write the join marker you expect (/**ASSERT MANY TO ONE**/, ...) for check to prove"
    )
    return [Hazard(site, Concern.MANY_TO_MANY, message, proof)]


def _deduplicated_joins(program: ir.Program) -> set[ir.Join]:
    """The joins in the FROM of a Select whose DISTINCT or GROUP BY merges
    the rows they repeat: repeats never reach its result, and what they do to
    an aggregate is a trap of its own."""
    found: set[ir.Join] = set()
    for site, relation in program.attachment_sites:
        stage = relation.input if isinstance(relation, ir.Limit) else relation
        if not site.trailing or not isinstance(stage, ir.Distinct | ir.Aggregate):
            continue
        pending = [stage.input]
        while pending:
            match pending.pop():
                case ir.Join(left=left, right=right) as join:
                    found.add(join)
                    pending += [left, right]
                case ir.Filter(input=source):
                    pending.append(source)
                case _:
                    pass
    return found


def _equates_a_key(join: ir.Join, side: ir.RelationExpr, other: ir.RelationExpr) -> bool:
    """Whether ON equates every column of a known key of `side` with a value
    of the other input's row (a column, an expression of its columns such as
    a FULL JOIN chain's `coalesce(a.k, b.k)`, or a constant), whatever their
    types: the join is on that key, and only an unknown type or an
    expression kept the proof from binding it."""
    equated: set[ir.OutputColumn] = set()
    for conjunct in join.conjuncts:
        if not isinstance(conjunct, ir.Equality):
            continue
        for mine, theirs in ((conjunct.left, conjunct.right), (conjunct.right, conjunct.left)):
            inputs = ir.determinants(theirs)
            if isinstance(mine, ir.ColumnRef) and mine.column in side.output_columns and inputs is not None and (
                set(inputs) <= set(other.output_columns)
            ):
                equated.add(mine.column)
    return any(set(key) <= equated for key in minimal_keys(side))


def _implicit_casts(site: AttachmentSite, join: ir.Join) -> list[Hazard]:
    """Each ON equality between a text column and a column of another known type."""
    found = []
    for conjunct in join.conjuncts:
        match conjunct:
            case ir.Equality(left=ir.ColumnRef(column=first), right=ir.ColumnRef(column=second)):
                types = (_comparison_type(first).value, _comparison_type(second).value)
                if None in types or _binary_text(first).value == _binary_text(second).value:
                    continue
                message = (
                    f"{first.name} ({types[0]}) = {second.name} ({types[1]}) compares text with another type: the text is cast, "
                    "so different strings may match one value ('01' and '1' both equal 1): cast explicitly"
                )
                evidence = Result(False, "The comparison casts text", (conjunct,))
                found.append(Hazard(site, Concern.IMPLICIT_CAST, message, evidence))
            case _:
                pass
    return found


def _scalar_subquery(site: AttachmentSite, join: ir.Join) -> list[Hazard]:
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
    return [Hazard(site, Concern.SCALAR_SUBQUERY, message, proof)]


# Join traps ------------------------------------------------------------------


def _traps(site: AttachmentSite, aggregate: ir.Aggregate, join_labels: dict[ir.Join, str]) -> list[Hazard]:
    """Each aggregate call whose input rows a join may repeat."""
    found: list[Hazard] = []
    for call in aggregate.calls:
        inputs = [ir.determinants(argument) for argument in call.arguments]
        if any(columns is None for columns in inputs):
            continue  # An argument whose columns are unknown.
        columns = frozenset(column for columns in inputs if columns is not None for column in columns)
        if not columns:
            # `count(*)`, or a constant: it counts the input's rows.
            for join, path, proof in _many_to_many_joins(aggregate.input, []):
                if _unrepeated(aggregate.input, join.left, path) or _unrepeated(aggregate.input, join.right, path):
                    continue
                message = (
                    f"{call.description} counts the rows of {_label(join, join_labels)}, which is not proved "
                    "to repeat rows of only one input (a fan trap): rows may be counted more than once"
                )
                found.append(Hazard(site, Concern.FAN_TRAP, message, proof))
            continue
        repeated = _repeating_join(aggregate.input, columns)
        if repeated is None:
            continue
        join, side, path, proof = repeated
        if _unrepeated(aggregate.input, side, path):
            continue  # A filter keeps one row per key after all (`WHERE o.id = 1`).
        if unique_join(join).value:
            concern, why = Concern.CHASM_TRAP, "is one to many, and the argument is on the side with one row per key"
        else:
            concern, why = Concern.FAN_TRAP, "is not proved to repeat rows of only one input"
        message = (
            f"{call.description} reads rows that {_label(join, join_labels)} may repeat: the join {why} "
            f"({concern.value.lower()}), so a value may be counted more than once"
        )
        found.append(Hazard(site, concern, message, proof))
    return found


def _repeating_join(
    relation: ir.RelationExpr, columns: frozenset[ir.OutputColumn]
) -> tuple[ir.Join, ir.RelationExpr, list[ir.RelationExpr], Result] | None:
    """The first join, from `relation` down to the input `columns` come from,
    not proved to keep that input's rows unrepeated: the join, that input,
    the stages from `relation` down to the join, and the failed proof. None
    when every join on the way is proved, or the columns cannot be traced to
    one input of a join."""
    path: list[ir.RelationExpr] = []
    while True:
        path.append(relation)
        upstream = _upstream(relation, columns)
        if upstream is None:
            return None
        match relation:
            case ir.Join(kind="semi" | "anti"):
                side = relation.left  # It never repeats a row.
            case ir.Join():
                if upstream <= set(relation.left.output_columns):
                    side, proof = relation.left, many_to_one_join(relation)
                elif upstream <= set(relation.right.output_columns):
                    side, proof = relation.right, one_to_many_join(relation)
                else:
                    return None  # The argument reads both inputs.
                if not proof.value and _has_unique_set(side):
                    return relation, side, path, proof
            case ir.Filter(input=side) | ir.Alias(source=side) | ir.Project(input=side) | ir.Limit(input=side):
                pass
            case ir.NamedRelation(body=ir.RelationExpr() as side):
                pass
            case _:
                return None  # A base table, or a stage that drops or merges rows.
        if not upstream <= set(side.output_columns):
            return None
        relation, columns = side, upstream


def _many_to_many_joins(relation: ir.RelationExpr, path: list[ir.RelationExpr]):
    """Each join under `relation` not proved to repeat rows of at most one
    input, both of whose inputs have a known Unique Set, with the stages down
    to it and the failed proof."""
    path = [*path, relation]
    match relation:
        case ir.Join(kind="semi" | "anti", left=left):
            yield from _many_to_many_joins(left, path)
        case ir.Join(left=left, right=right):
            proof = unique_join(relation)
            if not proof.value and _has_unique_set(left) and _has_unique_set(right):
                yield relation, path, proof
            yield from _many_to_many_joins(left, path)
            yield from _many_to_many_joins(right, path)
        case ir.Filter(input=source) | ir.Alias(source=source) | ir.Project(input=source) | ir.Limit(input=source):
            yield from _many_to_many_joins(source, path)
        case ir.NamedRelation(body=ir.RelationExpr() as source):
            yield from _many_to_many_joins(source, path)
        case _:
            pass


def _unrepeated(top: ir.RelationExpr, side: ir.RelationExpr, path: list[ir.RelationExpr]) -> bool:
    """Whether a Unique Set of `side`, carried up `path` (the stages from
    `top` down to the join `side` is an input of), is a Unique Set of `top`:
    `side`'s rows are not repeated there after all."""
    for key in minimal_keys(side):
        lifted: list[ir.OutputColumn] = list(key)
        for stage in reversed(path):
            passed = {
                output.scalar_expr.column: output for output in stage.output_columns if isinstance(output.scalar_expr, ir.ColumnRef)
            }
            if not all(column in passed for column in lifted):
                break
            lifted = [passed[column] for column in lifted]
        else:
            members = frozenset(lifted)
            if _reaches_row_identity(_closure(top, members, members)).value:
                return True
    return False


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


# Arbitrary LIMIT -------------------------------------------------------------


def _arbitrary_limit(site: AttachmentSite, limit: ir.Limit, keys: tuple[ir.OutputColumn, ...] | None) -> list[Hazard]:
    """A LIMIT whose sort keys leave two different rows tied.

    Rows that agree on every selected column are interchangeable, so the keys
    need only strictly determine the selected columns (NULL equal to NULL, as
    ORDER BY ties them), not a row of the input. Under a projection the keys
    are input columns it may not select, so the question is asked there. A
    key that does not compare by identity (a collation, a float) ties
    different values, so it determines nothing here.
    """
    if limit.count == 0 or keys is None:
        return []
    relation = limit.input
    if isinstance(relation, ir.Project):
        scope, needed = relation.input, {column: _needed(column) for column in relation.output_columns}
    else:
        scope, needed = relation, {column: frozenset({column}) for column in relation.output_columns}
    exact = tuple(key for key in keys if _ties_identical(key))
    closure = _closure(scope, frozenset(exact), frozenset())
    loose = [column for column, inputs in needed.items() if inputs is None or not inputs <= closure.value.columns]
    if not loose:
        return []
    names = ", ".join(column.name for column in loose)
    reason = f"the ORDER BY does not determine ({names}), so rows tied on it" if keys else "with no ORDER BY, rows"
    message = f"{reason} come in an arbitrary order and LIMIT may keep different rows on each run: order by a key as well"
    return [Hazard(site, Concern.ARBITRARY_LIMIT, message, closure)]


def _ties_identical(column: ir.OutputColumn) -> bool:
    """Whether rows ORDER BY ties on `column` hold identical values of it."""
    return _identity_comparison(column).value or _exact_arithmetic(column)


def _needed(column: ir.OutputColumn) -> frozenset[ir.OutputColumn] | None:
    """The input columns a projected column's value is a function of, if known."""
    inputs = ir.determinants(column.scalar_expr)
    return None if inputs is None else frozenset(inputs)
