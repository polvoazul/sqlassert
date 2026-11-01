"""Enumerated Domains: the finite set a column's non-null values come from.

A column's own domain comes from Knowledge about it and from its expression:
a literal is its own value, NULL contributes none, and `coalesce` takes one of
its arguments' values. A pass-through column keeps its source's domain, which
a strict `WHERE` or `ON` conjunct `col = expr` or `col IN (...)` narrows to the
values on its other side. Its domain is then the intersection of the own
domains of every column in its equality group: on every row they carry one
value, or are all NULL. An unbounded domain is `None`.

NULL is outside every domain, so padding and NULL literals never widen one.
"""

from __future__ import annotations

from sqlassert import ir
from sqlassert.properties import EnumeratedDomain
from sqlassert.python_engine import Result, Stratum, analysis, any_of, current_arena
from sqlassert.rules.equality import _equal_columns

# A finite set of possible non-null values, or None when any value is possible.
type Domain = frozenset[ir.LiteralValue] | None


@analysis(Stratum.DOMAIN)
def enumerated_domain(column: ir.OutputColumn, values: frozenset[ir.LiteralValue]) -> Result[bool]:
    domain = _domain(column)
    if domain.value is None:
        return Result(False, f"No finite domain is known for {column.name}", (column,), [domain], kind="unknown")
    uncovered = domain.value - values
    if uncovered:
        return Result(
            False, f"{column.name} may be {_render(uncovered)}, which the assertion does not cover",
            (column,), [domain], kind="unknown",
        )
    return Result(True, "Every possible value is listed", (column,), [domain])


@analysis(Stratum.DOMAIN)
def _domain(column: ir.OutputColumn) -> Result[Domain]:
    """`column`'s domain: its own, intersected with every equal column's."""
    group = _equal_columns(column)
    relation = column.get_relation(current_arena().program)
    members = [member for member in relation.output_columns if member in group.value]
    owns = [_own_domain(member) for member in members]
    known = [own for own in owns if own.value is not None]
    if not known:
        return Result(None, f"No finite domain is known for {column.name}", (column,), owns)
    value = frozenset.intersection(*(own.value for own in known if own.value is not None))
    premises = [group, *known] if len(members) > 1 else known
    return Result(value, f"{column.name} is one of {_render(value)}", (column,), premises)


@analysis(Stratum.DOMAIN)
def _own_domain(column: ir.OutputColumn) -> Result[Domain]:
    """`column`'s domain from Knowledge about it and from how its relation computes it."""
    arena = current_arena()
    sources: list[Result[Domain]] = [_computed_domain(column.get_relation(arena.program), column)]
    for item in arena.knowledge:
        match item:
            case EnumeratedDomain(column=known, values=values) if known is column:
                sources.append(Result(values, f"{column.name} is known to be one of {_render(values)}", (column,), [any_of(arena.accepted_evidence(item))]))
            case _:
                pass  # Other Knowledge says nothing about values.
    return _intersection(column, sources)


def _computed_domain(relation: ir.RelationExpr, column: ir.OutputColumn) -> Result[Domain]:
    match relation:
        case ir.NamedRelation(body=None) | ir.OpaqueRelation():
            return Result(None, f"{column.name} is not enumerated", (column,))
        case ir.SetOperation(operator="union"):
            # Outputs refer to the left input only; the right one is not tracked.
            return Result(None, "A UNION column's values are not enumerated", (relation, column))
        case ir.Filter() | ir.Join():
            return _intersection(column, [_expression_domain(column.scalar_expr), *_narrowings(relation, column)])
        case ir.Alias() | ir.NamedRelation() | ir.Project() | ir.Aggregate() | ir.Distinct() | ir.QualifyByPartition() | ir.SetOperation():
            return _expression_domain(column.scalar_expr)
        case _:
            raise TypeError(f"Unhandled relation: {type(relation).__name__}")


def _expression_domain(expression: ir.ScalarExpr) -> Result[Domain]:
    match expression:
        case ir.ColumnRef(column=source):
            return _domain(source)
        case ir.Constant(value=ir.LiteralValue() as value):
            return Result(frozenset({value}), f"The literal {value.sql()}", (expression,))
        case ir.NullLiteral():
            return Result(frozenset(), "NULL is outside every domain", (expression,))
        case ir.Coalesce(arguments=arguments):
            # The first non-null argument, so one of theirs.
            domains = [_expression_domain(argument) for argument in arguments]
            if any(domain.value is None for domain in domains):
                return Result(None, "A coalesce argument is not enumerated", (expression,), domains)
            value = frozenset().union(*(domain.value for domain in domains if domain.value is not None))
            return Result(value, "Coalesce takes one of its arguments' values", (expression,), domains)
        case ir.AnyAggregate(input=value):
            # Groups are never empty, so the value comes from some input row.
            return _expression_domain(value)
        case ir.Constant() | ir.OpaqueExpression() | ir.Equality() | ir.InList() | ir.IsNull() | ir.IsNotNull():
            return Result(None, "Values of this expression are not enumerated", (expression,))
        case _:
            raise TypeError(f"Unhandled scalar expression: {type(expression).__name__}")


def _narrowings(relation: ir.Filter | ir.Join, column: ir.OutputColumn) -> list[Result[Domain]]:
    """Domains a conjunct true on every row carrying `column`'s value bounds it to.

    `WHERE` and an inner join's ON hold on every row; an outer join's ON holds
    on the matched rows only, which pad nothing on the side it pads.
    """
    match column.scalar_expr:
        case ir.ColumnRef(column=source):
            pass
        case _:
            raise TypeError(f"{type(relation).__name__} column {column.name} is not a pass-through")
    match relation:
        case ir.Filter(conjuncts=conjuncts):
            clause = "WHERE"
        case ir.Join(kind="inner" | "cross", conjuncts=conjuncts):
            clause = "ON"
        case ir.Join(kind="left", right=padded, conjuncts=conjuncts) | ir.Join(kind="right", left=padded, conjuncts=conjuncts) if source in padded.output_columns:
            clause = "ON"
        case _:
            return []
    narrowings: list[Result[Domain]] = []
    for conjunct in conjuncts:
        match conjunct:
            case ir.Equality(left=ir.ColumnRef(column=first), right=other) if first is source:
                bounds = [_expression_domain(other)]
            case ir.Equality(left=other, right=ir.ColumnRef(column=second)) if second is source:
                bounds = [_expression_domain(other)]
            case ir.InList(operand=ir.ColumnRef(column=operand), values=values) if operand is source:
                bounds = [_expression_domain(value) for value in values]
            case _:
                continue  # Other conjuncts do not bound this column.
        if any(bound.value is None for bound in bounds):
            continue
        value = frozenset().union(*(bound.value for bound in bounds if bound.value is not None))
        narrowings.append(Result(value, f"The {clause} conjunct bounds {column.name} to {_render(value)}", (conjunct,), bounds))
    return narrowings


def _intersection(column: ir.OutputColumn, sources: list[Result[Domain]]) -> Result[Domain]:
    known = [source for source in sources if source.value is not None]
    if not known:
        return Result(None, f"No finite domain is known for {column.name}", (column,), sources)
    if len(known) == 1:
        return known[0]
    value = frozenset.intersection(*(source.value for source in known if source.value is not None))
    return Result(value, f"{column.name} is one of {_render(value)}", (column,), known)


def _render(values: frozenset[ir.LiteralValue]) -> str:
    return ", ".join(value.sql() for value in sorted(values)) if values else "no non-null value"
