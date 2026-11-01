"""ADR 0005: `analyze(sql)` warns whenever a connection would have helped.

The same SQL analyzed with a catalog proves what the warnings said was missing,
and a connection never turns PROVED into UNKNOWN.
"""

from __future__ import annotations

import duckdb
import pytest

from sqlassert import Outcome, analyze
from sqlassert import diagnostics as diag
from sqlassert.discovery.duckdb import DuckDBKnowledgeFetcher
from sqlassert.main import analyze_database

CATALOG = """
CREATE TABLE accounts (id INTEGER PRIMARY KEY, region INTEGER);
CREATE TABLE regions (code INTEGER PRIMARY KEY, name VARCHAR);
"""


@pytest.fixture
def connection():
    connection = duckdb.connect()
    connection.execute(CATALOG)
    yield connection
    connection.close()


def codes(warnings) -> list[str]:
    return [warning.code for warning in warnings]


def test_a_table_without_create_warns_once_and_names_the_fix():
    report = analyze("SELECT a.id FROM accounts a JOIN accounts b ON a.id = b.id /**ASSERT UNIQUE(id)**/")

    assert codes(report.warnings) == [diag.UNDECLARED_TABLE]
    message = report.warnings[0].message
    assert "accounts" in message
    assert "CREATE TABLE" in message
    assert "analyze_database" in message


def test_declared_tables_do_not_warn():
    report = analyze(CATALOG + "SELECT id FROM accounts /**ASSERT UNIQUE(id)**/")

    assert report.warnings == ()
    assert report.proved


def test_warnings_do_not_fail_a_report():
    report = analyze("SELECT id FROM accounts /**DECLARE UNIQUE(id)**/ /**ASSERT UNIQUE(id)**/")

    assert codes(report.warnings) == [diag.UNDECLARED_TABLE]
    assert report.diagnostics == ()
    assert report.proved


def test_an_unqualified_column_two_undeclared_sources_may_own_warns():
    report = analyze("SELECT id FROM accounts a JOIN regions r ON a.region = r.code /**ASSERT UNIQUE(id)**/")

    assert sorted(codes(report.warnings)) == [diag.AMBIGUOUS_COLUMN, diag.UNDECLARED_TABLE, diag.UNDECLARED_TABLE]
    ambiguous, = (warning for warning in report.warnings if warning.code == diag.AMBIGUOUS_COLUMN)
    assert "column id" in ambiguous.message
    assert "a, r" in ambiguous.message
    assert "qualify it" in ambiguous.message
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_qualified_column_does_not_warn_ambiguous():
    report = analyze("SELECT a.id FROM accounts a JOIN regions r ON a.region = r.code")

    assert diag.AMBIGUOUS_COLUMN not in codes(report.warnings)


SAME_SQL = [
    "SELECT id FROM accounts a JOIN regions r ON a.region = r.code /**ASSERT UNIQUE(id)**/",
    "SELECT a.id FROM accounts a JOIN regions r ON a.region = r.code /**ASSERT UNIQUE(id)**/",
    "SELECT * FROM accounts a /**ASSERT UNIQUE**/ JOIN regions r ON a.region = r.code",
    "SELECT id FROM accounts /**ASSERT NOT NULL UNIQUE(id)**/",
]


@pytest.mark.parametrize("sql", SAME_SQL)
def test_the_same_sql_warns_offline_and_proves_with_a_connection(connection, sql: str):
    offline = analyze(sql)
    online = analyze_database(sql, DuckDBKnowledgeFetcher(connection))

    assert offline.warnings
    assert [assertion.outcome for assertion in offline.assertions] == [Outcome.UNKNOWN]
    assert online.warnings == ()
    assert online.proved, online


ALREADY_PROVED = [
    "SELECT DISTINCT region FROM accounts /**ASSERT UNIQUE(region)**/",
    "SELECT region, count(*) AS n FROM accounts GROUP BY region /**ASSERT UNIQUE(region)**/",
    "SELECT id FROM accounts /**DECLARE UNIQUE(id)**/ /**ASSERT UNIQUE(id)**/",
]


@pytest.mark.parametrize("sql", ALREADY_PROVED)
def test_a_connection_never_turns_proved_into_unknown(connection, sql: str):
    offline = analyze(sql)
    online = analyze_database(sql, DuckDBKnowledgeFetcher(connection))

    assert offline.proved
    assert online.proved
