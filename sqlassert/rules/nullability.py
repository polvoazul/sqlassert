"""Null sources: every reason an output column may be NULL.

A column is NULL organically (a nullable source column, an opaque expression,
a NULL literal) or because an outer join padded its row. Padding is tracked per
join and side, so a null-rejecting conjunct on one padded column proves that
side matched and removes its padding from every column it padded; organic
NULLs are removed one column at a time. A column is non-null when no source
remains. A padding source means NULL on *every* padded row: an expression that
may be non-null on some (a CASE arm, a non-strict argument) turns it organic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from sqlassert import ir
from sqlassert.properties import NonNullElements
from sqlassert.python_engine import Result, Stratum, all_of, analysis, any_of, current_arena, unknown


@dataclass(frozen=True)
class Organic:
    """The value itself may be NULL."""


@dataclass(frozen=True)
class Padding:
    """`join` pads the column: it is NULL on every row the join null-extends
    on its `left` (or right) side. A FULL join pads each side on different rows.

    `aliases` are the Aliases the column was seen through, innermost last, so
    two occurrences of one relation never share a padding.
    """

    join: ir.Join
    left: bool
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
    relation = relation.closed
    match relation:
        case ir.NamedRelation(body=None):
            return {ORGANIC: unknown(f"{column.name} is nullable in {relation.name}", relation, column)}, []
        case ir.OpaqueRelation():
            return {ORGANIC: unknown("Nullability of an opaque relation's columns is not known", relation, column)}, []
        case ir.SetOperation():
            return _set_operation_sources(relation, column)
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
        case ir.NamedRelation() | ir.Project() | ir.Limit() | ir.Distinct() | ir.QualifyByPartition():
            return _expression_sources(column.scalar_expr)
        case _:
            assert_never(relation)


def _expression_sources(expression: ir.ScalarExpr) -> tuple[_Sources, list[Result]]:
    expression = expression.closed
    match expression:
        case ir.ColumnRef(column=source):
            sources = _null_sources(source)
            return dict(sources.members), [Result(True, "Column passes through its source", (expression,), [sources])]
        case ir.Constant():
            return {}, [Result(True, "A non-null literal", (expression,))]
        case ir.Count():
            return {}, [Result(True, "count(...) is never NULL", (expression,))]
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
        case ir.Call(total_arguments=tuple() as arguments, description=description):
            # NULL only when an argument it is total in is, so only through
            # theirs; NULL on every padded row only through a strict one.
            strict = {id(argument) for argument in expression.strict_arguments}
            result: _Sources = {}
            premises: list[Result] = []
            for argument in arguments:
                sources, argument_premises = _expression_sources(argument)
                if id(argument) not in strict:
                    sources = _may_be_null(sources, f"{expression.function.name} may be non-null where this argument is NULL", argument)
                for source, evidence in sources.items():
                    result[source] = result[source] | evidence if source in result else evidence
                premises.extend(argument_premises)
            if result:
                return result, []
            reason = f"{expression.function.name} is non-null when its arguments are" if arguments else f"{expression.function.name} is never NULL"
            return {}, [Result(True, reason, (expression,), premises)]
        case ir.OpaqueExpression(description=description) | ir.OpaquePureFunction(description=description) | ir.Call(description=description):
            return {ORGANIC: unknown(f"Nullability of {description} is not known", expression)}, []
        case ir.Elements(operand=operand):
            elements = _expression_elements(operand)
            if elements.value:
                return {}, [Result(True, "No element is NULL", (expression,), [elements])]
            return {ORGANIC: Result(False, "An element may be NULL", (expression,), [elements], kind="unknown")}, []
        case ir.Equality() | ir.IsNull() | ir.IsNotNull() | ir.InList() | ir.And() | ir.Or():
            return {ORGANIC: unknown("Nullability of a comparison is not inferred", expression)}, []
        case ir.SimpleCase():
            return _case_sources(expression)
        case _:
            assert_never(expression)


@analysis(Stratum.NON_NULL)
def non_null_elements(columns: frozenset[ir.OutputColumn]) -> Result[bool]:
    relation = current_arena().program.relation_of(columns)
    if relation is None:
        return unknown("Columns with non-null elements must belong to one relation", *columns)
    return all_of(_column_elements(column) for column in relation.output_columns if column in columns)


@analysis(Stratum.NON_NULL)
def _column_elements(column: ir.OutputColumn) -> Result[bool]:
    """Whether no element of `column`'s value is NULL: declared, or so in
    every value it passes on (a set operation's from each input)."""
    arena = current_arena()
    declared = arena.accepted_evidence(NonNullElements(columns=frozenset({column})))
    if declared:
        return Result(True, "Declared non-null elements", (column,), [any_of(declared)])
    relation = column.get_relation(arena.program)
    if isinstance(relation, ir.SetOperation):
        arms = relation.arms(column)
        if arms is None:
            return unknown("The set operation's inputs are not paired by position", relation, column)
        return all_of(_column_elements(arm) for arm in arms if arm is not None)
    if isinstance(column.scalar_expr.closed, ir.OpaqueExpression):
        return unknown(f"{column.name} may hold a NULL element (DECLARE NOT NULL ELEMENTS({column.name}) states it does not)", column)
    return _expression_elements(column.scalar_expr)


def _expression_elements(expression: ir.ScalarExpr) -> Result[bool]:
    expression = expression.closed
    match expression:
        case ir.ColumnRef(column=column):
            return _column_elements(column)
        case ir.NullLiteral():
            return Result(True, "NULL has no elements", (expression,))
        case ir.AnyAggregate(input=value):
            return _expression_elements(value)
        case ir.Coalesce(arguments=arguments):
            return all_of(_expression_elements(argument) for argument in arguments)
        case ir.SimpleCase(thens=thens, default=default):
            return all_of(_expression_elements(arm) for arm in (*thens, default))
        case (
            ir.Constant() | ir.Count() | ir.Call() | ir.OpaqueExpression() | ir.OpaquePureFunction() | ir.Elements()
            | ir.Equality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or()
        ):
            return unknown("Its elements may be NULL", expression)
        case _:
            assert_never(expression)


def _case_sources(case: ir.SimpleCase) -> tuple[_Sources, list[Result]]:
    """A CASE is NULL when the arm it takes is: any THEN, or ELSE (NULL when
    missing) unless no row reaches it, the operand non-null and every value
    of its domain listed by a WHEN (issue 32)."""
    from sqlassert.rules.domain import _else_unreachable  # Domains read non-nullness.

    sources: _Sources = {}
    premises: list[Result] = []
    arm_paddings: list[set[NullSource]] = []

    def add(reason: str, arm: ir.ScalarExpr, *why: Result) -> None:
        arm_sources, arm_premises = _expression_sources(arm)
        premises.extend(arm_premises)
        arm_paddings.append({source for source in arm_sources if isinstance(source, Padding)})
        for source, evidence in arm_sources.items():
            named = Result(False, reason, (arm,), [evidence, *why], kind="unknown")
            sources[source] = sources[source] | named if source in sources else named

    for index, then in enumerate(case.thens, 1):
        add(f"THEN arm {index} may be NULL", then)
    unreachable = _else_unreachable(case)
    if unreachable.value:
        premises.append(Result(True, "No row reaches ELSE", (case,), [unreachable]))
    else:
        missing = isinstance(case.default, ir.NullLiteral) and case.default.origin == case.origin
        reason = "With no ELSE, a row no WHEN matches is NULL" if missing else "ELSE may be NULL"
        add(reason, case.default, unreachable)
    # NULL on a join's padded rows only if every arm it may take is.
    common = set.intersection(*arm_paddings) if arm_paddings else set()
    kept = {source: evidence for source, evidence in sources.items() if not isinstance(source, Padding) or source in common}
    others = {source: evidence for source, evidence in sources.items() if source not in kept}
    for source, evidence in _may_be_null(others, "Another arm may be non-null on padded rows", case).items():
        kept[source] = kept[source] | evidence if source in kept else evidence
    return kept, premises


def _may_be_null(sources: _Sources, reason: str, expression: ir.ScalarExpr) -> _Sources:
    """`sources` where a padding only means "may be NULL": organic."""
    result: _Sources = {}
    for source, evidence in sources.items():
        if isinstance(source, Padding):
            evidence = Result(False, reason, (expression,), [evidence], kind="unknown")
            source = ORGANIC
        result[source] = result[source] | evidence if source in result else evidence
    return result


def _join_sources(join: ir.Join, column: ir.OutputColumn) -> tuple[_Sources, list[Result]]:
    sources, premises = _expression_sources(column.scalar_expr)
    match column.scalar_expr:
        case ir.ColumnRef(column=source):
            pass
        case _:
            raise TypeError(f"Join column {column.name} is not a pass-through")
    sources, removal = _rejected(_on_rejected(join), source, sources, "The ON condition rejects NULL on matched rows")
    left = source in join.left.output_columns
    if left in _padded_sides(join):
        sources[Padding(join, left)] = unknown(f"{column.name} is padded by the {join.kind.upper()} JOIN", join, column)
    return sources, premises + removal


def _set_operation_sources(relation: ir.SetOperation, column: ir.OutputColumn) -> tuple[_Sources, list[Result]]:
    """Paulley §3.2.5-3.2.6: a UNION row comes from either input, an INTERSECT
    row equals a row of both, an EXCEPT row is a left input row. An input's
    padding is not this relation's, so any remaining source is organic."""
    arms = relation.arms(column)
    if arms is None:
        if relation.operator != "except":
            return {ORGANIC: unknown("The set operation's inputs are not paired by position", relation, column)}, []
        arms = (_left_input(column), None)
    left, right = (_null_sources(arm) if arm is not None else None for arm in arms)
    assert left is not None
    match relation.operator:
        case "union":
            assert right is not None
            if not left.value and not right.value:
                return {}, [Result(True, "Non-null in both inputs of the UNION", (relation, column), [left, right])]
            return {ORGANIC: Result(False, f"{column.name} may be NULL in an input of the UNION", (relation, column), [left, right], kind="unknown")}, []
        case "intersect":
            assert right is not None
            for arm in (left, right):
                if not arm.value:
                    return {}, [Result(True, "Non-null in one input of the INTERSECT, which every row equals", (relation, column), [arm])]
            return {ORGANIC: Result(False, f"{column.name} may be NULL in both inputs of the INTERSECT", (relation, column), [left, right], kind="unknown")}, []
        case "except":
            if not left.value:
                return {}, [Result(True, "Non-null in the left input of the EXCEPT", (relation, column), [left])]
            return {ORGANIC: Result(False, f"{column.name} may be NULL in the left input of the EXCEPT", (relation, column), [left], kind="unknown")}, []
        case _:
            assert_never(relation.operator)


def _left_input(column: ir.OutputColumn) -> ir.OutputColumn:
    match column.scalar_expr:
        case ir.ColumnRef(column=source):
            return source
        case _:
            raise TypeError(f"Set operation column {column.name} is not a pass-through")


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
    """Each column a conjunct requires to be non-null, with that conjunct.

    A conjunct cannot be TRUE while it is NULL, so it rejects every column it
    is strict in; `IS NOT NULL` rejects those of its operand (Paulley §3.2.4,
    footnote 9). An OR rejects the columns each of its parts rejects.
    """
    rejected: dict[ir.OutputColumn, ir.ScalarExpr] = {}
    for conjunct in conjuncts:
        match conjunct:
            case ir.Or(parts=parts):
                first, *others = (_null_rejected(ir.conjuncts(part)) for part in parts)
                columns = tuple(column for column in first if all(column in other for other in others))
            case ir.IsNotNull(operand=operand):
                columns = ir.strict_inputs(operand)
            case _:
                columns = ir.strict_inputs(conjunct)
        for column in columns:
            rejected.setdefault(column, conjunct)
    return rejected


def _on_rejected(join: ir.Join) -> dict[ir.OutputColumn, ir.ScalarExpr]:
    """Input columns non-null on every matched row of `join`, on its padded side.

    An inner join drops rows failing ON on both sides; an outer join keeps them
    padded, so ON only constrains the side it pads. A semi join keeps the left
    rows some right row matches, an anti join those none does: ON constrains
    only the semi join's.
    """
    rejected = _null_rejected(join.conjuncts)
    match join.kind:
        case "inner" | "semi":
            return rejected
        case "left":
            return {column: c for column, c in rejected.items() if column in join.right.output_columns}
        case "right":
            return {column: c for column, c in rejected.items() if column in join.left.output_columns}
        case "cross" | "full" | "anti":
            return {}
        case _:
            assert_never(join.kind)


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
        case "inner" | "cross" | "semi" | "anti":
            return frozenset()
        case _:
            assert_never(join.kind)


def _seen_through(alias: ir.Alias, source: NullSource) -> NullSource:
    match source:
        case Padding(join=join, left=left, aliases=aliases):
            return Padding(join, left, (*aliases, alias))
        case Organic():
            return source
