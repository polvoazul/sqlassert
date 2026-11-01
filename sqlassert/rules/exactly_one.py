"""Exactly One Joins: every left row matches exactly one right row.

Two halves, reported separately. At most one match is a Unique Join. At least
one match needs the left predicate column to be non-null, since a null equals
nothing, and Referential Coverage from it onto the exact right predicate
column. Only one direct column-to-column equality is supported: any further
conjunct could remove the guaranteed match.
"""

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, analysis, all_of, unknown
from sqlassert.rules.foreign_key import referential_coverage
from sqlassert.rules.nullability import non_null_column
from sqlassert.rules.unique_join import unique_join


@analysis(Stratum.UNIQUE_JOIN)
def exactly_one_join(join: ir.Join) -> Result[bool]:
    if join.kind not in {"inner", "left"}:
        return unknown("Exactly-one inference supports inner and left joins", join)
    pair = _direct_equality(join)
    if pair is None:
        return unknown(
            "Exactly-one inference needs a predicate that is a single column-to-column equality between the inputs",
            join,
        )
    left, right = pair
    return all_of((
        _half("At most one match: the right predicate column covers a Unique Set", (join,), unique_join(join)),
        _half(
            "At least one match: the left column references the right predicate column",
            (left, right),
            referential_coverage((left,), (right,)),
        ),
        _half("Non-null left column: a null left value matches no row", (left,), non_null_column(left)),
    ))


def _half(reason: str, subjects: tuple[object, ...], evidence: Result[bool]) -> Result[bool]:
    return Result(evidence.value, reason, subjects, [evidence], kind="all")


def _direct_equality(join: ir.Join) -> tuple[ir.OutputColumn, ir.OutputColumn] | None:
    """The (left, right) columns of a predicate that is exactly one equality between them."""
    if join.residual_conjuncts or len(join.equalities) != 1:
        return None
    equality, = join.equalities
    match equality.left, equality.right:
        case ir.ColumnRef(column=first), ir.ColumnRef(column=second):
            for left, right in ((first, second), (second, first)):
                if left in join.left.output_columns and right in join.right.output_columns:
                    return left, right
            return None
        case _:
            return None  # A constant, expression, or opaque operand fixes no column pairing.
