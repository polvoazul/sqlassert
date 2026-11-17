"""What DuckDB and SQLite do with the names `ir/convert.py` assumes of a star
over a USING join and of a derived table's repeated names (docs/dialects.md,
"Stars and names"). Probed here on the in-process engines, as
`scripts/certify_dialects.py` does for the dialect flags."""

import sqlite3

import duckdb
import pytest

from sqlassert.dialects import semantics

TABLES = (
    "CREATE TABLE a (x INTEGER, k INTEGER, m INTEGER); CREATE TABLE b (y INTEGER, m INTEGER, k INTEGER, z INTEGER); "
    "CREATE TABLE c (k INTEGER, w INTEGER);"
)


def _duckdb(sql: str) -> list[str]:
    connection = duckdb.connect()
    for statement in TABLES.split("; "):
        connection.execute(statement)
    return [column[0] for column in connection.execute(sql).description]


def _sqlite(sql: str) -> list[str]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(TABLES)
    return [column[0] for column in connection.execute(sql).description]


ENGINES = [("duckdb", _duckdb), ("sqlite", _sqlite)]


@pytest.mark.parametrize(("dialect", "columns"), ENGINES)
def test_a_using_column_stays_at_its_left_inputs_position(dialect, columns):
    """The standard would list `k` first: that is not what these engines do, and
    the flag says so."""
    assert semantics(dialect).using_column_stays_in_place
    assert columns("SELECT * FROM a JOIN b USING (k)") == ["x", "k", "m", "y", "m", "z"]
    assert columns("SELECT * FROM a RIGHT JOIN b USING (k)") == ["x", "k", "m", "y", "m", "z"]
    assert columns("SELECT * FROM a JOIN b USING (m, k)") == ["x", "k", "m", "y", "z"]
    assert columns("SELECT * FROM a JOIN b USING (k) JOIN c USING (k)") == ["x", "k", "m", "y", "m", "z", "w"]
    assert columns("SELECT a.* FROM a JOIN b USING (k)") == ["x", "k", "m"]


def test_duckdb_renames_the_repeats_of_a_derived_table():
    assert _duckdb("SELECT * FROM (SELECT 1 AS k, 2 AS k, 3 AS k_1) AS s") == ["k", "k_1", "k_1_1"]
    assert _duckdb("SELECT * FROM (SELECT 1 AS k, 3 AS k_1, 2 AS k) AS s") == ["k", "k_1", "k_2"]
    assert _duckdb("SELECT * FROM (SELECT 1 AS k, 2 AS K) AS s") == ["k", "K_1"]
    assert _duckdb("WITH c AS (SELECT 1 AS k, 2 AS k) SELECT * FROM c") == ["k", "k_1"]


def test_sqlite_renames_the_repeats_of_a_derived_table():
    assert _sqlite("SELECT * FROM (SELECT 1 AS k, 2 AS k, 3 AS k_1) AS s") == ["k", "k:1", "k_1"]
