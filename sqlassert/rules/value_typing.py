"""Value typing: how the values of a scalar expression compare, and the one
question every equality asks of it.

`scalar_typing(expression)` is the one place that decides what type an
expression has, or that it has none known: a column's, a literal's, a CAST's,
a function the dialect gives a fixed result type, integer arithmetic, a float
contaminating arithmetic, a CASE, COALESCE or NULLIF's results' common type.
Convert records only what SQL says of one node (a declared type, a literal's
kind, a CAST's target, a fixed result type); everything that follows from
those is derived here, so no two places can disagree about an expression.

`exact_comparison(left, right)` asks whether `left = right` compares their
values with no lossy coercion: both types are known, and either both are text
compared by identity, or both are of one type whose equal values are
identical, or both are integers (an integer of any width compares by value
with any other), or one is an exact number literal and the other an integer or
decimal. Every consumer of an equality reads this answer: what an equality
pins or binds, the Equality Groups it merges, and the Hazard of a comparison
that is not exact. An unknown type gives no fact.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import assert_never

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, analysis, current_arena

# What `count(...)` is: an integer, not claimed to be a width.
_BIGINT = ir.ValueTyping("BIGINT", False, True, True, integer=4)
# What exact arithmetic over a decimal is: no precision or scale is claimed, so no
# type but another computed decimal's is the same.
_DECIMAL_EXPRESSION = ir.ValueTyping("DECIMAL EXPRESSION", False, True, True)

_ARITHMETIC = {"+", "-", "*", "unary -"}


def _unknown(expression: object, why: str = "No type is known") -> Result[ir.ValueTyping | None]:
    return Result(None, why, (expression,))


@analysis(Stratum.COMPARISON)
def scalar_typing(expression: ir.ScalarExpr) -> Result[ir.ValueTyping | None]:
    """How the values of `expression` compare, or None when its type is not known."""
    expression = expression.closed
    match expression:
        case ir.ColumnRef(column=column):
            return _column_typing(column)
        case ir.Constant(typing=ir.ValueTyping() as typing) | ir.OpaqueExpression(typing=ir.ValueTyping() as typing):
            return Result(typing, "The type of a literal or a call the dialect types", (expression,))
        case ir.Count():
            return Result(_BIGINT, "count(...) is an integer", (expression,))
        case ir.AnyAggregate(input=value):
            # A picked value (min, max, any_value) is a value of its input.
            return scalar_typing(value)
        case ir.Call(typing=ir.ValueTyping() as typing) | ir.OpaquePureFunction(typing=ir.ValueTyping() as typing):
            return Result(typing, "The type of a CAST or of a function the dialect gives a fixed result type", (expression,))
        case ir.Call(function=ir.Builtin(name="nullif"), arguments=arguments):
            return _common_typing(expression, arguments)
        case ir.Call():
            return _arithmetic_typing(expression)
        case ir.OpaquePureFunction(arguments=arguments, results=tuple() as results):
            return _common_typing(expression, [arguments[position] for position in results])
        case ir.Coalesce(arguments=arguments):
            return _common_typing(expression, arguments)
        case ir.SimpleCase(thens=thens, default=default):
            return _common_typing(expression, [*thens, default])
        case (
            ir.Constant() | ir.NullLiteral() | ir.OpaquePureFunction() | ir.OpaqueExpression() | ir.Accumulation() | ir.Elements()
            | ir.Equality() | ir.NullSafeEquality() | ir.InList() | ir.IsNull() | ir.IsNotNull() | ir.And() | ir.Or()
        ):
            return _unknown(expression)
        case _:
            assert_never(expression)


@analysis(Stratum.COMPARISON)
def _column_typing(column: ir.OutputColumn) -> Result[ir.ValueTyping | None]:
    """The type of a column: the one it is declared with, else its expression's.
    A set operation's column has its inputs' when they share it, not the left
    input's: the engine would cast one to the other."""
    relation = column.get_relation(current_arena().program)
    if isinstance(relation, ir.SetOperation):
        if (arms := relation.arms(column)) is not None:
            left, right = (_column_typing(arm) for arm in arms)
            first, second = left.value, right.value
            if first is not None and second is not None and first.comparison_type == second.comparison_type:
                shared = ir.ValueTyping(
                    first.comparison_type, first.binary_text and second.binary_text,
                    first.identity_comparison and second.identity_comparison, first.exact_numeric and second.exact_numeric,
                    first.floating and second.floating, first.integer if first.integer == second.integer else None,
                )
                return Result(shared, "Both inputs of the set operation share the type", (column,), [left, right])
        return _unknown(column, "The inputs of the set operation do not share a type")
    if column.typing is not None:
        return Result(column.typing, "Declared type", (column,))
    return scalar_typing(column.scalar_expr)


def _common_typing(expression: ir.ScalarExpr, results: Sequence[ir.ScalarExpr]) -> Result[ir.ValueTyping | None]:
    """The type of an expression that returns one of `results` (or NULL): the
    one they share (the NULLs take it); among integer types the widest, an
    integer type being all that every engine promotes them to; a string
    literal beside text compared by identity is that text. None when a result
    has no known type, or the types differ (the Hazard `Mixed CASE types`)."""
    values = [result for result in results if not isinstance(result, ir.NullLiteral)]
    literals = [value for value in values if isinstance(value, ir.Constant) and value.value is not None and value.value.type == "string"]
    typed = [scalar_typing(value) for value in values if not any(value is literal for literal in literals)]
    typings = [result.value for result in typed]
    if None in typings:
        return _unknown(expression, "A result has no known type")
    known = [typing for typing in typings if typing is not None]
    if not known:
        # Only NULLs and string literals: the literals' type.
        if not literals:
            return _unknown(expression, "Every result is NULL")
        first = scalar_typing(literals[0])
        return Result(first.value, "Every result is a string literal", (expression,), [first])
    premises = typed
    if literals:
        if not all(typing.binary_text for typing in known):
            return _unknown(expression, "A string literal beside a type that is not text compared by identity")
    if len(set(known)) == 1:
        return Result(known[0], "The results share a type", (expression,), premises)
    if (widest := _widest_integer(known)) is not None:
        return Result(widest, "The results are integers: the widest", (expression,), premises)
    return _unknown(expression, "The results have different types")


def _arithmetic_typing(call: ir.Call) -> Result[ir.ValueTyping | None]:
    """The type of `+`, `-`, `*` or negation over typed operands.

    A float operand makes the result a float, in every dialect. Otherwise only
    where the dialect's arithmetic is checked (`Builtin.checked`: it raises on
    overflow instead of wrapping, saturating or turning to a float): integers
    give an integer; the negation of an integer or decimal has its type; a
    decimal with an integer or decimal literal added or subtracted is a
    decimal, whose precision is not claimed (it is wider). Any other call has
    no known type.

    Provenance: DuckDB 2.0.0 `typeof` (`-x` is `x`'s type; `x + 1`, `x * 2` of an
    integer type keep it; `DECIMAL(10,2) + 1` is `DECIMAL(13,2)`); other engines
    from memory (docs/dialects.md, "Types of computed expressions")."""
    if call.function.name not in _ARITHMETIC:
        return _unknown(call)
    operands = [scalar_typing(argument) for argument in call.arguments]
    typings = [operand.value for operand in operands]
    if any(typing is None for typing in typings):
        return _unknown(call, "An operand has no known type")
    known = [typing for typing in typings if typing is not None]
    floats = [typing for typing in known if typing.floating]
    if floats:
        return Result(floats[0], "A float operand makes the arithmetic a float", (call,), operands)
    if not call.function.checked:
        return _unknown(call, "The dialect's arithmetic is not checked")
    if (widest := _widest_integer(known)) is not None:
        return Result(widest, "Checked integer arithmetic over integers is an integer", (call,), operands)
    if len(known) == 1 and known[0].exact_numeric:
        return Result(known[0], "Negation keeps the type of an integer or decimal", (call,), operands)
    if call.injective_argument is not None and all(typing.exact_numeric for typing in known):
        return Result(_DECIMAL_EXPRESSION, "A decimal with an exact literal added is a decimal", (call,), operands)
    return _unknown(call, "No type is claimed for this arithmetic")


@analysis(Stratum.COMPARISON)
def exact_comparison(left: ir.ScalarExpr, right: ir.ScalarExpr) -> Result[bool]:
    """Whether `left = right` compares their values without a lossy coercion."""
    first, second = scalar_typing(left), scalar_typing(right)
    premises = [first, second]
    match left.closed, right.closed:
        case ir.ColumnRef(column=one), ir.ColumnRef(column=other) if one is other:
            return Result(True, "A column equals itself", (left, right))
        case _:
            pass
    a, b = first.value, second.value
    if a is None or b is None:
        return Result(False, "A type is not known", (left, right), premises)
    if a.binary_text and b.binary_text:
        return Result(True, "Text compared by identity on both sides", (left, right), premises)
    if a.identity_comparison and b.identity_comparison and a.comparison_type == b.comparison_type:
        return Result(True, "Both sides have one type whose equal values are identical", (left, right), premises)
    if a.integer is not None and b.integer is not None:
        return Result(True, "Integers compare by value whatever their width", (left, right), premises)
    if _exact_literal(left, a) and b.exact_numeric or _exact_literal(right, b) and a.exact_numeric:
        return Result(True, "An integer or decimal literal compares by value with an integer or decimal", (left, right), premises)
    return Result(False, "The comparison may coerce a side and lose what tells values apart", (left, right), premises)


def _widest_integer(typings: Sequence[ir.ValueTyping]) -> ir.ValueTyping | None:
    """The widest of `typings`, when each is a signed integer type: an integer
    type is all that every engine promotes them to."""
    if not typings or any(typing.integer is None for typing in typings):
        return None
    return max(typings, key=lambda typing: typing.integer or 0)


def _exact_literal(expression: ir.ScalarExpr, typing: ir.ValueTyping) -> bool:
    """An integer or decimal literal: a number that is no float."""
    match expression:
        case ir.Constant(value=ir.LiteralValue(type="numeric")):
            return not typing.floating
        case _:
            return False


def column_ref(column: ir.OutputColumn) -> ir.ColumnRef:
    """A reference to `column`, to ask `exact_comparison` of it."""
    return ir.ColumnRef(origin=column.origin, column=column)


# Questions about one column's type -------------------------------------------


def _comparison_type(column: ir.OutputColumn) -> Result[str | None]:
    typing = _column_typing(column)
    return Result(None if typing.value is None else typing.value.comparison_type, "Comparison type", (column,), [typing])


def _identity_comparison(column: ir.OutputColumn) -> Result[bool]:
    """Whether equal values of `column` are identical."""
    typing = _column_typing(column)
    return Result(typing.value is not None and typing.value.identity_comparison, "Type compares by identity", (column,), [typing])


def _binary_text(column: ir.OutputColumn) -> Result[bool]:
    """Whether `column` is text compared by identity."""
    typing = _column_typing(column)
    return Result(typing.value is not None and typing.value.binary_text, "Text compared by identity", (column,), [typing])


def exact_numeric(column: ir.OutputColumn) -> Result[bool]:
    """Whether `column` is an integer or decimal."""
    typing = _column_typing(column)
    return Result(typing.value is not None and typing.value.exact_numeric, "An integer or decimal", (column,), [typing])


def _signed_integer(column: ir.OutputColumn) -> bool:
    typing = _column_typing(column).value
    return typing is not None and typing.integer is not None
