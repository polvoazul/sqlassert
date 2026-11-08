"""Exactly One Joins: every left row matches exactly one right row.

Three halves, each reported. At most one match is a Many To One Join, which further
conjuncts only strengthen. At least one match needs every conjunct to be an
equality between a left and a right column, and Referential Coverage of the
left columns, as one tuple, onto the right columns they are paired with: a
row of the right input carrying every left value then satisfies the whole ON.
Coverage of each column separately does not give a row matching all of them.
Any other conjunct may reject the covering row, so matching stays unknown.
Non-null left columns: a null equals nothing, and `MATCH SIMPLE` exempts a
row with any null referencing component. The input relations may be derived:
coverage, uniqueness and nullability each follow their own propagation, and
coverage onto a narrowed right input must hold on its own.
"""

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, analysis, all_of, unknown
from sqlassert.rules.foreign_key import referential_coverage
from sqlassert.rules.nullability import non_null_columns
from sqlassert.rules.join_cardinality import many_to_one_join


@analysis(Stratum.UNIQUE_JOIN)
def exactly_one_join(join: ir.Join) -> Result[bool]:
    # As for a Many To One Join, the left row's matches decide it, except that an
    # anti join drops every matched row.
    if join.kind == "anti":
        return unknown("An anti join drops the rows it matches", join)
    at_most_one = _half("At most one match: the right predicate columns cover a Unique Set", (join,), many_to_one_join(join))
    pairs, others = _cross_equalities(join)
    if not pairs:
        return at_most_one & unknown(
            "At least one match needs an equality between a left and a right column", join
        )
    lefts = tuple(left for left, _ in pairs)
    rights = tuple(right for _, right in pairs)
    coverage = referential_coverage(lefts, rights)
    if others:
        coverage = all_of((coverage, *(
            unknown("Only column-to-column equalities between the inputs are known to keep a covering row", conjunct)
            for conjunct in others
        )))
    return all_of((
        at_most_one,
        _half("At least one match: the left columns reference the right predicate columns", (*lefts, *rights), coverage),
        _half(
            "Non-null left columns: a null left value matches no row", lefts, non_null_columns(frozenset(lefts))
        ),
    ))


def _half(reason: str, subjects: tuple[object, ...], evidence: Result[bool]) -> Result[bool]:
    return Result(evidence.value, reason, subjects, [evidence], kind="all")


def _cross_equalities(
    join: ir.Join,
) -> tuple[list[tuple[ir.OutputColumn, ir.OutputColumn]], list[ir.ScalarExpr]]:
    """The (left, right) column pairs of ON's cross-input equalities, and every other conjunct."""
    pairs: list[tuple[ir.OutputColumn, ir.OutputColumn]] = []
    others: list[ir.ScalarExpr] = []
    for conjunct in join.conjuncts:
        match conjunct:
            case ir.Equality(left=ir.ColumnRef(column=first), right=ir.ColumnRef(column=second)):
                pair = next((
                    (left, right) for left, right in ((first, second), (second, first))
                    if left in join.left.output_columns and right in join.right.output_columns
                ), None)
                if pair is None:
                    others.append(conjunct)  # Both columns on one side: a filter.
                elif pair not in pairs:
                    pairs.append(pair)
            case _:
                others.append(conjunct)  # A constant, expression, or opaque operand fixes no column pairing.
    return pairs, others
