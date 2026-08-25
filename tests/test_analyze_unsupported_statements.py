"""Statements this engine does not model at all: neither a Create Statement of
a supported kind (TABLE, VIEW) nor a root select. Each must be a durable
diagnostic, never a crash and never a silently-dropped statement.
"""

from __future__ import annotations

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import UNSUPPORTED_CREATE_STATEMENT, UNSUPPORTED_STATEMENT


def test_a_top_level_statement_that_is_neither_create_nor_select_is_diagnosed():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        INSERT INTO users VALUES (1);

        SELECT * FROM users
        """
    )

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNSUPPORTED_STATEMENT]


def test_create_index_is_reported_and_dropped_not_silently_ignored():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE INDEX idx_users_id ON users (id);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNSUPPORTED_CREATE_STATEMENT]
    # The index declares nothing this analysis models; the table's own
    # PRIMARY KEY still proves the join.
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]


def test_create_sequence_is_reported_and_dropped():
    report = analyze(
        """
        CREATE SEQUENCE seq;

        SELECT 1
        """
    )

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNSUPPORTED_CREATE_STATEMENT]


def test_create_schema_is_reported_and_dropped():
    report = analyze(
        """
        CREATE SCHEMA analytics;

        SELECT 1
        """
    )

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNSUPPORTED_CREATE_STATEMENT]
