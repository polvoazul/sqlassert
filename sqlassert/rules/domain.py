"""Enumerated Domains: the finite set a column's non-null values come from.

A column's own domain comes from Knowledge about it and from its expression:
a literal is its own value, NULL contributes none, and `coalesce` takes one of
its arguments' values. A pass-through column keeps its source's domain, which
a strict `WHERE` or `ON` conjunct `col = expr` or `col IN (...)` narrows to the
values on its other side. Its domain is then the intersection of the own
domains of every column in its equality group: on every row they carry one
value, or are all NULL. An unbounded domain is `None`.

NULL is outside every domain, so padding and NULL literals never widen one.

Members are typed literals, and covering is by identity: a value is covered
only by an identical literal, so numeric 1 never covers string '1', nor 1.0.
Intersecting is not by identity, because a column is compared with a literal
after implicit casts, so two distinct literals can both equal its value: a
FLOAT equals 0.1 and 0.100000001, a VARCHAR '1.4' equals 1 and 1.4, an
INTEGER '01' and '1', and a case-insensitive VARCHAR 'a' and 'A'. A meet
therefore keeps every value that may equal one on each other side
(`_may_coincide`), which only ever keeps more than the true intersection.
Distinct strings are distinct values only where the column is text compared
by identity (`_binary_text`).

A simple CASE takes one of its THEN values, or its ELSE value unless ELSE is
unreachable: the CASE is exhaustive and its operand non-null. Exhaustiveness
covers by identity too: an arm covers a domain member only when it is that
literal, so the operand compared with the arm is the very comparison that
puts its value in the domain.
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import assert_never

from sqlassert.diagnostics import quoted
from sqlassert import ir
from sqlassert.properties import EnumeratedDomain
from sqlassert.python_engine import Result, Stratum, analysis, any_of, current_arena, unknown
from sqlassert.rules.equality import _equal_columns, _same_type
from sqlassert.rules.nullability import non_null_column

# A finite set of possible non-null values, or None when any value is possible.
type Domain = frozenset[ir.LiteralValue] | None


@analysis(Stratum.DOMAIN)
def enumerated_domain(column: ir.OutputColumn, values: frozenset[ir.LiteralValue]) -> Result[bool]:
    domain = _domain(column)
    if domain.value is None:
        return Result(False, f"No finite domain is known for {quoted(column.name)}", (column,), [domain], kind="unknown")
    uncovered = domain.value - values
    if uncovered:
        return Result(
            False, f"{quoted(column.name)} may be {_render(uncovered)}, which the assertion does not cover",
            (column,), [domain], kind="unknown",
        )
    return Result(True, "Every possible value is listed", (column,), [domain])


@analysis(Stratum.DOMAIN)
def exhaustive_case(case: ir.SimpleCase) -> Result[bool]:
    operand = _operand_name(case.operand)
    domain = _expression_domain(case.operand)
    if domain.value is None:
        return Result(False, f"No finite domain is known for {operand}", (case,), [domain], kind="unknown")
    missing = domain.value - _listed(case)
    if missing:
        return Result(False, f"{operand} may be {_render(missing)}, which no WHEN arm lists", (case,), [domain], kind="unknown")
    return Result(True, f"Every possible value of {operand} has a WHEN arm", (case,), [domain])


def _listed(case: ir.SimpleCase) -> frozenset[ir.LiteralValue]:
    """The literals a WHEN arm is exactly."""
    listed: set[ir.LiteralValue] = set()
    for when in case.whens:
        match when:
            case ir.Constant(value=ir.LiteralValue() as value):
                listed.add(value)
            case _:
                pass  # Another arm covers no member by identity.
    return frozenset(listed)


def _operand_name(operand: ir.ScalarExpr) -> str:
    match operand:
        case ir.ColumnRef(column=column):
            return column.name
        case _:
            return "the CASE operand"


def _else_unreachable(case: ir.SimpleCase) -> Result[bool]:
    """No row reaches ELSE: every non-null operand value has an arm, and the operand is non-null."""
    match case.operand:
        case ir.ColumnRef(column=column):
            present = non_null_column(column)
        case ir.Constant(value=ir.LiteralValue()):
            present = Result(True, "A non-null literal", (case.operand,))
        case _:
            present = unknown("Nullability of the CASE operand is not inferred", case.operand)
    return exhaustive_case(case) & present


@analysis(Stratum.DOMAIN)
def _domain(column: ir.OutputColumn) -> Result[Domain]:
    """`column`'s domain: its own, intersected with every equal column's."""
    group = _equal_columns(column)
    relation = column.get_relation(current_arena().program)
    members = [member for member in relation.output_columns if member in group.value]
    owns = [_own_domain(member) for member in members]
    known = [own for own in owns if own.value is not None]
    if not known:
        return Result(None, f"No finite domain is known for {quoted(column.name)}", (column,), owns)
    value = _meet([own.value for own in known if own.value is not None], _binary_text(column))
    premises = [group, *known] if len(members) > 1 else known
    return Result(value, f"{quoted(column.name)} is one of {_render(value)}", (column,), premises)


@analysis(Stratum.DOMAIN)
def _own_domain(column: ir.OutputColumn) -> Result[Domain]:
    """`column`'s domain from Knowledge about it and from how its relation computes it."""
    arena = current_arena()
    sources: list[Result[Domain]] = [_computed_domain(column.get_relation(arena.program), column)]
    for item in arena.knowledge:
        match item:
            case EnumeratedDomain(column=known, values=values) if known is column:
                sources.append(Result(values, f"{quoted(column.name)} is known to be one of {_render(values)}", (column,), [any_of(arena.accepted_evidence(item))]))
            case _:
                pass  # Other Knowledge says nothing about values.
    return _intersection(column, sources)


def _computed_domain(relation: ir.RelationExpr, column: ir.OutputColumn) -> Result[Domain]:
    relation = relation.closed
    match relation:
        case ir.NamedRelation(body=None) | ir.OpaqueRelation():
            return Result(None, f"{quoted(column.name)} is not enumerated", (column,))
        case ir.SetOperation():
            return _set_operation_domain(relation, column)
        case ir.Filter() | ir.Join():
            return _intersection(column, [_expression_domain(column.scalar_expr), *_narrowings(relation, column)])
        case ir.Alias() | ir.NamedRelation() | ir.Project() | ir.Limit() | ir.Aggregate() | ir.Distinct() | ir.QualifyByPartition():
            return _expression_domain(column.scalar_expr)
        case _:
            assert_never(relation)


def _set_operation_domain(relation: ir.SetOperation, column: ir.OutputColumn) -> Result[Domain]:
    """A UNION row comes from either input, an INTERSECT row equals a row of
    both, an EXCEPT row is a left input row (Paulley §3.2.5-3.2.6). Inputs
    pair by position; a UNION of mixed value types is not enumerated, since
    the output type may convert one."""
    left = _expression_domain(column.scalar_expr)
    arms = relation.arms(column)
    if relation.operator == "except" or arms is None and relation.operator == "intersect":
        return left
    if arms is None:
        return Result(None, "The UNION's inputs are not paired by position", (relation, column))
    right = _domain(arms[1])
    if relation.operator == "intersect":
        # The row carries the left value, equal to a right one under the left
        # column's comparison: a string bounds it only if that is identity.
        if right.value is not None and any(member.type == "string" for member in right.value) and not _binary_text(arms[0]):
            return left
        return _intersection(column, [left, right])
    if left.value is None or right.value is None:
        return Result(None, f"An input of the UNION does not enumerate {quoted(column.name)}", (relation, column), [left, right])
    value = left.value | right.value
    if len({member.type for member in value}) > 1:
        return Result(None, f"The UNION's inputs give {quoted(column.name)} values of different types", (relation, column), [left, right])
    return Result(value, f"{quoted(column.name)} is one of either input's values: {_render(value)}", (relation, column), [left, right])


def _expression_domain(expression: ir.ScalarExpr) -> Result[Domain]:
    expression = expression.closed
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
            # The value comes from some input row, or is NULL (an empty group).
            return _expression_domain(value)
        case ir.SimpleCase(thens=thens, default=default):
            arms = [_expression_domain(then) for then in thens]
            unreachable = _else_unreachable(expression)
            if not unreachable.value:
                arms.append(_expression_domain(default))
            if any(arm.value is None for arm in arms):
                return Result(None, "A CASE arm is not enumerated", (expression,), arms)
            value = frozenset().union(*(arm.value for arm in arms if arm.value is not None))
            premises = [*arms, unreachable] if unreachable.value else arms
            return Result(value, "A CASE takes one of its arms' values", (expression,), premises)
        case (
            ir.Constant() | ir.OpaqueExpression() | ir.OpaquePureFunction() | ir.Call() | ir.Count() | ir.Accumulation() | ir.Elements() | ir.Equality()
            | ir.NullSafeEquality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or()
        ):
            return Result(None, "Values of this expression are not enumerated", (expression,))
        case _:
            assert_never(expression)


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
        case ir.Join(kind="inner" | "cross" | "semi", conjuncts=conjuncts):
            clause = "ON"
        case ir.Join(kind="left", right=padded, conjuncts=conjuncts) | ir.Join(kind="right", left=padded, conjuncts=conjuncts) if source in padded.output_columns:
            clause = "ON"
        case _:
            return []
    narrowings: list[Result[Domain]] = []
    for conjunct in conjuncts:
        bound = _conjunct_bound(conjunct, source)
        if bound is not None and bound.value is not None:
            narrowings.append(Result(bound.value, f"The {clause} conjunct bounds {quoted(column.name)} to {_render(bound.value)}", (conjunct,), [bound]))
    return narrowings


def _conjunct_bound(conjunct: ir.ScalarExpr, source: ir.OutputColumn) -> Result[Domain] | None:
    """The values `source` takes on rows where `conjunct` is TRUE, or None
    when the conjunct does not bound it.

    An AND bounds it to what each of its bounding parts allows; an OR to the
    union of its parts' bounds, when every part bounds it. A bound is never an
    unknown domain.
    """
    match conjunct:
        # A TRUE IS NOT DISTINCT FROM leaves a non-null side equal to the other.
        case ir.Equality(left=ir.ColumnRef(column=first), right=other) | ir.NullSafeEquality(left=ir.ColumnRef(column=first), right=other) if first is source:
            bounds = [_bound_domain(source, other)]
        case ir.Equality(left=other, right=ir.ColumnRef(column=second)) | ir.NullSafeEquality(left=other, right=ir.ColumnRef(column=second)) if second is source:
            bounds = [_bound_domain(source, other)]
        case ir.InList(operand=ir.ColumnRef(column=operand), values=values) if operand is source:
            bounds = [_bound_domain(source, value) for value in values]
        case ir.And(parts=parts):
            found = [bound for part in parts if (bound := _conjunct_bound(part, source)) is not None]
            # Distinct literals may compare equal (issue 29), so parts meet as domains do.
            return _intersection(source, found) if found else None
        case ir.Or(parts=parts):
            bounds = [_conjunct_bound(part, source) for part in parts]
            if any(bound is None for bound in bounds):
                return None  # A part that does not bound the column lets any value through.
        case _:
            return None  # Other conjuncts do not bound this column.
    if any(bound is None or bound.value is None for bound in bounds):
        return None
    value = frozenset().union(*(bound.value for bound in bounds if bound is not None and bound.value is not None))
    return Result(value, f"{quoted(source.name)} is in {_render(value)} on every row this conjunct keeps", (conjunct,), [bound for bound in bounds if bound is not None])


def _bound_domain(column: ir.OutputColumn, expression: ir.ScalarExpr) -> Result[Domain]:
    match expression:
        case ir.Constant() | ir.NullLiteral():
            return _expression_domain(expression)
        case ir.ColumnRef(column=source) if _same_type(column, source):
            return _expression_domain(expression)
        case _:
            return Result(None, "The comparison may cast its operands, so no value domain is transferred", (expression,))


def _intersection(column: ir.OutputColumn, sources: list[Result[Domain]]) -> Result[Domain]:
    known = [source for source in sources if source.value is not None]
    if not known:
        return Result(None, f"No finite domain is known for {quoted(column.name)}", (column,), sources)
    if len(known) == 1:
        return known[0]
    value = _meet([source.value for source in known if source.value is not None], _binary_text(column))
    return Result(value, f"{quoted(column.name)} is one of {_render(value)}", (column,), known)


def _meet(domains: list[frozenset[ir.LiteralValue]], binary_text: bool) -> frozenset[ir.LiteralValue]:
    """The first domain's values that may equal a value of every other one.

    A column's value equals some member of each domain, so the member of the
    first it equals may coincide with one of every other and is kept.
    """
    first, *others = domains
    return frozenset(value for value in first if all(any(_may_coincide(value, other, binary_text) for other in domain) for domain in others))


def _binary_text(column: ir.OutputColumn) -> bool:
    """Whether `column` and every column equated with it, here or upstream,
    are text compared by identity, so carry one string value.

    Only matching comparison types merge Equality Groups. A set operation's
    type may widen its left input's, so its columns are not followed.
    """
    relation = column.get_relation(current_arena().program)
    for member in _equal_columns(column).value:
        match member.scalar_expr:
            case ir.ColumnRef(column=source) if not isinstance(relation, ir.SetOperation):
                if not _binary_text(source):
                    return False
            case _:
                if not member.binary_text:
                    return False
    return True


# A FLOAT keeps 24 significant bits: two numbers rounding to one FLOAT differ
# by at most its precision relative to their size, with margin for a literal
# rounded through DOUBLE first, or are both too small to tell from zero.
_FLOAT_RELATIVE = Fraction(1, 2**22)
_FLOAT_ABSOLUTE = Fraction(1, 2**148)


def _may_coincide(first: ir.LiteralValue, second: ir.LiteralValue, binary_text: bool) -> bool:
    """Whether some column value may equal both literals.

    Literals of different types may: SQL casts one side, so '1' and 1 both
    equal INTEGER 1. Distinct booleans never do. Distinct strings may unless
    the column is text compared by identity: an INTEGER equals '01' and '1',
    a case-insensitive one 'a' and 'A'. Distinct numbers may when one value
    rounds to both.
    """
    if first == second or first.type != second.type:
        return True
    match first.type:
        case "string":
            return not binary_text
        case "boolean":
            return False
        case "numeric":
            return _numbers_may_coincide(first.text, second.text)
        case _:
            assert_never(first.type)


def _numbers_may_coincide(first: str, second: str) -> bool:
    gap = abs(_number(first) - _number(second))
    # A string is cast to an exact literal's type, rounding half away from
    # zero to its scale: one value rounds to both when the cells overlap.
    if gap < _half_step(first) + _half_step(second):
        return True
    # A FLOAT or DOUBLE is compared at its own precision.
    return gap <= max(abs(_number(first)), abs(_number(second))) * _FLOAT_RELATIVE or gap <= _FLOAT_ABSOLUTE


def _half_step(text: str) -> Fraction:
    """Half the rounding step of a numeric literal's SQL type: an integer's
    is one, a decimal's its last digit, and an exponent form is a DOUBLE."""
    if "e" in text:
        return Fraction(0)
    _, _, decimals = text.partition(".")
    return Fraction(1, 2 * 10 ** len(decimals))


def _number(text: str) -> Fraction:
    return Fraction(Decimal(text))


def _render(values: frozenset[ir.LiteralValue]) -> str:
    return ", ".join(value.sql() for value in sorted(values, key=_order)) if values else "no non-null value"


def _order(value: ir.LiteralValue) -> tuple[str, Fraction, str]:
    """Booleans, then numbers by value, then strings: a fixed reading order."""
    return value.type, _number(value.text) if value.type == "numeric" else Fraction(0), value.text
