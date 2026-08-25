"""A proof is only ever as good as the relation it was proved about.

Every case here shares a bare relation name with a declared table without
sharing its Unique Sets. Each must be UNKNOWN: an unsound PROVED is the worst
defect this engine can have.
"""

from __future__ import annotations

import pytest

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


MISPLACED_MARKERS = [
    ("before a comma", "SELECT * FROM a /**UNIQUE**/ , b", "unattached-marker"),
    ("trailing with no join", "SELECT * FROM a /**UNIQUE**/", "unattached-marker"),
    ("in the select list", "SELECT /**UNIQUE**/ 1", "unattached-marker"),
    ("before a create statement", "/**UNIQUE**/ CREATE TABLE t (id INTEGER);", "sql-parse-failed"),
    ("before a select", "/**UNIQUE**/ SELECT * FROM t", "sql-parse-failed"),
    ("after the join keyword", "SELECT * FROM a JOIN /**UNIQUE**/ b ON a.x = b.x", "sql-parse-failed"),
    (
        "with the join in a later statement",
        "/**UNIQUE**/ CREATE TABLE users (id INTEGER PRIMARY KEY);\n"
        "SELECT * FROM sessions JOIN users ON sessions.user_id = users.id",
        "sql-parse-failed",
    ),
]


@pytest.mark.parametrize(("label", "sql", "code"), MISPLACED_MARKERS)
def test_a_marker_that_does_not_mark_a_join_is_reported(label: str, sql: str, code: str):
    """However a marker is misplaced, it is named and nothing is proved.

    The grammar requires a marker to sit immediately before its join. Some
    misplacements are caught by the join rule and some break parsing outright,
    so the code differs — but an assertion is never silently dropped, and the
    diagnostic always points at the marker's own line.
    """
    report = analyze(sql)

    assert report.assertions == (), label
    assert not report.proved, label
    assert [diagnostic.code for diagnostic in report.diagnostics] == [code], label
    lines = []
    for diagnostic in report.diagnostics:
        assert diagnostic.origin is not None, label
        lines.append(diagnostic.origin.line)
    assert lines == [1], label
    assert "marker" in report.diagnostics[0].message, label


@pytest.mark.parametrize(
    "join",
    ["JOIN", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN", "FULL OUTER JOIN", "CROSS JOIN"],
)
def test_a_marker_reaches_its_join_through_join_modifier_keywords(join: str):
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT * FROM sessions /**UNIQUE**/ {join} users ON sessions.user_id = users.id
        """
    )

    assert len(report.assertions) == 1
    assert not report.diagnostics


UNRECOGNIZED_MARKERS = [
    ("spaced", "/** UNIQUE **/"),
    ("extra spaces", "/**  UNIQUE  **/"),
    ("one closing star", "/**UNIQUE*/"),
    ("three closing stars", "/**UNIQUE***/"),
    ("an unknown term", "/**UNIQE**/"),
    ("a doc comment", "/** explains this join **/"),
]


@pytest.mark.parametrize(("label", "comment"), UNRECOGNIZED_MARKERS)
def test_a_comment_shaped_like_a_marker_is_reported(label: str, comment: str):
    """`/**...*/` is assertion syntax, so a near miss is likelier a mistyped
    marker than a note. Silently ignoring one would read to its author as a
    proof of a join nobody checked."""
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT * FROM sessions {comment} JOIN users ON sessions.user_id = users.id
        """
    )

    assert report.assertions == (), label
    assert not report.proved, label
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["unrecognized-marker"], label


@pytest.mark.parametrize("comment", ["/* unique */", "/*UNIQUE*/", "/* explains this join */", "-- a note"])
def test_an_ordinary_comment_is_not_mistaken_for_a_marker(comment: str):
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT * FROM sessions {comment}
        JOIN users ON sessions.user_id = users.id
        """
    )

    assert report.assertions == ()
    assert not report.diagnostics
    assert report.proved


@pytest.mark.parametrize("marker", ["/**UNIQUE**/", "/**unique**/", "/**Unique**/"])
def test_a_marker_is_recognized_whatever_its_case(marker: str):
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT * FROM sessions {marker} JOIN users ON sessions.user_id = users.id
        """
    )

    assert report.proved
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
