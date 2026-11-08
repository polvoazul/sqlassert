"""Join cardinality: whether a join can repeat rows of its left input, its right input, or both.

A row of one input appears once per matching row of the other, or once padded,
whatever the join kind. Matching rows satisfy ON, so they agree on every
column an ON equality binds to the first row's column or to a constant, all
non-null. When those columns reach the matched input's Row Identity, a row
matches at most one row and is never repeated. A cross join is an ON with no
conjuncts: the other input must have at most one row. A semi or anti join
keeps or drops left rows whole and outputs no right row; its left rows are
never repeated (`many_to_one_join`), and no cardinality marker applies to it.
"""

from typing import Literal, assert_never

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, all_of, any_of, analysis
from sqlassert.rules.column_set import ColumnSet
from sqlassert.rules.equality import _pins
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
    reached = _reaches_row_identity(_closure(relation, bound, bound))
    reason = "Right columns bound for each left row" if matched == "right" else "Left columns bound for each right row"
    return all_of((Result(True, reason, (join,), list(cols.members.values())), reached))


@analysis(Stratum.UNIQUE_JOIN)
def _bound_columns(join: ir.Join, matched: Side) -> Result[ColumnSet]:
    """The columns of `matched` an ON equality binds to the other input's column or to a constant."""
    relation, other = (join.right, join.left) if matched == "right" else (join.left, join.right)
    members = {}
    for equality in join.conjuncts:
        if not isinstance(equality, ir.Equality):
            continue
        for bound, value in ((equality.left, equality.right), (equality.right, equality.left)):
            if bound is value:
                continue
            bound, value = bound.closed, value.closed
            match bound:
                case ir.ColumnRef(column=column):
                    if column not in relation.output_columns:
                        continue
                case ir.Constant() | ir.NullLiteral() | ir.Coalesce() | ir.SimpleCase() | ir.OpaqueExpression() | ir.OpaquePureFunction() | ir.Call() | ir.Count() | ir.AnyAggregate() | ir.Elements() | ir.Equality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or():
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
                case ir.NullLiteral() | ir.Coalesce() | ir.SimpleCase() | ir.OpaqueExpression() | ir.OpaquePureFunction() | ir.Call() | ir.Count() | ir.AnyAggregate() | ir.Elements() | ir.Equality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or():
                    continue
                case _:
                    assert_never(value)

            evidence = Result(True, "Equality fixes the matched column for each row of the other input", (equality, column, value))
            members[column] = members[column] | evidence if column in members else evidence

    return Result(ColumnSet(relation, set(members)), "Bound columns", members=members)
