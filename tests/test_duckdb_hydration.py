"""DuckDB catalog hydration, end to end against an in-memory connection.

Every assertion here reads only the catalog: tables are empty, and the facts
come from DuckDB's own constraints and view definitions.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import CATALOG_METADATA_UNAVAILABLE, CATALOG_RELATION_NOT_FOUND, RECURSIVE_VIEW_DEFINITION
from sqlassert.ir.convert import IrParser
from sqlassert.main import analyze_database
from sqlassert.sql_parse import SqlParser

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
    /**ASSERT MANY TO ONE**/ JOIN main.users ON orders.user_id = users.id
    """

    assert analyze_with(connection, sql).proved
    assert not analyze(sql).proved


def test_a_composite_nullable_unique_set_behaves_as_if_declared_in_sql(connection):
    query = """
    CREATE TABLE seats (org_id INTEGER, user_id INTEGER);

    SELECT *
    FROM seats
    /**ASSERT MANY TO ONE**/ JOIN memberships
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
        /**ASSERT MANY TO ONE**/ JOIN active_users ON orders.user_id = active_users.id
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


def test_an_enforced_foreign_key_proves_against_the_catalog(connection):
    connection.execute("CREATE TABLE payments (order_id INTEGER REFERENCES orders(id), org_id INTEGER, user_id INTEGER, FOREIGN KEY (org_id, user_id) REFERENCES memberships(org_id, user_id))")
    single = "SELECT * FROM payments /**ASSERT FOREIGN KEY(order_id) REFERENCES orders(id)**/"
    composite = "SELECT * FROM payments /**ASSERT FOREIGN KEY(user_id, org_id) REFERENCES memberships(user_id, org_id)**/"

    assert analyze_with(connection, single).proved
    assert analyze_with(connection, composite).proved
    assert [a.outcome for a in analyze_with(connection, "SELECT * FROM payments /**ASSERT FOREIGN KEY(order_id) REFERENCES users(id)**/").assertions] == [Outcome.UNKNOWN]


# Column types, enums, and collations (issue 30) -------------------------------

TYPED = """
CREATE TABLE typed (id INTEGER PRIMARY KEY, plain VARCHAR, folded VARCHAR COLLATE NOCASE, status ENUM('new', 'paid'), ratio DOUBLE);
"""


@pytest.mark.parametrize(
    ("label", "sql", "online", "offline"),
    [
        ("enum type", "SELECT status FROM typed /**ASSERT ENUM(status IN ('new', 'paid'))**/", Outcome.PROVED, Outcome.UNKNOWN),
        ("enum type, value missing", "SELECT status FROM typed /**ASSERT ENUM(status IN ('new'))**/", Outcome.UNKNOWN, Outcome.UNKNOWN),
        # Binary text: distinct strings are distinct values, so two IN lists meet.
        ("plain text", "SELECT plain FROM typed WHERE plain IN ('x', 'y') AND plain IN ('x', 'z') /**ASSERT ENUM(plain IN ('x'))**/", Outcome.PROVED, Outcome.UNKNOWN),
        ("collated text", "SELECT folded FROM typed WHERE folded IN ('x', 'y') AND folded IN ('x', 'z') /**ASSERT ENUM(folded IN ('x'))**/", Outcome.UNKNOWN, Outcome.UNKNOWN),
        # Identity comparison: a function of a column it determines.
        ("function of text", "SELECT plain, upper(plain) AS u, count(*) AS n FROM typed GROUP BY plain, upper(plain) /**ASSERT UNIQUE(plain)**/", Outcome.PROVED, Outcome.UNKNOWN),
        ("function of a double", "SELECT ratio, CAST(ratio AS VARCHAR) AS r, count(*) AS n FROM typed GROUP BY ratio, CAST(ratio AS VARCHAR) /**ASSERT UNIQUE(ratio)**/", Outcome.UNKNOWN, Outcome.UNKNOWN),
    ],
)
def test_catalog_column_types_reach_the_analysis(label: str, sql: str, online: Outcome, offline: Outcome):
    connection = duckdb.connect()
    connection.execute(TYPED)

    assert [assertion.outcome for assertion in analyze_with(connection, sql).assertions] == [online], label
    assert [assertion.outcome for assertion in analyze(sql).assertions] == [offline], label


def test_a_session_default_collation_makes_text_collated():
    connection = duckdb.connect()
    connection.execute(TYPED)
    connection.execute("SET default_collation = 'nocase'")
    sql = "SELECT plain FROM typed WHERE plain IN ('x', 'y') AND plain IN ('x', 'z') /**ASSERT ENUM(plain IN ('x'))**/"

    assert [assertion.outcome for assertion in analyze_with(connection, sql).assertions] == [Outcome.UNKNOWN]


def test_an_inline_enum_column_type_is_a_domain_without_a_connection():
    sql = "CREATE TABLE t (status ENUM('new', 'paid')); SELECT status FROM t /**ASSERT ENUM(status IN ('new', 'paid'))**/"

    assert analyze(sql).proved


def test_a_column_of_a_user_defined_type_is_lowered():
    # Regression: the identity-comparison check once rebuilt the type by name.
    # Lowered directly: DuckDB cannot run the program without CREATE TYPE.
    parsed = SqlParser("duckdb").parse("CREATE TABLE t (id INTEGER, s mood); SELECT id, s FROM t")
    table, = IrParser("duckdb").parse(parsed).program.named_relations

    assert [column.identity_comparison for column in table.output_columns] == [True, False]
