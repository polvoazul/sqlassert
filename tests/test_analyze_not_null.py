"""Not-null assertions and declarations, and the Non-Null Unique Sets they complete.

`/**ASSERT NOT NULL(...)**/` asks for one proof covering every listed column;
`/**DECLARE NOT NULL(...)**/` and `/**DECLARE PRIMARY KEY(...)**/` state trusted
facts and produce no assertion result of their own.
"""

from __future__ import annotations


from sqlassert import Outcome, analyze


# Declarations ----------------------------------------------------------------


# Non-Null Unique Sets --------------------------------------------------------------


# Null sources: organic NULLs and outer-join padding ---------------------------

PADDING_SCHEMA = """
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL, nickname VARCHAR);
CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id INTEGER, amount INTEGER NOT NULL);
"""


def padded_outcomes(sql: str) -> list[Outcome]:
    report = analyze(PADDING_SCHEMA + sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


def test_a_null_rejecting_where_conjunct_undoes_a_left_join_s_padding():
    """`u.id = 5` holds on no padded row, so every row left is matched and the
    right side's NOT NULL columns are non-null again; its keys are keys again."""
    assert padded_outcomes(
        """
        SELECT u.id, u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE u.id = 5 /**ASSERT NOT NULL(id, email)**/
        """
    ) == [Outcome.PROVED]
    assert padded_outcomes(
        """
        SELECT u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE u.id IS NOT NULL /**ASSERT NOT NULL(email)**/
        """
    ) == [Outcome.PROVED]


def test_without_a_null_rejecting_conjunct_padding_remains():
    assert padded_outcomes(
        """
        SELECT u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE o.id = 5 /**ASSERT NOT NULL(email)**/
        """
    ) == [Outcome.UNKNOWN]
    assert padded_outcomes(
        """
        SELECT u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE u.id IS NULL /**ASSERT NOT NULL(email)**/
        """
    ) == [Outcome.UNKNOWN]


def test_each_outer_join_pads_its_own_columns():
    nested = """
        SELECT u.email, p.amount FROM orders o
        LEFT JOIN users u ON o.user_id = u.id
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE {condition} /**ASSERT NOT NULL(amount)**/ /**ASSERT NOT NULL(email)**/
    """
    assert padded_outcomes(nested.format(condition="p.id IS NOT NULL")) == [Outcome.PROVED, Outcome.UNKNOWN]
    assert padded_outcomes(nested.format(condition="u.id IS NOT NULL")) == [Outcome.UNKNOWN, Outcome.PROVED]


def test_a_column_both_nullable_and_padded_needs_its_own_rejection():
    joined = "SELECT u.nickname FROM orders o LEFT JOIN users u ON o.user_id = u.id WHERE {condition} /**ASSERT NOT NULL(nickname)**/"
    assert padded_outcomes(joined.format(condition="u.id = 5")) == [Outcome.UNKNOWN]
    assert padded_outcomes(joined.format(condition="u.nickname IS NOT NULL")) == [Outcome.PROVED]


# Simple CASE (issue 32) ------------------------------------------------------


# Null rejection by WHERE and ON (Paulley §3.2.4, footnote 9). A conjunct
# rejects a column when it cannot be TRUE while the column is NULL: every
# comparison but the IS family, over operands that are NULL whenever the
# column is (columns, arithmetic, CAST, ...). `coalesce`, CASE and functions
# may turn a NULL into a value, so they reject nothing.


def test_a_comparison_undoes_a_left_join_s_padding():
    assert padded_outcomes(
        """
        SELECT u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE u.id > 5 /**ASSERT NOT NULL(email)**/
        """
    ) == [Outcome.PROVED]


# An OR rejects the columns every branch rejects; a branch is a little WHERE
# clause, whose AND rejects what any of its parts does.


def test_an_or_undoes_a_left_join_s_padding():
    assert padded_outcomes(
        """
        SELECT u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE (u.id > 5 AND o.id > 0) OR u.id < 0 /**ASSERT NOT NULL(email)**/
        """
    ) == [Outcome.PROVED]


# A Builtin's value is non-null when every argument it is total in is
# (`n + 1`), never NULL when it is total in none (`concat_ws(',', s)` skips a
# NULL `s`), and may be NULL regardless where it returns NULL for some values
# (`n % 0` in DuckDB, `inf - inf` in SQLite).


def test_a_builtin_over_a_padded_column_is_non_null_once_the_padding_is_rejected():
    assert padded_outcomes(
        "SELECT o.id, p.amount + 1 AS next FROM orders o LEFT JOIN payments p ON p.order_id = o.id /**ASSERT NOT NULL(next)**/"
    ) == [Outcome.UNKNOWN]
    assert padded_outcomes(
        "SELECT o.id, p.amount + 1 AS next FROM orders o LEFT JOIN payments p ON p.order_id = o.id WHERE p.id > 0 /**ASSERT NOT NULL(next)**/"
    ) == [Outcome.PROVED]
