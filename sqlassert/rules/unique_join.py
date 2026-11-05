"""Unique joins: the right columns an ON equality binds determine the right row."""

from typing import assert_never

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, all_of, analysis
from sqlassert.rules.column_set import ColumnSet
from sqlassert.rules.uniqueness import _closure, _reaches_row_identity


@analysis(Stratum.UNIQUE_JOIN)
def unique_join(join: ir.Join) -> Result[bool]:
    # Whatever the kind, a left row appears at most once per matching right
    # row (or once, padded, or kept or dropped whole by a semi or anti join);
    # a cross join is an ON with no conjuncts.
    cols = _multiplicity_columns_rhs(join)
    bound = frozenset(cols.value.columns)
    # Matching right rows agree on the bound columns, all non-null.
    reached = _reaches_row_identity(_closure(join.right, bound, bound))
    return all_of((Result(True, "Right columns bound for each left row", (join,), list(cols.members.values())), reached))


@analysis(Stratum.UNIQUE_JOIN)
def _multiplicity_columns_rhs(join: ir.Join) -> Result[ColumnSet]:
    members = {}
    for equality in join.conjuncts:
        if not isinstance(equality, ir.Equality):
            continue
        for rhs, value in ((equality.left, equality.right), (equality.right, equality.left)):
            if rhs is value:
                continue
            rhs, value = ir.closed_scalar(rhs), ir.closed_scalar(value)
            match rhs:
                case ir.ColumnRef(column=column):
                    if column not in join.right.output_columns:
                        continue
                case ir.Constant() | ir.NullLiteral() | ir.Coalesce() | ir.SimpleCase() | ir.OpaqueExpression() | ir.OpaquePureFunction() | ir.Call() | ir.Count() | ir.AnyAggregate() | ir.Equality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or():
                    continue
                case _:
                    assert_never(rhs)

            match value:
                case ir.Constant():
                    pass
                case ir.ColumnRef(column=left_column):
                    if left_column not in join.left.output_columns:
                        continue
                case ir.NullLiteral() | ir.Coalesce() | ir.SimpleCase() | ir.OpaqueExpression() | ir.OpaquePureFunction() | ir.Call() | ir.Count() | ir.AnyAggregate() | ir.Equality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or():
                    continue
                case _:
                    assert_never(value)

            evidence = Result(True, "Equality fixes RHS column for each left row", (equality, column, value))
            members[column] = members[column] | evidence if column in members else evidence

    return Result(ColumnSet(join.right, set(members)), "RHS multiplicity columns", members=members)
