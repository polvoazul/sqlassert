"""Non-null columns: establish them from literals and knowledge, and follow
pass-through columns back to their source unless a join null-extends them."""

from sqlassert import ir
from sqlassert.python_engine import Result, all_of, analysis, any_of, current_arena, unknown


@analysis
def non_null_columns(columns: frozenset[ir.OutputColumn]) -> Result[bool]:
    relations = current_arena().relations_for(columns)
    if not relations:
        return unknown("Non-null columns must belong to one relation", *columns)
    # Every column is evaluated, so an unknown names each one that failed.
    ordered = [column for column in relations[0].output_columns if column in columns]
    return all_of(non_null_column(column) for column in ordered)


@analysis
def non_null_column(column: ir.OutputColumn) -> Result[bool]:
    relations = current_arena().relations_for(frozenset((column,)))
    if not relations:
        return unknown("Column belongs to no relation", column)
    return any_of(_non_null_output(relation, column) for relation in relations)


@analysis
def _non_null_output(relation: ir.RelationExpr, column: ir.OutputColumn) -> Result[bool]:
    match relation:
        case ir.Join():
            match column.scalar_expr:
                case ir.ColumnRef(column=source) if source in _null_extended(relation):
                    return unknown(f"A {relation.kind} join null-extends this column", relation, column)
                case _:
                    pass
        case ir.SetOperation():
            # Outputs refer to the left input only; the right one is not tracked.
            return unknown("Set operations do not preserve non-null columns", relation, column)
        case ir.OpaqueRelation():
            return unknown("Nullability of an opaque relation's columns is not known", relation, column)
        case ir.Alias() | ir.NamedRelation() | ir.Filter() | ir.Project() | ir.Aggregate() | ir.Distinct() | ir.QualifyByPartition():
            pass
        case _:
            raise TypeError(f"Unhandled relation: {type(relation).__name__}")
    return _non_null_expression(column.scalar_expr)


@analysis
def _non_null_expression(expression: ir.ScalarExpr) -> Result[bool]:
    match expression:
        case ir.ColumnRef(column=source):
            return Result(True, "Column passes through its source", (expression,)) & non_null_column(source)
        case ir.Constant():
            return Result(True, "A non-null literal", (expression,))
        case ir.NullLiteral():
            return unknown("A NULL literal is null", expression)
        case ir.AnyAggregate(input=value):
            # Groups are never empty, so the value comes from some input row.
            return Result(True, "A group's value comes from one of its rows", (expression,)) & _non_null_expression(value)
        case ir.OpaqueExpression(description=description):
            return unknown(f"Nullability of {description} is not known", expression)
        case ir.Equality():
            return unknown("Nullability of a comparison is not inferred", expression)
        case _:
            raise TypeError(f"Unhandled scalar expression: {type(expression).__name__}")


def _null_extended(join: ir.Join) -> frozenset[ir.OutputColumn]:
    match join.kind:
        case "inner" | "cross":
            return frozenset()
        case "left":
            return frozenset(join.right.output_columns)
        case "right":
            return frozenset(join.left.output_columns)
        case _:
            # FULL null-extends both sides; other kinds are not modelled.
            return frozenset(join.left.output_columns + join.right.output_columns)
