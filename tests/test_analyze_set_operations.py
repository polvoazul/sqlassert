"""UNION, INTERSECT, and EXCEPT at the root of a SQL Program.

Each arm of a set operation is lowered independently, exactly as if it were
its own top-level query, so a marked join in either arm is analyzed on its
own relation rather than reported as unanalyzed. What is deliberately not
modeled is the set operation's own row-set semantics: `UNION`/`INTERSECT`/
`EXCEPT` (without `ALL`) dedup their combined output, which would itself be a
Unique Set over every output column -- the `ALL` form of each keeps
duplicates instead, and either way that is a proof in its own right, not
exercised here.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import UNANALYZED_ASSERTION


@pytest.fixture
def users_and_two_sources() -> str:
    """`users(id)` unique; `sessions(user_id)` and `orders(user_id)` each
    join against it, one per arm of a set operation."""
    return """
    CREATE TABLE users (id INTEGER PRIMARY KEY);
    CREATE TABLE sessions (user_id INTEGER);
    CREATE TABLE orders (user_id INTEGER);
    """


def test_a_join_in_each_union_all_arm_is_proved_independently(users_and_two_sources):
    report = analyze(
        users_and_two_sources
        + """
        SELECT * FROM sessions /**UNIQUE**/ JOIN users ON sessions.user_id = users.id
        UNION ALL
        SELECT * FROM orders /**UNIQUE**/ JOIN users ON orders.user_id = users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [
        Outcome.PROVED,
        Outcome.PROVED,
    ]
    assert report.proved is True


@pytest.mark.parametrize("operator", ["UNION", "INTERSECT", "EXCEPT"])
def test_a_join_in_either_arm_is_proved_for_every_set_operator(users_and_two_sources, operator: str):
    """UNION, INTERSECT, and EXCEPT share the same lowering path; only ALL vs.
    implicit-distinct differs between them, and that distinction is exactly
    the row-set semantics this slice does not model."""
    report = analyze(
        users_and_two_sources
        + f"""
        SELECT * FROM sessions /**UNIQUE**/ JOIN users ON sessions.user_id = users.id
        {operator}
        SELECT * FROM orders /**UNIQUE**/ JOIN users ON orders.user_id = users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [
        Outcome.PROVED,
        Outcome.PROVED,
    ], operator


def test_one_arm_unproved_does_not_affect_the_other_arm_or_the_overall_result(users_and_two_sources):
    report = analyze(
        users_and_two_sources
        + """
        SELECT * FROM sessions /**UNIQUE**/ JOIN users ON sessions.user_id = users.id
        UNION ALL
        SELECT * FROM orders /**UNIQUE**/ JOIN users ON orders.user_id = orders.user_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [
        Outcome.PROVED,
        Outcome.UNKNOWN,
    ]
    assert report.proved is False


def test_a_chained_three_way_union_lowers_every_arm(users_and_two_sources):
    report = analyze(
        users_and_two_sources
        + """
        CREATE TABLE carts (user_id INTEGER);

        SELECT * FROM sessions /**UNIQUE**/ JOIN users ON sessions.user_id = users.id
        UNION ALL
        SELECT * FROM orders /**UNIQUE**/ JOIN users ON orders.user_id = users.id
        UNION ALL
        SELECT * FROM carts /**UNIQUE**/ JOIN users ON carts.user_id = users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [
        Outcome.PROVED,
        Outcome.PROVED,
        Outcome.PROVED,
    ]
    assert report.proved is True


def test_a_cte_whose_body_is_a_union_stays_unanalyzed(users_and_two_sources):
    """A set operation is only lowered as the program's own root query. A CTE
    body that is a set operation is a different slice, and stays conservative: `combined`
    lowers to an OpaqueRelation with no Unique Set, so the join against it is
    UNKNOWN -- not, as it would be for a plain unsupported CTE reference,
    unanalyzed, since the marker itself is at the root query's join, which
    this slice does reach."""
    report = analyze(
        users_and_two_sources
        + """
        WITH combined AS (
            SELECT user_id FROM sessions
            UNION ALL
            SELECT user_id FROM orders
        )
        SELECT * FROM users /**UNIQUE**/ JOIN combined ON users.id = combined.user_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert not report.diagnostics


def test_a_join_inside_a_from_subquery_wrapping_a_union_is_still_unanalyzed(users_and_two_sources):
    """A set operation used as a FROM subquery is a third slice again: the
    marked join lives inside a part of the program this
    analysis does not reach at all, so it is reported rather than silently
    dropped, the same as a join inside a CTE body."""
    report = analyze(
        users_and_two_sources
        + """
        SELECT *
        FROM (
            SELECT * FROM sessions /**UNIQUE**/ JOIN users ON sessions.user_id = users.id
            UNION ALL
            SELECT * FROM orders JOIN users ON orders.user_id = users.id
        ) AS combined
        """
    )

    assert report.assertions == ()
    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNANALYZED_ASSERTION]
