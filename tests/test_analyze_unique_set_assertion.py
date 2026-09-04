"""Unique Set Assertions: `/**UNIQUE(...)**/` and `/**PRIMARY KEY(...)**/`
written on a Select Expression -- a Root Select, a view, a CTE, or a
subquery -- asserting that the named output columns form a Unique Set or,
for the `PRIMARY KEY` spelling, the stricter non-null Candidate Key.

Proof is defined purely in terms of a Relation Expression and its Output Columns:
nothing here should behave differently depending on whether the Select
Expression is a Root Select, a view, or a CTE (see
`test_the_same_assertion_proves_identically_regardless_of_attachment_site`).
"""

from __future__ import annotations

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import (
    UNANALYZED_ASSERTION,
    UNATTACHED_MARKER,
    UNKNOWN_ASSERTED_COLUMN,
    UNRECOGNIZED_MARKER,
)


def test_a_single_column_unique_set_assertion_on_a_view_is_proved():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR);
        CREATE VIEW active AS SELECT id, email FROM users WHERE id > 0 /**UNIQUE(id)**/;

        SELECT * FROM active
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]


def test_a_composite_unique_set_assertion_requires_every_member():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, PRIMARY KEY (order_id, line_no));

        SELECT order_id, line_no FROM order_items /**UNIQUE(order_id, line_no)**/
        """
    )

    assert report.proved


def test_a_composite_unique_set_assertion_missing_a_member_is_unknown():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, PRIMARY KEY (order_id, line_no));

        SELECT order_id FROM order_items /**UNIQUE(order_id)**/
        """
    )

    assert not report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]


def test_a_primary_key_flavored_assertion_is_identified_as_a_candidate_key():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR);

        SELECT id, email FROM users /**PRIMARY KEY(id)**/
        """
    )

    assert report.proved


def test_a_primary_key_flavored_assertion_on_a_nullable_column_is_unknown():
    """Unique alone is not enough for the `PRIMARY KEY` spelling: it also
    requires the column to be independently known non-null."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, email VARCHAR UNIQUE);

        SELECT email FROM users /**PRIMARY KEY(email)**/
        """
    )

    assert not report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]


def test_the_same_column_asserted_unique_instead_is_proved_even_when_nullable():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, email VARCHAR UNIQUE);

        SELECT email FROM users /**UNIQUE(email)**/
        """
    )

    assert report.proved


def test_a_superset_of_a_smaller_proved_unique_set_is_proved_too():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);

        SELECT id, name FROM users /**UNIQUE(id, name)**/
        """
    )

    assert report.proved


def test_multiple_assertions_on_one_select_expression_are_each_reported():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE);

        SELECT id, email FROM users /**UNIQUE(id)**/ /**UNIQUE(email)**/
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
        CREATE VIEW dedup AS SELECT id FROM raw GROUP BY id /**UNIQUE(id)**/;
        CREATE TABLE sessions (user_id INTEGER);

        SELECT * FROM sessions /**UNIQUE**/ JOIN dedup ON sessions.user_id = dedup.id
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
        /**UNIQUE**/ JOIN users ON sessions.user_id = users.id
        /**UNIQUE(user_id)**/
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED, Outcome.PROVED]


def test_a_cte_can_carry_a_unique_set_assertion():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);

        WITH active AS (SELECT id FROM users WHERE id > 0 /**UNIQUE(id)**/)
        SELECT * FROM active
        """
    )

    assert report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]


def test_a_from_subquery_can_carry_a_unique_set_assertion():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);

        SELECT * FROM (SELECT id FROM users WHERE id > 0 /**UNIQUE(id)**/) AS active
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
    root = outcomes(f"{declaration} SELECT a, b FROM t /**UNIQUE(a)**/")
    view = outcomes(f"{declaration} CREATE VIEW v AS SELECT a, b FROM t /**UNIQUE(a)**/; SELECT * FROM v")
    cte = outcomes(f"{declaration} WITH c AS (SELECT a, b FROM t /**UNIQUE(a)**/) SELECT * FROM c")

    assert root == view == cte == [Outcome.PROVED]


def test_an_empty_column_list_is_reported_rather_than_silently_accepted():
    report = analyze("CREATE TABLE t (a INTEGER); SELECT a FROM t /**UNIQUE()**/")

    assert report.assertions == ()
    assert [d.code for d in report.diagnostics] == [UNATTACHED_MARKER]


def test_a_self_duplicating_column_list_is_reported_rather_than_silently_accepted():
    report = analyze("CREATE TABLE t (a INTEGER); SELECT a FROM t /**UNIQUE(a, a)**/")

    assert report.assertions == ()
    assert [d.code for d in report.diagnostics] == [UNATTACHED_MARKER]


def test_a_typo_in_the_keyword_is_reported_as_an_unrecognized_marker():
    report = analyze("CREATE TABLE t (a INTEGER); SELECT a FROM t /**UNIQE(a)**/")

    assert report.assertions == ()
    assert [d.code for d in report.diagnostics] == [UNRECOGNIZED_MARKER]


def test_a_marker_on_a_join_inside_a_cte_body_is_unanalyzed_not_dropped():
    """A CTE body containing a JOIN is still not modeled (unrelated to this
    marker) -- the marker on it must be reported, not silently lost."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        WITH enriched AS (
            SELECT users.id
            FROM users
            JOIN sessions ON sessions.user_id = users.id
            /**UNIQUE(id)**/
        )
        SELECT * FROM enriched
        """
    )

    assert report.assertions == ()
    assert [d.code for d in report.diagnostics] == [UNANALYZED_ASSERTION]


def test_report_facts_includes_unique_sets_earned_through_a_proved_assertion():
    report = analyze(
        """
        CREATE TABLE raw (id INTEGER, region INTEGER);
        CREATE VIEW dedup AS SELECT id FROM raw GROUP BY id /**UNIQUE(id)**/;

        SELECT * FROM dedup
        """
    )

    assert report.facts.is_unique("dedup", ["id"])
