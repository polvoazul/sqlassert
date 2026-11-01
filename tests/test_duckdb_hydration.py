"""DuckDB catalog hydration, end to end against an in-memory connection.

Every assertion here reads only the catalog: tables are empty, and the facts
come from DuckDB's own constraints and view definitions.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import CATALOG_METADATA_UNAVAILABLE, CATALOG_RELATION_NOT_FOUND, RECURSIVE_VIEW_DEFINITION
from sqlassert.main import analyze_database

duckdb = pytest.importorskip("duckdb")

from sqlassert.discovery.duckdb import DuckDBKnowledgeFetcher  # noqa: E402

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE memberships (org_id INTEGER NOT NULL, user_id INTEGER, UNIQUE (org_id, user_id));
"""


@pytest.fixture
def connection():
    connection = duckdb.connect()
    connection.execute(SCHEMA)
    yield connection
    connection.close()


def analyze_with(connection, sql: str):
    return analyze_database(sql, DuckDBKnowledgeFetcher(connection))


def test_a_unique_join_proves_against_a_live_primary_key(connection):
    sql = """
    SELECT *
    FROM orders
    /**ASSERT UNIQUE**/ JOIN main.users ON orders.user_id = users.id
    """

    assert analyze_with(connection, sql).proved
    assert not analyze(sql).proved


def test_a_composite_nullable_unique_set_behaves_as_if_declared_in_sql(connection):
    query = """
    CREATE TABLE seats (org_id INTEGER, user_id INTEGER);

    SELECT *
    FROM seats
    /**ASSERT UNIQUE**/ JOIN memberships
        ON seats.org_id = memberships.org_id AND seats.user_id = memberships.user_id
    """
    declared = analyze(
        "CREATE TABLE memberships (org_id INTEGER NOT NULL, user_id INTEGER, UNIQUE (org_id, user_id));" + query
    )

    report = analyze_with(connection, query)

    assert report.proved
    assert [a.outcome for a in report.assertions] == [a.outcome for a in declared.assertions]


def test_a_candidate_key_assertion_uses_catalog_uniqueness_and_non_nullness(connection):
    unique = analyze_with(connection, "SELECT org_id, user_id FROM memberships /**ASSERT UNIQUE(org_id, user_id)**/")
    nullable_key = analyze_with(connection, "SELECT org_id, user_id FROM memberships /**ASSERT PRIMARY KEY(org_id, user_id)**/")
    key = analyze_with(connection, "SELECT id, email FROM users /**ASSERT PRIMARY KEY(id)**/")

    assert unique.proved
    assert [a.outcome for a in nullable_key.assertions] == [Outcome.UNKNOWN]
    assert key.proved


def test_views_expand_recursively_from_the_catalog(connection):
    connection.execute("CREATE VIEW recent_users AS SELECT id, email FROM users")
    connection.execute("CREATE VIEW active_users AS SELECT * FROM recent_users WHERE id > 0")

    report = analyze_with(
        connection,
        """
        SELECT *
        FROM orders
        /**ASSERT UNIQUE**/ JOIN active_users ON orders.user_id = active_users.id
        """,
    )

    assert report.proved


def test_a_view_reads_relations_in_its_own_schema(connection):
    """DuckDB binds `s.v`'s `users` to `s.users`, which has no key."""
    connection.execute("CREATE SCHEMA s")
    connection.execute("CREATE TABLE s.users (id INTEGER)")
    connection.execute("CREATE VIEW s.v AS SELECT id FROM users")

    report = analyze_with(connection, "SELECT id FROM s.v /**ASSERT UNIQUE(id)**/")

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]
    assert not report.diagnostics


def test_recursive_catalog_views_terminate_with_a_diagnostic(connection):
    connection.execute("CREATE VIEW a AS SELECT 1 AS id")
    connection.execute("CREATE VIEW b AS SELECT * FROM a")
    connection.execute("CREATE OR REPLACE VIEW a AS SELECT * FROM b")

    report = analyze_with(connection, "SELECT id FROM a /**ASSERT UNIQUE(id)**/")

    assert not report.proved
    assert RECURSIVE_VIEW_DEFINITION in {diagnostic.code for diagnostic in report.diagnostics}


def test_temporary_tables_shadow_the_default_schema(connection):
    connection.execute("CREATE TEMP TABLE users (id INTEGER)")

    report = analyze_with(connection, "SELECT id FROM users /**ASSERT UNIQUE(id)**/")

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]


def test_a_missing_relation_is_a_diagnostic(connection):
    report = analyze_with(connection, "SELECT id FROM ghosts /**ASSERT UNIQUE(id)**/")

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]
    assert [diagnostic.code for diagnostic in report.diagnostics] == [CATALOG_RELATION_NOT_FOUND]


def test_an_unreadable_catalog_is_a_diagnostic(connection):
    connection.close()

    report = analyze_with(connection, "SELECT id FROM users /**ASSERT UNIQUE(id)**/")

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]
    assert [diagnostic.code for diagnostic in report.diagnostics] == [CATALOG_METADATA_UNAVAILABLE]


def test_sql_declared_relations_take_precedence_over_the_catalog(connection):
    report = analyze_with(connection, "CREATE TABLE users (id INTEGER); SELECT id FROM users /**ASSERT UNIQUE(id)**/")

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]
