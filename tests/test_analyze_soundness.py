"""A proof is only ever as good as the relation it was proved about.

Every case here shares a bare relation name with a declared table without
sharing its Unique Sets. Each must be UNKNOWN: an unsound PROVED is the worst
defect this engine can have.
"""

from __future__ import annotations

from sqlassert import Outcome, analyze


def test_a_from_subquery_does_not_inherit_a_declared_table_name_s_unique_set():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN (SELECT user_id AS id FROM sessions) AS users
            ON sessions.user_id = users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_cte_does_not_inherit_a_declared_table_name_s_unique_set():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        WITH users AS (SELECT user_id AS id FROM sessions)
        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_relations_in_different_schemas_are_different_relations():
    report = analyze(
        """
        CREATE TABLE a.users (id INTEGER PRIMARY KEY);
        CREATE TABLE b.users (id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN b.users
            ON sessions.user_id = b.users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_duplicate_declaration_is_an_explicit_program_diagnostic():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE users (id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert not report.proved
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["duplicate-declaration"]
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_marker_never_reaches_a_join_in_a_later_statement():
    report = analyze(
        """
        /**UNIQUE**/ CREATE TABLE users (id INTEGER PRIMARY KEY);

        SELECT *
        FROM sessions
        JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert report.assertions == ()
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["unattached-marker"]
    assert not report.proved


def test_an_assertion_this_analysis_cannot_reach_is_reported_not_dropped():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        WITH joined AS (
            SELECT *
            FROM sessions
            /**UNIQUE**/ JOIN users
                ON sessions.user_id = users.id
        )
        SELECT * FROM joined
        """
    )

    assert not report.proved
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["unanalyzed-assertion"]
