"""Intermediate values used by column-set property functions."""

from dataclasses import dataclass

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, analysis


@dataclass(eq=False)
class ColumnSet:
    relation: ir.RelationExpr
    columns: set[ir.OutputColumn]


@analysis(Stratum.COLUMN_SET)
def _column_set(relation: ir.RelationExpr, columns: frozenset[ir.OutputColumn]) -> Result[ColumnSet]:
    members = {column: Result(True, "Column belongs to the requested set", (column,)) for column in columns}
    return Result(ColumnSet(relation, set(columns)), "Requested column set", members=members)
