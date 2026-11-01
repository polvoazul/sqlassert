"""Null sources: every reason an output column may be NULL.

A column is NULL organically (a nullable source column, an opaque expression,
a NULL literal) or because an outer join padded its row. Padding is tracked per
join, so a null-rejecting conjunct on one padded column proves that join
matched and removes its padding from every column it padded; organic NULLs are
removed one column at a time. A column is non-null when no source remains.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, all_of, analysis, any_of, current_arena, unknown


@dataclass(frozen=True)
class Organic:
    """The value itself may be NULL."""


@dataclass(frozen=True)
class Padding:
    """`join` pads the column: it is NULL on every row the join null-extends.

    `aliases` are the Aliases the column was seen through, innermost last, so
    two occurrences of one relation never share a padding.
    """

    join: ir.Join
    aliases: tuple[ir.Alias, ...] = ()


type NullSource = Organic | Padding

ORGANIC = Organic()

# A column's null sources, each with the evidence that it is present.
type _Sources = dict[NullSource, Result[bool]]


@analysis(Stratum.NON_NULL)
def non_null_columns(columns: frozenset[ir.OutputColumn]) -> Result[bool]:
    relation = current_arena().program.relation_of(columns)
    if relation is None:
        return unknown("Non-null columns must belong to one relation", *columns)
    # Every column is evaluated, so an unknown names each one that failed.
    ordered = [column for column in relation.output_columns if column in columns]
    return all_of(non_null_column(column) for column in ordered)


@analysis(Stratum.NON_NULL)
def non_null_column(column: ir.OutputColumn) -> Result[bool]:
    sources = _null_sources(column)
    if sources.value:
        return Result(False, f"{column.name} may be NULL", (column,), list(sources.members.values()), kind="unknown")
    return Result(True, "No NULL source remains", (column,), [sources])


@analysis(Stratum.NON_NULL)
def _null_sources(column: ir.OutputColumn) -> Result[frozenset[NullSource]]:
    arena = current_arena()
    accepted = arena.accepted.get(("non_null_column", (("column", column),)), ())
    if accepted:
        return Result(frozenset(), "Known non-null", (column,), [any_of(accepted)])
    relation = column.get_relation(arena.program)
    sources, premises = _output_sources(relation, column)
    return Result(frozenset(sources), "Null sources", (column,), premises, members=sources)


def _output_sources(relation: ir.RelationExpr, column: ir.OutputColumn) -> tuple[_Sources, list[Result]]:
    match relation:
        case ir.NamedRelation(body=None):
            return {ORGANIC: unknown(f"{column.name} is nullable in {relation.name}", relation, column)}, []
        case ir.OpaqueRelation():
            return {ORGANIC: unknown("Nullability of an opaque relation's columns is not known", relation, column)}, []
        case ir.SetOperation():
            # Outputs refer to the left input only; the right one is not tracked.
            return {ORGANIC: unknown("Set operations do not preserve non-null columns", relation, column)}, []
        case ir.Join():
            return _join_sources(relation, column)
        case ir.Filter():
            return _filter_sources(relation, column)
        case ir.Alias():
            sources, premises = _expression_sources(column.scalar_expr)
            return {_seen_through(relation, source): evidence for source, evidence in sources.items()}, premises
        case ir.Aggregate():
            sources, premises = _expression_sources(column.scalar_expr)
            if column in relation.grouping_outputs:
                return sources, premises
            # A group's value comes from one of its rows: padding of that row is
            # no longer tied to the rows of the join that padded it.
            return {(ORGANIC if isinstance(source, Padding) else source): evidence for source, evidence in sources.items()}, premises
        case ir.NamedRelation() | ir.Project() | ir.Distinct() | ir.QualifyByPartition():
            return _expression_sources(column.scalar_expr)
        case _:
            raise TypeError(f"Unhandled relation: {type(relation).__name__}")


def _expression_sources(expression: ir.ScalarExpr) -> tuple[_Sources, list[Result]]:
    match expression:
        case ir.ColumnRef(column=source):
            sources = _null_sources(source)
            return dict(sources.members), [Result(True, "Column passes through its source", (expression,), [sources])]
        case ir.Constant():
            return {}, [Result(True, "A non-null literal", (expression,))]
        case ir.NullLiteral():
            return {ORGANIC: unknown("A NULL literal is null", expression)}, []
        case ir.Coalesce(arguments=arguments):
            # Null only when every argument is, so one non-null argument suffices.
            arguments_sources = [_expression_sources(argument) for argument in arguments]
            for sources, premises in arguments_sources:
                if not sources:
                    return {}, [Result(True, "Coalesce is non-null when any argument is", (expression,), premises)]
            # NULL on a join's padded rows only if every argument is.
            common = set.intersection(*({s for s in sources if isinstance(s, Padding)} for sources, _ in arguments_sources))
            first, _ = arguments_sources[0]
            result: _Sources = {source: first[source] for source in common}
            if any(set(sources) - common for sources, _ in arguments_sources):
                evidence = [evidence for sources, _ in arguments_sources for evidence in sources.values()]
                result[ORGANIC] = Result(False, "Every coalesce argument may be NULL", (expression,), evidence, kind="unknown")
            return result, []
        case ir.AnyAggregate(input=value):
            # Groups are never empty, so the value comes from some input row.
            return _expression_sources(value)
        case ir.OpaqueExpression(description=description):
            return {ORGANIC: unknown(f"Nullability of {description} is not known", expression)}, []
        case ir.Equality() | ir.IsNull() | ir.IsNotNull():
            return {ORGANIC: unknown("Nullability of a comparison is not inferred", expression)}, []
        case ir.SimpleCase():
            return {ORGANIC: unknown("Nullability of a CASE is not inferred", expression)}, []
        case _:
            raise TypeError(f"Unhandled scalar expression: {type(expression).__name__}")


def _join_sources(join: ir.Join, column: ir.OutputColumn) -> tuple[_Sources, list[Result]]:
    sources, premises = _expression_sources(column.scalar_expr)
    match column.scalar_expr:
        case ir.ColumnRef(column=source):
            pass
        case _:
            raise TypeError(f"Join column {column.name} is not a pass-through")
    if join.kind in {"semi", "anti"}:
        return {ORGANIC: unknown(f"Nullability through a {join.kind} join is not modelled", join, column)}, []
    sources, removal = _rejected(_on_rejected(join), source, sources, "The ON condition rejects NULL on matched rows")
    left = source in join.left.output_columns
    if left in _padded_sides(join):
        sources[Padding(join)] = unknown(f"{column.name} is padded by the {join.kind.upper()} JOIN", join, column)
    return sources, premises + removal


def _filter_sources(relation: ir.Filter, column: ir.OutputColumn) -> tuple[_Sources, list[Result]]:
    sources, premises = _expression_sources(column.scalar_expr)
    match column.scalar_expr:
        case ir.ColumnRef(column=source):
            pass
        case _:
            raise TypeError(f"Filter column {column.name} is not a pass-through")
    sources, removal = _rejected(_null_rejected(relation.conjuncts), source, sources, "The WHERE clause rejects NULL")
    return sources, premises + removal


def _rejected(
    rejected: dict[ir.OutputColumn, ir.ScalarExpr], source: ir.OutputColumn, sources: _Sources, reason: str
) -> tuple[_Sources, list[Result]]:
    """`sources` once the rows on which a rejected column is NULL are gone.

    A rejected column loses its organic NULLs, and every join that padded a
    rejected column is proved matched, so its padding goes from every column.
    """
    sources = dict(sources)
    removal = []
    if source in rejected and ORGANIC in sources:
        del sources[ORGANIC]
        removal.append(Result(True, reason, (rejected[source], source)))
    for column, conjunct in rejected.items():
        for padding in _null_sources(column).value:
            if isinstance(padding, Padding) and padding in sources:
                del sources[padding]
                removal.append(Result(True, f"{reason}, so the padding join matched", (conjunct, column)))
    return sources, removal


def _null_rejected(conjuncts: tuple[ir.ScalarExpr, ...]) -> dict[ir.OutputColumn, ir.ScalarExpr]:
    """Each column a conjunct requires to be non-null, with that conjunct."""
    rejected: dict[ir.OutputColumn, ir.ScalarExpr] = {}
    for conjunct in conjuncts:
        match conjunct:
            case ir.Equality(left=left, right=right):
                operands: tuple[ir.ScalarExpr, ...] = (left, right)
            case ir.IsNotNull(operand=operand) | ir.InList(operand=operand):
                operands = (operand,)
            case _:
                operands = ()  # IS NULL and opaque conjuncts may hold on NULL.
        for operand in operands:
            match operand:
                case ir.ColumnRef(column=column):
                    rejected.setdefault(column, conjunct)
                case _:
                    pass  # Only a column operand is a column the conjunct rejects.
    return rejected


def _on_rejected(join: ir.Join) -> dict[ir.OutputColumn, ir.ScalarExpr]:
    """Input columns non-null on every matched row of `join`, on its padded side.

    An inner join drops rows failing ON on both sides; an outer join keeps them
    padded, so ON only constrains the side it pads.
    """
    rejected = _null_rejected(join.conjuncts)
    match join.kind:
        case "inner":
            return rejected
        case "left":
            return {column: c for column, c in rejected.items() if column in join.right.output_columns}
        case "right":
            return {column: c for column, c in rejected.items() if column in join.left.output_columns}
        case _:
            return {}


def _padded_sides(join: ir.Join) -> frozenset[bool]:
    """The sides `join` null-extends: True for the left input, False for the right.

    A proved Exactly One LEFT join matches every left row, so it pads nothing.
    """
    from sqlassert.rules.exactly_one import exactly_one_join

    match join.kind:
        case "left":
            return frozenset() if exactly_one_join(join).value else frozenset({False})
        case "right":
            return frozenset({True})
        case "full":
            return frozenset({True, False})
        case _:
            return frozenset()


def _seen_through(alias: ir.Alias, source: NullSource) -> NullSource:
    match source:
        case Padding(join=join, aliases=aliases):
            return Padding(join, (*aliases, alias))
        case Organic():
            return source
