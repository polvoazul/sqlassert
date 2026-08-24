"""Create View declarations lowered as named Relation Definitions.

A view's body lowers through the same recursive path a CTE or FROM subquery
uses (`IrParser._lower_table_reference`, built on #5's `_lower_nested_select`),
but unlike a CTE it is declared up front and visible from anywhere in the
program -- so it supports forward references, nesting, reuse, duplicate
detection, and cycle detection that a CTE's local WITH scope does not need.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import DUPLICATE_DECLARATION, RECURSIVE_VIEW_DEFINITION


@pytest.fixture
def users_and_sessions() -> str:
    """`users(id)` unique, joined against by `sessions(user_id)`."""
    return """
    CREATE TABLE users (id INTEGER PRIMARY KEY);
    CREATE TABLE sessions (user_id INTEGER);
    """


def test_a_view_preserving_a_unique_set_proves_a_join_in_the_root_select(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        CREATE VIEW active_users AS SELECT id FROM users WHERE id > 0;

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN active_users
            ON sessions.user_id = active_users.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_view_may_reference_a_relation_declared_later_in_the_program():
    report = analyze(
        """
        CREATE VIEW active_users AS SELECT id FROM users WHERE id > 0;
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN active_users
            ON sessions.user_id = active_users.id
        """
    )

    assert report.proved


def test_a_view_built_from_another_view_preserves_its_unique_set(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        CREATE VIEW base AS SELECT id FROM users;
        CREATE VIEW derived AS SELECT id FROM base;

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN derived
            ON sessions.user_id = derived.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_projection_alias_inside_a_view_maps_the_unique_set_to_the_outer_column(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        CREATE VIEW renamed AS SELECT id AS uid FROM users;

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN renamed
            ON sessions.user_id = renamed.uid
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("uid",)


def test_reusing_the_same_view_twice_gives_each_occurrence_its_own_identity(users_and_sessions):
    """Two references to one view must behave like two aliases of a table: each
    gets its own Relation Instance, so a self-join through the view proves
    correctly instead of the two occurrences being confused for one another."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, manager_id INTEGER);
        CREATE VIEW active_users AS SELECT id, manager_id FROM users WHERE id > 0;

        SELECT *
        FROM active_users a
        /**UNIQUE**/ JOIN active_users b
            ON a.manager_id = b.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_view_dropping_a_required_composite_key_member_is_unknown():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, sku VARCHAR, PRIMARY KEY (order_id, line_no));
        CREATE TABLE orders (id INTEGER);
        CREATE VIEW partial AS SELECT order_id FROM order_items;

        SELECT *
        FROM orders
        /**UNIQUE**/ JOIN partial
            ON orders.id = partial.order_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_duplicate_view_declaration_is_an_explicit_diagnostic():
    report = analyze(
        """
        CREATE VIEW dup AS SELECT 1 AS id;
        CREATE VIEW dup AS SELECT 2 AS id;

        SELECT * FROM dup
        """
    )

    assert [diagnostic.code for diagnostic in report.diagnostics] == [DUPLICATE_DECLARATION]


def test_a_view_duplicating_a_table_name_is_an_explicit_diagnostic():
    report = analyze(
        """
        CREATE TABLE dup (id INTEGER);
        CREATE VIEW dup AS SELECT 1 AS id;

        SELECT * FROM dup
        """
    )

    assert [diagnostic.code for diagnostic in report.diagnostics] == [DUPLICATE_DECLARATION]


def test_a_self_referencing_view_is_a_cycle_diagnostic_and_does_not_hang(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        CREATE VIEW broken AS SELECT id FROM broken;

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN broken
            ON sessions.user_id = broken.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert [diagnostic.code for diagnostic in report.diagnostics] == [RECURSIVE_VIEW_DEFINITION]


def test_a_mutual_view_cycle_is_a_cycle_diagnostic_and_does_not_hang(users_and_sessions):
    report = analyze(
        users_and_sessions
        + """
        CREATE VIEW a_view AS SELECT id FROM b_view;
        CREATE VIEW b_view AS SELECT id FROM a_view;

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN a_view
            ON sessions.user_id = a_view.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert [diagnostic.code for diagnostic in report.diagnostics] == [RECURSIVE_VIEW_DEFINITION]
