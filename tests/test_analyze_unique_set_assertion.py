"""Unique Set Assertions: `/**ASSERT UNIQUE(...)**/` and `/**ASSERT NOT NULL UNIQUE(...)**/`
written on a Select Expression -- a Root Select, a view, a CTE, or a
subquery -- asserting that the named output columns form a Unique Set or,
for `NOT NULL UNIQUE` (alias `PRIMARY KEY`), the stricter Non-Null Unique Set.

Proof is defined purely in terms of a Relation Expression and its Output Columns:
nothing here should behave differently depending on whether the Select
Expression is a Root Select, a view, or a CTE (see
`test_the_same_assertion_proves_identically_regardless_of_attachment_site`).
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import (
    UNATTACHED_MARKER,
    UNKNOWN_ASSERTED_COLUMN,
    UNRECOGNIZED_MARKER,
)


def test_a_single_column_unique_set_assertion_on_a_view_is_proved():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR);
        CREATE VIEW active AS SELECT id, email FROM users WHERE id > 0 /**ASSERT UNIQUE(id)**/;

        SELECT * FROM active
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]


def test_a_composite_unique_set_assertion_requires_every_member():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, PRIMARY KEY (order_id, line_no));

        SELECT order_id, line_no FROM order_items /**ASSERT UNIQUE(order_id, line_no)**/
        """
    )

    assert report.proved


def test_a_composite_unique_set_assertion_missing_a_member_is_unknown():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, PRIMARY KEY (order_id, line_no));

        SELECT order_id FROM order_items /**ASSERT UNIQUE(order_id)**/
        """
    )

    assert not report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]


NON_NULL_UNIQUE_SPELLINGS = pytest.mark.parametrize("spelling", ["NOT NULL UNIQUE", "PRIMARY KEY"])


@NON_NULL_UNIQUE_SPELLINGS
def test_a_non_null_unique_assertion_is_proved_under_either_spelling(spelling):
    """`PRIMARY KEY` is a convenience alias for the canonical `NOT NULL UNIQUE`."""
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR);

        SELECT id, email FROM users /**ASSERT {spelling}(id)**/
        """
    )

    assert report.proved


@NON_NULL_UNIQUE_SPELLINGS
def test_a_non_null_unique_assertion_on_a_nullable_column_is_unknown(spelling):
    """Unique alone is not enough: the column must also be independently
    known non-null."""
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER, email VARCHAR UNIQUE);

        SELECT email FROM users /**ASSERT {spelling}(email)**/
        """
    )

    assert not report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]


def test_the_same_column_asserted_unique_instead_is_proved_even_when_nullable():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, email VARCHAR UNIQUE);

        SELECT email FROM users /**ASSERT UNIQUE(email)**/
        """
    )

    assert report.proved


def test_a_superset_of_a_smaller_proved_unique_set_is_proved_too():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);

        SELECT id, name FROM users /**ASSERT UNIQUE(id, name)**/
        """
    )

    assert report.proved


def test_multiple_assertions_on_one_select_expression_are_each_reported():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE);

        SELECT id, email FROM users /**ASSERT UNIQUE(id)**/ /**ASSERT UNIQUE(email)**/
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED, Outcome.PROVED]


def test_a_proved_assertion_on_a_view_feeds_forward_to_a_later_statement():
    """A view's proved Unique Set Assertion becomes visible to a later
    statement referencing it, exactly as an externally supplied Knowledge
    declaration would -- no separate declaration needed."""
    report = analyze(
        """
        CREATE TABLE raw (id INTEGER, region INTEGER);
        CREATE VIEW dedup AS SELECT id FROM raw GROUP BY id /**ASSERT UNIQUE(id)**/;
        CREATE TABLE sessions (user_id INTEGER);

        SELECT * FROM sessions /**ASSERT UNIQUE**/ JOIN dedup ON sessions.user_id = dedup.id
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED, Outcome.PROVED]


def test_a_root_select_with_its_own_join_and_projection_can_be_asserted():
    """The Root Select's own tail (WHERE/GROUP BY/DISTINCT/QUALIFY/projection)
    is modeled the same way a CTE or view body's already is, including
    directly on top of a proven-unique join."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR);
        CREATE TABLE sessions (user_id INTEGER PRIMARY KEY, ts TIMESTAMP);

        SELECT sessions.user_id
        FROM sessions
        /**ASSERT UNIQUE**/ JOIN users ON sessions.user_id = users.id
        /**ASSERT UNIQUE(user_id)**/
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED, Outcome.PROVED]


def test_a_cte_can_carry_a_unique_set_assertion():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);

        WITH active AS (SELECT id FROM users WHERE id > 0 /**ASSERT UNIQUE(id)**/)
        SELECT * FROM active
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]


def test_a_from_subquery_can_carry_a_unique_set_assertion():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);

        SELECT * FROM (SELECT id FROM users WHERE id > 0 /**ASSERT UNIQUE(id)**/) AS active
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]


def test_the_same_assertion_proves_identically_regardless_of_attachment_site():
    """The assertion's IR and engine representation is defined purely in
    terms of a Relation Expression and its Output Columns -- a view-attached,
    CTE-attached, and Root-Select-attached assertion over the same underlying
    structure must be indistinguishable to the engine."""

    def outcomes(sql: str) -> list[Outcome]:
        report = analyze(sql)
        return [a.outcome for a in report.assertions]

    declaration = "CREATE TABLE t (a INTEGER PRIMARY KEY, b INTEGER);"
    root = outcomes(f"{declaration} SELECT a, b FROM t /**ASSERT UNIQUE(a)**/")
    view = outcomes(f"{declaration} CREATE VIEW v AS SELECT a, b FROM t /**ASSERT UNIQUE(a)**/; SELECT * FROM v")
    cte = outcomes(f"{declaration} WITH c AS (SELECT a, b FROM t /**ASSERT UNIQUE(a)**/) SELECT * FROM c")

    assert root == view == cte == [Outcome.PROVED]


def test_an_empty_column_list_is_reported_rather_than_silently_accepted():
    report = analyze("CREATE TABLE t (a INTEGER); SELECT a FROM t /**ASSERT UNIQUE()**/")

    assert report.assertions == ()
    assert [d.code for d in report.diagnostics] == [UNATTACHED_MARKER]


def test_a_self_duplicating_column_list_is_reported_rather_than_silently_accepted():
    report = analyze("CREATE TABLE t (a INTEGER); SELECT a FROM t /**ASSERT UNIQUE(a, a)**/")

    assert report.assertions == ()
    assert [d.code for d in report.diagnostics] == [UNATTACHED_MARKER]


def test_a_typo_in_the_keyword_is_reported_as_an_unrecognized_marker():
    report = analyze("CREATE TABLE t (a INTEGER); SELECT a FROM t /**ASSERT UNIQE(a)**/")

    assert report.assertions == ()
    assert [d.code for d in report.diagnostics] == [UNRECOGNIZED_MARKER]


def test_a_marker_on_a_select_with_a_join_inside_a_cte_body_is_analyzed():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        WITH enriched AS (
            SELECT users.id
            FROM users
            JOIN sessions ON sessions.user_id = users.id
            /**ASSERT UNIQUE(id)**/
        )
        SELECT * FROM enriched
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert not report.diagnostics


def test_report_facts_includes_unique_sets_earned_through_a_proved_assertion():
    report = analyze(
        """
        CREATE TABLE raw (id INTEGER, region INTEGER);
        CREATE VIEW dedup AS SELECT id FROM raw GROUP BY id /**ASSERT UNIQUE(id)**/;

        SELECT * FROM dedup
        """
    )

    assert report.facts.is_unique("dedup", ["id"])


def test_assertions_cannot_prove_themselves_or_each_other():
    report = analyze(
        """
        CREATE TABLE t (id INTEGER, name VARCHAR);
        SELECT id, name FROM t /**ASSERT UNIQUE(id)**/ /**ASSERT UNIQUE(id, name)**/
        """
    )

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN, Outcome.UNKNOWN]


def test_a_subset_of_a_known_composite_unique_set_is_not_proved_unique():
    report = analyze(
        """
        CREATE TABLE t (id INTEGER, name VARCHAR, region VARCHAR, UNIQUE (id, region));
        SELECT id, name FROM t /**ASSERT UNIQUE(id, name)**/
        """
    )

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]
