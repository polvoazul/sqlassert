"""CTE and FROM-subquery plans lowered recursively.

`test_analyze_propagation.py` already covers the narrow single-table FROM
subquery. Every case here needs the recursive part of #5: a CTE actually
lowered into a relational subplan, and nesting of CTEs and FROM subqueries
inside one another, so a Unique Set survives to prove a join several layers
out.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import UNANALYZED_ASSERTION


@pytest.fixture
def users_and_sessions() -> str:
    """`users(id)` unique, joined against by `sessions(user_id)`.

    Every case below is a variation on how a CTE or FROM subquery gets from
    one to the other, so they share this one declaration.
    """
    return """
    CREATE TABLE users (id INTEGER PRIMARY KEY);
    CREATE TABLE sessions (user_id INTEGER);
    """


def test_a_cte_preserving_a_unique_set_proves_a_join_in_the_root_select(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH active_users AS (SELECT id FROM users WHERE id > 0)
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN active_users
            ON sessions.user_id = active_users.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_from_subquery_over_a_cte_preserves_its_unique_set(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH active_users AS (SELECT id FROM users)
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN (SELECT * FROM active_users) AS u
            ON sessions.user_id = u.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_cte_built_from_another_cte_preserves_its_unique_set(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH base AS (SELECT id FROM users),
             derived AS (SELECT id FROM base)
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN derived
            ON sessions.user_id = derived.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_cte_containing_a_from_subquery_preserves_its_unique_set(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH wrapped AS (SELECT * FROM (SELECT id FROM users) AS u)
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN wrapped
            ON sessions.user_id = wrapped.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_projection_alias_inside_a_cte_maps_the_unique_set_to_the_outer_column(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH renamed AS (SELECT id AS uid FROM users)
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN renamed
            ON sessions.user_id = renamed.uid
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("uid",)


def test_a_cte_dropping_a_required_composite_key_member_is_unknown():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, sku VARCHAR, PRIMARY KEY (order_id, line_no));
        CREATE TABLE orders (id INTEGER);

        WITH partial AS (SELECT order_id FROM order_items)
        SELECT *
        FROM orders
        /**UNIQUE**/ JOIN partial
            ON orders.id = partial.order_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_scalar_subquery_in_a_cte_projection_is_unsupported_and_never_guessed(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH computed AS (SELECT (SELECT 1) AS id FROM users)
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN computed
            ON sessions.user_id = computed.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_correlated_subquery_in_a_cte_filter_does_not_block_the_proof(users_and_sessions):
    """A WHERE clause only ever removes rows, so a Filter carries forward its
    input's Unique Sets regardless of what its predicate says -- correlated
    subquery included. Filter has no predicate to understand in the first
    place (see `ir/model.py`), so this is sound without modeling the
    subquery at all.
    """
    report = analyze(
        users_and_sessions
        + """
        WITH filtered AS (
            SELECT id FROM users WHERE id = (SELECT MAX(user_id) FROM sessions s WHERE s.user_id = users.id)
        )
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN filtered
            ON sessions.user_id = filtered.id
        """
    )

    assert report.proved


def test_a_correlated_subquery_projected_as_a_key_member_is_unsupported_and_never_guessed(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH computed AS (
            SELECT (SELECT MAX(user_id) FROM sessions s WHERE s.user_id = users.id) AS id
            FROM users
        )
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN computed
            ON sessions.user_id = computed.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_join_inside_a_cte_body_is_not_modeled_and_reports_a_diagnostic(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH enriched AS (
            SELECT users.id
            FROM users
            /**UNIQUE**/ JOIN sessions ON sessions.user_id = users.id
        )
        SELECT * FROM enriched
        """
    )

    assert report.assertions == ()
    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNANALYZED_ASSERTION]


def test_a_self_referencing_cte_without_recursive_does_not_hang_and_is_unknown(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        WITH broken AS (SELECT id FROM broken)
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN broken
            ON sessions.user_id = broken.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
