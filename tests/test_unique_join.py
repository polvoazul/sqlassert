from __future__ import annotations

import duckdb
import pytest

from assql import unique_assertions, validate_unique_joins


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = duckdb.connect(":memory:")
    connection.execute("PRAGMA enable_verification")
    connection.execute("CREATE TABLE uj_users (id INTEGER PRIMARY KEY, name VARCHAR)")
    connection.execute(
        "CREATE TABLE uj_orders ("
        "order_id INTEGER, "
        "user_id INTEGER, "
        "qty INTEGER, "
        "PRIMARY KEY (order_id, user_id)"
        ")"
    )
    connection.execute("CREATE TABLE uj_sessions (user_id INTEGER, ts TIMESTAMP)")
    connection.execute("INSERT INTO uj_users VALUES (1, 'alice'), (2, 'bob')")
    connection.execute("INSERT INTO uj_orders VALUES (1, 1, 30), (1, 2, 20), (2, 1, 10)")
    connection.execute(
        "INSERT INTO uj_sessions VALUES "
        "(1, '2024-01-01'), "
        "(1, '2024-01-02'), "
        "(2, '2024-01-01')"
    )
    connection.execute(
        "CREATE VIEW uj_active_customers AS "
        "SELECT id FROM uj_users where name = 'bob'"
    )
    connection.execute(
        """
        CREATE VIEW myview AS
        WITH cte AS (
            SELECT user_id as uid, GREATEST(qty, 100) as total_qty
            FROM (SELECT user_id, SUM(qty) as qty FROM uj_orders GROUP BY user_id) x
        ) SELECT * FROM cte
        """
    )
    connection.execute("CREATE VIEW uj_sessions_view AS SELECT user_id, ts FROM uj_sessions")
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            """
            SELECT COUNT(*)
            FROM uj_sessions
            /**unique**/ JOIN uj_users
                ON uj_sessions.user_id = uj_users.id
            """,
            3,
        ),
        (
            """
            SELECT COUNT(*)
            FROM uj_sessions
            /**unique**/ INNER JOIN uj_orders
                ON uj_sessions.user_id = uj_orders.user_id AND uj_orders.order_id = 1
            """,
            3,
        ),
        (
            """
            SELECT COUNT(*) FROM uj_sessions /**unique**/ INNER JOIN (
                SELECT user_id, MAX(ts) AS max_ts
                FROM uj_sessions
                GROUP BY user_id
            ) t ON uj_sessions.user_id = t.user_id
            """,
            3,
        ),
        (
            """
            SELECT COUNT(*) FROM uj_sessions /**unique**/ INNER JOIN (
                SELECT DISTINCT user_id
                FROM uj_sessions
            ) e ON uj_sessions.user_id = e.user_id
            """,
            3,
        ),
        (
            """
            SELECT COUNT(*) FROM uj_sessions /**unique**/ INNER JOIN (
                SELECT user_id
                FROM uj_sessions
                QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY ts) = 1
            ) s ON uj_sessions.user_id = s.user_id
            """,
            3,
        ),
        (
            """
            SELECT COUNT(*)
            FROM uj_sessions /**unique**/ LEFT JOIN uj_active_customers
                ON uj_sessions.user_id = uj_active_customers.id
            """,
            3,
        ),
        (
            """
            SELECT SUM(total_qty)
            FROM myview
            /**unique**/ JOIN uj_users
                ON myview.uid = uj_users.id
            """,
            200,
        ),
        (
            """
            SELECT COUNT(*)
            FROM uj_sessions /**unique**/ LEFT JOIN uj_active_customers
                ON uj_sessions.user_id = uj_active_customers.id
            """,
            3,
        ),
    ],
)
def test_unique_join_happy_paths(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    expected: int,
) -> None:
    assert con.execute(sql).fetchone()[0] == expected

    validation = validate_unique_joins(con, sql)

    assert validation.valid, validation.reason


@pytest.mark.parametrize(
    "sql",
    [
        """
        SELECT * FROM uj_sessions /**unique**/ INNER JOIN uj_orders
        ON uj_sessions.user_id = uj_orders.order_id
        """,
        """
        SELECT * FROM uj_sessions /**unique**/ INNER JOIN uj_sessions s2
        ON uj_sessions.user_id = s2.user_id
        """,
        """
        SELECT * FROM uj_sessions /**unique**/ INNER JOIN (
            SELECT user_id FROM uj_sessions
            GROUP BY user_id, ts
        ) g ON uj_sessions.user_id = g.user_id
        """,
        """
        SELECT * FROM uj_sessions /**unique**/ INNER JOIN (
            SELECT user_id
            FROM (SELECT user_id, ts FROM uj_sessions) s
        ) t ON uj_sessions.user_id = t.user_id
        """,
        """
        SELECT * FROM uj_sessions /**unique**/ INNER JOIN uj_sessions_view
        ON uj_sessions.user_id = uj_sessions_view.user_id
        """,
        """
        SELECT * FROM uj_sessions /**unique**/ ANTI JOIN uj_users
        ON uj_sessions.user_id = uj_users.id
        """,
    ],
)
def test_unique_join_unhappy_paths(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    validation = validate_unique_joins(con, sql)

    assert not validation.valid
    assert validation.reason


def test_returns_no_assertions_without_marker() -> None:
    sql = "select * from uj_sessions join uj_users on uj_sessions.user_id = uj_users.id"

    assert unique_assertions(sql) == []


def test_parse_error_with_marker_returns_false() -> None:
    sql = "select * from uj_sessions /**unique**/ join"

    assert unique_assertions(sql) == ["select false"]


def test_connection_backed_parse_error_returns_invalid_result(con: duckdb.DuckDBPyConnection) -> None:
    result = validate_unique_joins(con, "select * from uj_sessions /**unique**/ join")

    assert not result.valid
    assert result.reason == "SQL parse failed"


def test_connection_backed_result_reports_primary_key_reason(
    con: duckdb.DuckDBPyConnection,
) -> None:
    sql = "select * from uj_sessions /**unique**/ join uj_users on uj_sessions.user_id = uj_users.id"

    result = validate_unique_joins(con, sql)

    assert result.valid
    assert result.checks[0].primary_key_columns == ("id",)
    assert "primary key" in result.checks[0].reason
