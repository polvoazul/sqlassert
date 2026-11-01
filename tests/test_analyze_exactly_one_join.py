"""Exactly One Join Assertions: `/**ASSERT EXACTLY ONE**/` before a JOIN.

Every left row matches exactly one right row. The at-most-one half is a Unique
Join: the right-side predicate column covers a Unique Set. The at-least-one half
needs a non-null left column with Referential Coverage onto the exact right
relation and predicate column. Only inner and left joins with one direct
column-to-column equality are supported here.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import UNANALYZED_ASSERTION, UNATTACHED_MARKER, UNRECOGNIZED_MARKER

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
CREATE TABLE accounts (user_id INTEGER PRIMARY KEY);
CREATE TABLE sessions (user_id INTEGER);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    buyer INTEGER REFERENCES users(id),
    plain INTEGER NOT NULL
);
CREATE TABLE members (user_id INTEGER NOT NULL REFERENCES accounts(user_id));
CREATE TABLE events (user_id INTEGER);
"""


def report_of(sql: str):
    return analyze(f"{SCHEMA} {sql}")


def outcomes(sql: str) -> list[Outcome]:
    report = report_of(sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


def explanation(sql: str) -> str:
    report = report_of(sql)
    assert not report.diagnostics, report.diagnostics
    evidence = report.assertions[0].evidence
    assert evidence is not None
    return evidence.explain()


PROVED = [
    ("inner join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id"),
    ("left join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ LEFT JOIN users ON orders.user_id = users.id"),
    ("operands reversed", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON users.id = orders.user_id"),
    ("aliased tables", "SELECT * FROM orders AS o /**ASSERT EXACTLY ONE**/ JOIN users AS u ON o.user_id = u.id"),
    ("using", "SELECT * FROM members /**ASSERT EXACTLY ONE**/ JOIN accounts USING (user_id)"),
]


@pytest.mark.parametrize(("label", "sql"), PROVED)
def test_a_simple_exactly_one_join_is_proved(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


UNKNOWN = [
    ("nullable left column", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.buyer = users.id"),
    ("no foreign key", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.plain = users.id"),
    ("right column not unique", "SELECT * FROM users /**ASSERT EXACTLY ONE**/ JOIN orders ON users.id = orders.user_id"),
    (
        "additional predicate",
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id AND users.name = 'x'",
    ),
    (
        "additional equality",
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id AND orders.id = users.id",
    ),
    ("equality to a constant", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON users.id = 1"),
    ("non-equality predicate", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id < users.id"),
    ("right join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ RIGHT JOIN users ON orders.user_id = users.id"),
    ("full join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ FULL JOIN users ON orders.user_id = users.id"),
    (
        "foreign key onto a narrowed relation",
        "WITH active AS (SELECT * FROM users WHERE name = 'x') "
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN active ON orders.user_id = active.id",
    ),
    (
        "foreign key onto another relation",
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN accounts ON orders.user_id = accounts.user_id",
    ),
]


@pytest.mark.parametrize(("label", "sql"), UNKNOWN)
def test_an_unproved_exactly_one_join_is_unknown(label: str, sql: str):
    assert outcomes(sql) == [Outcome.UNKNOWN], label


def test_a_nullable_left_column_is_reported_as_the_only_missing_evidence():
    explained = explanation(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.buyer = users.id"
    )

    assert "PROVED: At most one match" in explained
    assert "PROVED: At least one match" in explained
    assert "UNKNOWN: Non-null left column" in explained


def test_a_missing_foreign_key_is_reported_as_missing_guaranteed_matching():
    explained = explanation(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.plain = users.id"
    )

    assert "PROVED: At most one match" in explained
    assert "UNKNOWN: At least one match" in explained
    assert "PROVED: Non-null left column" in explained


def test_every_missing_half_is_reported_separately():
    explained = explanation(
        "SELECT * FROM events /**ASSERT EXACTLY ONE**/ JOIN sessions ON events.user_id = sessions.user_id"
    )

    assert "UNKNOWN: At most one match" in explained
    assert "UNKNOWN: At least one match" in explained
    assert "UNKNOWN: Non-null left column" in explained


def test_an_additional_predicate_is_named_in_the_explanation():
    explained = explanation(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id AND users.name = 'x'"
    )

    assert "single column-to-column equality" in explained


def test_referential_coverage_onto_a_non_unique_column_still_needs_a_unique_right_column():
    # Coverage alone guarantees a match, not that the match is unique.
    sql = """
        CREATE TABLE visits (user_id INTEGER NOT NULL REFERENCES sessions(user_id));
        SELECT * FROM visits /**ASSERT EXACTLY ONE**/ JOIN sessions ON visits.user_id = sessions.user_id
    """
    explained = explanation(sql)

    assert "UNKNOWN: At most one match" in explained
    assert "PROVED: At least one match" in explained
    assert "PROVED: Non-null left column" in explained


def test_the_assertion_locates_its_join():
    report = report_of(
        "SELECT * FROM orders\n/**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id"
    )

    origin = report.assertions[0].origin
    assert origin.line == SCHEMA.count("\n") + 2
    assert "JOIN users" in origin.detail


def test_exactly_one_and_unique_markers_mark_separate_joins():
    assert outcomes(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id "
        "/**ASSERT UNIQUE**/ JOIN accounts ON orders.plain = accounts.user_id"
    ) == [Outcome.PROVED, Outcome.PROVED]


def test_an_exactly_one_marker_in_an_unreferenced_cte_is_a_diagnostic():
    report = report_of(
        "WITH enriched AS (SELECT orders.id FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id) "
        "SELECT * FROM orders"
    )

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNANALYZED_ASSERTION]
    assert "exactly one join assertion" in report.diagnostics[0].message


def test_an_exactly_one_marker_not_before_a_join_is_unattached():
    report = report_of("SELECT * FROM orders /**ASSERT EXACTLY ONE**/ WHERE id = 1")

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNATTACHED_MARKER]
    assert "exactly one join marker" in report.diagnostics[0].message


def test_a_mistyped_exactly_one_marker_is_reported_with_the_marker_list():
    report = report_of("SELECT * FROM orders /**ASSERT EXACTLY 1**/ JOIN users ON orders.user_id = users.id")

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNRECOGNIZED_MARKER]
    assert "/**ASSERT EXACTLY ONE**/" in report.diagnostics[0].message


def test_a_proved_exactly_one_left_join_pads_no_right_column():
    # Every left row has its match, so the LEFT JOIN never null-extends.
    proved = """
        SELECT o.id, u.id AS matched_id FROM orders AS o
        /**ASSERT EXACTLY ONE**/ LEFT JOIN users AS u ON u.id = o.user_id
        /**ASSERT NOT NULL(matched_id)**/
    """
    # A nullable buyer may match nothing, so the right side may be padded.
    padded = """
        SELECT o.id, u.id AS matched_id FROM orders AS o
        /**ASSERT EXACTLY ONE**/ LEFT JOIN users AS u ON u.id = o.buyer
        /**ASSERT NOT NULL(matched_id)**/
    """

    assert outcomes(proved) == [Outcome.PROVED, Outcome.PROVED]
    assert outcomes(padded) == [Outcome.UNKNOWN, Outcome.UNKNOWN]


def test_a_proved_exactly_one_join_keeps_the_left_inputs_key():
    assert outcomes("""
        SELECT o.id, u.name FROM orders AS o
        /**ASSERT EXACTLY ONE**/ JOIN users AS u ON u.id = o.user_id
        /**ASSERT NOT NULL UNIQUE(id)**/
    """) == [Outcome.PROVED, Outcome.PROVED]
