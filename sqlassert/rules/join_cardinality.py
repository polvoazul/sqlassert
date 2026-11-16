"""Join cardinality: whether a join can repeat rows of its left input, its right input, or both.

A row of one input appears once per matching row of the other, or once padded,
whatever the join kind. Matching rows satisfy ON, so they agree on every
column an ON equality binds to the first row's column or to a constant, all
non-null. IS NOT DISTINCT FROM binds them too, but matches NULL to NULL, so
those columns are non-null only where proved, or where the other side is a
literal or a non-null column, which no NULL equals. When those columns reach the
matched input's Row Identity, a row matches at most one row and is never
repeated. A cross join is an ON with no
conjuncts: the other input must have at most one row. A semi or anti join
keeps or drops left rows whole and outputs no right row; its left rows are
never repeated (`many_to_one_join`), and no cardinality marker applies to it.
"""

from dataclasses import dataclass, field
from typing import Literal, assert_never

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, all_of, any_of, analysis, cache, current_arena
from sqlassert.rules.column_set import Attribute, ColumnSet
from sqlassert.rules.equality import _comparison_type, _identity_comparison, _pins
from sqlassert.rules.nullability import non_null_column
from sqlassert.rules.uniqueness import _closure, _reaches_row_identity

type Side = Literal["left", "right"]


@analysis(Stratum.UNIQUE_JOIN)
def many_to_one_join(join: ir.Join) -> Result[bool]:
    if join.scalar_subquery:
        return Result(True, "A scalar subquery returns at most one row for each outer row, or the query fails", (join,))
    return _at_most_one_match(join, "right")


@analysis(Stratum.UNIQUE_JOIN)
def one_to_many_join(join: ir.Join) -> Result[bool]:
    return _at_most_one_match(join, "left")


@analysis(Stratum.UNIQUE_JOIN)
def one_to_one_join(join: ir.Join) -> Result[bool]:
    return all_of((many_to_one_join(join), one_to_many_join(join)))


@analysis(Stratum.UNIQUE_JOIN)
def unique_join(join: ir.Join) -> Result[bool]:
    # Not many to many: the proved alternative names the input that is unique.
    return any_of((many_to_one_join(join), one_to_many_join(join)))


def _at_most_one_match(join: ir.Join, matched: Side) -> Result[bool]:
    """Each row of the other input matches at most one row of `matched`."""
    relation = join.right if matched == "right" else join.left
    cols = _bound_columns(join, matched)
    bound = frozenset(cols.value.columns)
    reached = _reaches_row_identity(_closure(relation, bound, frozenset(cols.value.definite)))
    reason = "Right columns bound for each left row" if matched == "right" else "Left columns bound for each right row"
    return all_of((Result(True, reason, (join,), list(cols.members.values())), reached))


@dataclass(eq=False)
class BoundColumns(ColumnSet):
    """`columns` bound for each row of the other input; `definite` those of
    them non-null on every matched row: bound by `=`, not only by IS NOT
    DISTINCT FROM, which also matches NULL to NULL."""

    definite: set[Attribute] = field(default_factory=set)


@analysis(Stratum.UNIQUE_JOIN)
def _bound_columns(join: ir.Join, matched: Side) -> Result[BoundColumns]:
    """The columns of `matched` an ON equality (or IS NOT DISTINCT FROM)
    binds to the other input's column or to a constant."""
    relation, other = (join.right, join.left) if matched == "right" else (join.left, join.right)
    members = {}
    definite = set()
    for equality in join.conjuncts:
        if not isinstance(equality, ir.Equality | ir.NullSafeEquality):
            continue
        for bound, value in ((equality.left, equality.right), (equality.right, equality.left)):
            if bound is value:
                continue
            bound, value = bound.closed, value.closed
            match bound:
                case ir.ColumnRef(column=column):
                    if column not in relation.output_columns:
                        continue
                case ir.Constant() | ir.NullLiteral() | ir.Coalesce() | ir.SimpleCase() | ir.OpaqueExpression() | ir.OpaquePureFunction() | ir.Call() | ir.Count() | ir.AnyAggregate() | ir.Accumulation() | ir.Elements() | ir.Equality() | ir.NullSafeEquality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or():
                    continue
                case _:
                    assert_never(bound)

            # An equality that casts pins nothing: '01' and '1' both equal 1.
            match value:
                case ir.Constant():
                    if not _pins(column, value):
                        continue
                case ir.ColumnRef(column=other_column):
                    if other_column not in other.output_columns or not _pins(column, value):
                        continue
                case ir.NullLiteral() | ir.Coalesce() | ir.SimpleCase() | ir.OpaqueExpression() | ir.OpaquePureFunction() | ir.Call() | ir.Count() | ir.AnyAggregate() | ir.Accumulation() | ir.Elements() | ir.Equality() | ir.NullSafeEquality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or():
                    continue
                case _:
                    assert_never(value)

            evidence = Result(True, "Equality fixes the matched column for each row of the other input", (equality, column, value))
            members[column] = members[column] | evidence if column in members else evidence
            # IS NOT DISTINCT FROM a literal or a non-null column is `=`.
            if isinstance(equality, ir.Equality) or isinstance(value, ir.Constant):
                definite.add(column)
            elif non_null_column(value.column).value:
                definite.add(column)
                members[column] = members[column] & non_null_column(value.column)

    return Result(BoundColumns(relation, set(members), definite), "Bound columns", members=members)


# Why a join has no proved Join Cardinality --------------------------------

type UnprovedReason = Literal["unsupported", "unconstrained", "unresolved-key", "untyped-key", "expression-key", "no-unique-set"]
type TypeLoss = Literal["values", "computed", "opaque-relation", "set-operation", "table", "type-mismatch"]
type _Operand = Literal["left", "right", "constant", "opaque", "both"]


@dataclass(frozen=True)
class UnprovedJoin:
    """Why neither `many_to_one_join` nor `one_to_many_join` is proved for a
    join: the first reason that applies, in the order of `UnprovedReason`.

    - `unsupported`: an input is a source sqlassert does not model (a table
      function, UNNEST, LATERAL), or the join is not reachable from the
      program: it sits in a SELECT that lowered opaque.
    - `unconstrained`: no ON equality relates one input to the other or to a
      constant (a cross join, a range join). No Unique Set can make it a
      Unique Join; whether rows repeat depends on the data, not on a key.
    - `unresolved-key`: an operand of such an equality lowered opaque: a
      column that does not resolve in its input, or an expression sqlassert
      does not model.
    - `untyped-key`: a column = column equality binds nothing, since the two
      columns do not compare without a lossy coercion; `detail` names where
      the type is lost (`TypeLoss`).
    - `expression-key`: every such equality compares an expression, not a
      column of one input.
    - `no-unique-set`: the equalities bind columns that reach the Row
      Identity of neither input.

    `construct` names the construct sqlassert does not support, when one
    is behind the reason: the nearest one at or under the input, or the
    relation, the reason points at.

    A diagnostic about a missing proof: it never changes what is proved."""

    reason: UnprovedReason
    detail: TypeLoss | None = None
    construct: str | None = None


def unproved_join(join: ir.Join) -> UnprovedJoin | None:
    """Why `join` has no proved Join Cardinality; None when it has one or is
    a semi or anti join, which has none. Runs inside an analysis session."""
    if join.kind in {"semi", "anti"}:
        return None
    if join not in _reachable():
        return UnprovedJoin("unsupported")
    if many_to_one_join(join).value or one_to_many_join(join).value:
        return None
    for relation in (join.left, join.right):
        source = relation
        while isinstance(source, ir.Alias):
            source = source.source
        if isinstance(source, ir.OpaqueRelation) and (construct := _unsupported_under(source)) is not None:
            return UnprovedJoin("unsupported", construct=construct)
    pairs = []
    for equality in join.conjuncts:
        if not isinstance(equality, ir.Equality | ir.NullSafeEquality):
            continue
        sides = (_operand(join, equality.left), _operand(join, equality.right))
        if "both" not in sides and (sides[0] != sides[1] or sides[0] == "opaque"):
            pairs.append((equality.left.closed, equality.right.closed, sides))
    if not pairs:
        return UnprovedJoin("unconstrained")
    if any("opaque" in sides for _, _, sides in pairs):
        return UnprovedJoin("unresolved-key", construct=_unsupported_under(join.left) or _unsupported_under(join.right))
    for first, second, _ in pairs:
        if isinstance(first, ir.ColumnRef) and isinstance(second, ir.ColumnRef) and not (
            _pins(first.column, second) and _pins(second.column, first)
        ):
            for column in (first.column, second.column):
                if (loss := _type_loss(column)) is not None:
                    return UnprovedJoin("untyped-key", loss[0], _unsupported_under(loss[1]) if loss[0] == "opaque-relation" else None)
            return UnprovedJoin("untyped-key", "type-mismatch")
    if all(not isinstance(first, ir.ColumnRef | ir.Constant) or not isinstance(second, ir.ColumnRef | ir.Constant) for first, second, _ in pairs):
        return UnprovedJoin("expression-key")
    return UnprovedJoin("no-unique-set")


@cache
def _reachable() -> frozenset[ir.Node]:
    return frozenset(current_arena().program.nodes())


def _unsupported_under(relation: ir.RelationExpr) -> str | None:
    """The construct sqlassert does not support nearest to `relation`: its
    own, else the first found reading down through its inputs."""
    unsupported = {id(node): construct for node, construct in current_arena().program.unsupported}
    pending, seen = [relation], set()
    while pending and unsupported:
        node = pending.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        if id(node) in unsupported:
            return unsupported[id(node)]
        pending.extend(child for child in ir.children(node) if isinstance(child, ir.RelationExpr))
    return None


def _operand(join: ir.Join, expression: ir.ScalarExpr) -> _Operand:
    """Which input an equality operand reads: `opaque` when nothing is known
    of it, `constant` when it reads neither (an outer column is fixed for
    the join too)."""
    sides = set()
    pending = [expression]
    while pending:
        match node := pending.pop():
            case ir.OpaqueExpression():
                return "opaque"
            case ir.ColumnRef(column=column):
                if column in join.left.output_columns:
                    sides.add("left")
                elif column in join.right.output_columns:
                    sides.add("right")
            case _:
                pending.extend(child for child in ir.children(node) if isinstance(child, ir.ScalarExpr))
    match sorted(sides):
        case []:
            return "constant"
        case ["left"]:
            return "left"
        case ["right"]:
            return "right"
        case _:
            return "both"


def _type_loss(column: ir.OutputColumn) -> tuple[TypeLoss, ir.RelationExpr] | None:
    """Where `column` loses the type equality needs to bind it, following
    plain column references (and set operation arms) down to the first
    column that is not one: `table` for a table column with no declared
    type (no CREATE, or none in the catalog), else the kind of relation or
    expression that computes it without a type compared by identity. None
    when that column keeps its type. With the relation that column is of."""
    relation = column.get_relation(current_arena().program)
    if isinstance(relation, ir.SetOperation):
        arms = relation.arms(column)
        if arms is None:
            return "set-operation", relation
        return _type_loss(arms[0]) or _type_loss(arms[1])
    match column.scalar_expr:
        case ir.ColumnRef(column=source) | ir.AnyAggregate(input=ir.ColumnRef(column=source)):
            return _type_loss(source)
    typed = _comparison_type(column).value is not None
    if isinstance(relation, ir.NamedRelation) and relation.role is ir.RelationRole.TABLE:
        # A declared type that does not compare by identity is the type, not a loss.
        return None if typed else ("table", relation)
    if typed and _identity_comparison(column).value:
        return None
    if isinstance(relation, ir.ValuesList):
        return "values", relation
    if isinstance(relation, ir.OpaqueRelation) or isinstance(column.scalar_expr, ir.OpaqueExpression):
        return "opaque-relation", relation
    return "computed", relation
