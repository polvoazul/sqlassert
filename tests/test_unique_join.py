from __future__ import annotations

import duckdb
import pytest

from assql import unique_assertions, validate_unique_joins


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute("""
        PRAGMA enable_verification;
        CREATE TABLE uj_users (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE TABLE uj_orders (
            order_id INTEGER,
            user_id INTEGER,
            qty INTEGER,
            PRIMARY KEY (order_id, user_id)
        );
        CREATE TABLE uj_sessions (user_id INTEGER, ts TIMESTAMP);

        INSERT INTO uj_users VALUES (1, 'alice'), (2, 'bob');
        INSERT INTO uj_orders VALUES (1, 1, 30), (1, 2, 20), (2, 1, 10);
        INSERT INTO uj_sessions VALUES
            (1, '2024-01-01'),
            (1, '2024-01-02'),
            (2, '2024-01-01');

        CREATE VIEW uj_active_customers AS
            SELECT id FROM uj_users WHERE name = 'bob';

        CREATE VIEW myview AS
            WITH cte AS (
                SELECT user_id as uid, GREATEST(qty, 100) as total_qty
                FROM (SELECT user_id, SUM(qty) as qty FROM uj_orders GROUP BY user_id) x
            ) SELECT * FROM cte;

        CREATE VIEW uj_sessions_view AS SELECT user_id, ts FROM uj_sessions;
    """)
    try:
        yield connection
    finally:
        connection.close()


def assert_valid(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    expected_constraint: tuple[str, ...],
) -> None:
    validation = validate_unique_joins(con, sql)

    assert validation.valid, validation.reason
    assert len(validation.checks) == 1
    assert validation.checks[0].constrained_key_columns == expected_constraint


def assert_invalid(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    expected_reason: str,
) -> None:
    validation = validate_unique_joins(con, sql)

    assert not validation.valid
    assert validation.reason == expected_reason


# ------------------------------------------------------------------------------
# Happy paths
# ------------------------------------------------------------------------------


def test_single_column_pk_on_rhs(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT COUNT(*)
            FROM uj_sessions
            /**UNIQUE**/ JOIN uj_users
                ON uj_sessions.user_id = uj_users.id
            """
    assert_valid(con, query, ("id",))


def test_using_single_column_pk_on_rhs(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT *
            FROM uj_users
            /**UNIQUE**/ JOIN uj_users u2
                USING (id)
            """
    assert_valid(con, query, ("id",))


def test_composite_pk_with_rhs_filter(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT COUNT(*)
            FROM uj_sessions
            /**UNIQUE**/ INNER JOIN uj_orders
                ON uj_sessions.user_id = uj_orders.user_id AND uj_orders.order_id = 1
            """
    assert_valid(con, query, ("order_id", "user_id"))


def test_group_by_rhs_subquery(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT COUNT(*) FROM uj_sessions /**UNIQUE**/ INNER JOIN (
                SELECT user_id, MAX(ts) AS max_ts
                FROM uj_sessions
                GROUP BY user_id
            ) t ON uj_sessions.user_id = t.user_id
            """
    assert_valid(con, query, ("user_id",))


def test_select_distinct_rhs_subquery(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT COUNT(*) FROM uj_sessions /**UNIQUE**/ INNER JOIN (
                SELECT DISTINCT user_id
                FROM uj_sessions
            ) e ON uj_sessions.user_id = e.user_id
            """
    assert_valid(con, query, ("user_id",))


def test_qualify_row_number_rhs_subquery(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT COUNT(*) FROM uj_sessions /**UNIQUE**/ INNER JOIN (
                SELECT user_id
                FROM uj_sessions
                QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY ts) = 1
            ) s ON uj_sessions.user_id = s.user_id
            """
    assert_valid(con, query, ("user_id",))


def test_view_over_pk_table(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT COUNT(*)
            FROM uj_sessions /**UNIQUE**/ LEFT JOIN uj_active_customers
                ON uj_sessions.user_id = uj_active_customers.id
            """
    assert_valid(con, query, ("id",))


def test_complex_cte_view_join_to_pk_rhs(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT SUM(total_qty)
            FROM myview
            /**UNIQUE**/ JOIN uj_users
                ON myview.uid = uj_users.id
            """
    assert_valid(con, query, ("id",))


def test_repeated_view_over_pk_table(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT COUNT(*)
            FROM uj_sessions /**UNIQUE**/ LEFT JOIN uj_active_customers
                ON uj_sessions.user_id = uj_active_customers.id
            """
    assert_valid(con, query, ("id",))


# ------------------------------------------------------------------------------
# Unhappy paths
# ------------------------------------------------------------------------------


def test_composite_pk_only_one_column_in_join_predicate(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT * FROM uj_sessions /**UNIQUE**/ INNER JOIN uj_orders
            ON uj_sessions.user_id = uj_orders.order_id
            """
    assert_invalid(con, query, "in join INNER JOIN uj_orders ON uj_sessions.user_id = uj_orders.order_id, we can't prove that RHS column order_id is unique")


def test_no_unique_constraint_on_rhs(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT * FROM uj_sessions /**UNIQUE**/ INNER JOIN uj_sessions s2
            ON uj_sessions.user_id = s2.user_id
            """
    assert_invalid(con, query, "in join INNER JOIN uj_sessions AS s2 ON uj_sessions.user_id = s2.user_id, we can't prove that RHS column user_id is unique")


def test_using_no_unique_constraint_on_rhs(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT * FROM uj_sessions
            /**UNIQUE**/ INNER JOIN uj_sessions s2
                USING (user_id)
            """
    assert_invalid(con, query, "in join INNER JOIN uj_sessions AS s2 USING (user_id), we can't prove that RHS column user_id is unique")


def test_group_by_extra_column_is_not_metadata_provable(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT * FROM uj_sessions /**UNIQUE**/ INNER JOIN (
                SELECT user_id FROM uj_sessions
                GROUP BY user_id, ts
            ) g ON uj_sessions.user_id = g.user_id
            """
    assert_invalid(con, query, "in join INNER JOIN (SELECT user_id FROM uj_sessions GROUP BY user_id, ts) AS g ON uj_sessions.user_id = g.user_id, we can't prove that RHS column user_id is unique")


def test_nested_rhs_subquery_is_not_metadata_provable(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT * FROM uj_sessions /**UNIQUE**/ INNER JOIN (
                SELECT user_id
                FROM (SELECT user_id, ts FROM uj_sessions) s
            ) t ON uj_sessions.user_id = t.user_id
            """
    assert_invalid(con, query, "in join INNER JOIN (SELECT user_id FROM (SELECT user_id, ts FROM uj_sessions) AS s) AS t ON uj_sessions.user_id = t.user_id, we can't prove that RHS column user_id is unique")


def test_view_with_no_unique_guarantee(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT * FROM uj_sessions /**UNIQUE**/ INNER JOIN uj_sessions_view
            ON uj_sessions.user_id = uj_sessions_view.user_id
            """
    assert_invalid(con, query, "in join INNER JOIN uj_sessions_view ON uj_sessions.user_id = uj_sessions_view.user_id, we can't prove that RHS column user_id is unique")


def test_anti_join_not_supported(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT * FROM uj_sessions /**UNIQUE**/ ANTI JOIN uj_users
            ON uj_sessions.user_id = uj_users.id
            """
    assert_invalid(con, query, "in join ANTI JOIN uj_users ON uj_sessions.user_id = uj_users.id, anti joins are not supported")


# ------------------------------------------------------------------------------
# Multiple markers and API edge cases
# ------------------------------------------------------------------------------


def test_multiple_unique_joins_happy_path(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT *
            FROM uj_sessions
            /**UNIQUE**/ JOIN uj_users
                ON uj_sessions.user_id = uj_users.id
            /**UNIQUE**/ JOIN uj_orders
                ON uj_sessions.user_id = uj_orders.user_id AND uj_orders.order_id = 1
            """
    validation = validate_unique_joins(con, query)

    assert validation.valid, validation.reason
    assert len(validation.checks) == 2
    assert [check.valid for check in validation.checks] == [True, True]
    assert validation.checks[0].constrained_key_columns == ("id",)
    assert validation.checks[1].constrained_key_columns == ("order_id", "user_id")


def test_multiple_unique_joins_unhappy_path(con: duckdb.DuckDBPyConnection):
    query = """
            SELECT *
            FROM uj_sessions
            /**UNIQUE**/ JOIN uj_users
                ON uj_sessions.user_id = uj_users.id
            /**UNIQUE**/ JOIN uj_sessions s2
                ON uj_sessions.user_id = s2.user_id
            """
    validation = validate_unique_joins(con, query)

    assert not validation.valid
    assert validation.reason == "in join JOIN uj_sessions AS s2 ON uj_sessions.user_id = s2.user_id, we can't prove that RHS column user_id is unique"
    assert len(validation.checks) == 2
    assert [check.valid for check in validation.checks] == [True, False]
    assert validation.checks[0].constrained_key_columns == ("id",)
    assert validation.checks[1].inferred_key_columns == ("user_id",)
    assert validation.checks[1].reason == validation.reason


def test_returns_no_assertions_without_marker():
    query = "select * from uj_sessions join uj_users on uj_sessions.user_id = uj_users.id"
    assert unique_assertions(query) == []


def test_parse_error_with_marker_returns_false():
    query = "select * from uj_sessions /**UNIQUE**/ join"
    assert unique_assertions(query) == ["select false"]


def test_connection_backed_parse_error_returns_invalid_result(con: duckdb.DuckDBPyConnection):
    query = "select * from uj_sessions /**UNIQUE**/ join"
    result = validate_unique_joins(con, query)

    assert not result.valid
    assert result.reason == "SQL parse failed"


def test_connection_backed_result_reports_unique_constraint_reason(con: duckdb.DuckDBPyConnection):
    query = "select * from uj_sessions /**UNIQUE**/ join uj_users on uj_sessions.user_id = uj_users.id"
    result = validate_unique_joins(con, query)

    assert result.valid
    assert result.checks[0].constrained_key_columns == ("id",)
    assert "uniqueness proof" in result.checks[0].reason
