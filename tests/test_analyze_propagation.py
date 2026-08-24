"""Uniqueness propagated through Filter and Project, and Relation Instance
identity for aliases and self-joins.

Every case here joins against a derived table (`(SELECT ...) AS alias`) or a
second occurrence of an already-declared table, rather than a bare table, so
these are the scenarios `test_analyze_unique_join.py` does not cover.
"""

from __future__ import annotations

from sqlassert import Outcome, analyze


def test_a_filter_preserves_an_existing_unique_set():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN (SELECT * FROM users WHERE id > 0) AS u
            ON sessions.user_id = u.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert report.assertions[0].proving_unique_set == ("id",)


def test_projecting_every_member_of_a_unique_set_preserves_it():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, sku VARCHAR, PRIMARY KEY (order_id, line_no));
        CREATE TABLE orders (id INTEGER);

        SELECT *
        FROM orders
        /**UNIQUE**/ JOIN (SELECT order_id, line_no FROM order_items) AS oi
            ON orders.id = oi.order_id AND orders.id = oi.line_no
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert report.assertions[0].proving_unique_set == ("order_id", "line_no")


def test_renaming_a_projected_unique_column_maps_the_unique_set_to_the_output_name():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN (SELECT id AS uid FROM users) AS u
            ON sessions.user_id = u.uid
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert report.assertions[0].proving_unique_set == ("uid",)


def test_dropping_a_required_key_member_is_unknown():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, sku VARCHAR, PRIMARY KEY (order_id, line_no));
        CREATE TABLE orders (id INTEGER);

        SELECT *
        FROM orders
        /**UNIQUE**/ JOIN (SELECT order_id FROM order_items) AS oi
            ON orders.id = oi.order_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_replacing_a_key_member_with_a_computed_expression_is_unknown():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN (SELECT id + 0 AS id FROM users) AS u
            ON sessions.user_id = u.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_two_aliases_of_the_same_relation_are_separate_instances():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, manager_id INTEGER);

        SELECT *
        FROM users a
        /**UNIQUE**/ JOIN users b
            ON a.manager_id = b.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_self_join_stays_unknown_independently_of_a_valid_join_elsewhere():
    """Two occurrences of `users`, one validly joined on its unique column and
    one on a non-unique column: proof of the first must not leak into the
    second."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, manager_id INTEGER, name VARCHAR);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users a
            ON sessions.user_id = a.id
        /**UNIQUE**/ JOIN users b
            ON a.manager_id = b.name
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED, Outcome.UNKNOWN]
