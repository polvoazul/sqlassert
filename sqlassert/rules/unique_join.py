"""Unique joins: equality-bound RHS columns cover a known unique set."""

from sqlassert import ir
from sqlassert.python_engine import Result, analysis, unknown
from sqlassert.rules.column_set import ColumnSet
from sqlassert.rules.uniqueness import _covers_unique_set


@analysis
def unique_join(join: ir.Join) -> Result[bool]:
    if join.kind not in {"inner", "left"}:
        return unknown("Unique-join inference supports inner and left joins", join)

    cols = _multiplicity_columns_rhs(join)
    return _covers_unique_set(cols)


@analysis
def _multiplicity_columns_rhs(join: ir.Join) -> Result[ColumnSet]:
    members = {}
    for equality in join.equalities:
        for rhs, value in ((equality.left, equality.right), (equality.right, equality.left)):
            if rhs is value:
                continue
            match rhs:
                case ir.ColumnRef(column=column):
                    if column not in join.right.output_columns:
                        continue
                case ir.Constant() | ir.NullLiteral() | ir.OpaqueExpression() | ir.AnyAggregate() | ir.Equality():
                    continue
                case _:
                    raise TypeError(f"Unhandled scalar expression: {type(rhs).__name__}")

            match value:
                case ir.Constant():
                    pass
                case ir.ColumnRef(column=left_column):
                    if left_column not in join.left.output_columns:
                        continue
                case ir.NullLiteral() | ir.OpaqueExpression() | ir.AnyAggregate() | ir.Equality():
                    continue
                case _:
                    raise TypeError(f"Unhandled scalar expression: {type(value).__name__}")

            evidence = Result(True, "Equality fixes RHS column for each left row", (equality, column, value))
            members[column] = members[column] | evidence if column in members else evidence

    return Result(ColumnSet(join.right, set(members)), "RHS multiplicity columns", members=members)
