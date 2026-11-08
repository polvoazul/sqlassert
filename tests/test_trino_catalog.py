"""`TrinoKnowledgeFetcher` against a real Trino, through the `trino` client.

Runs only when `SQLASSERT_TRINO` holds the client's `connect()` keyword
arguments as JSON, for a server with the `memory` catalog (the default
`trinodb/trino` image), for example
`{"host": "127.0.0.1", "port": 58080, "user": "probe"}`. It creates and drops
its own schema in `memory`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from sqlassert import Outcome, analyze_database
from sqlassert.discovery.trino import TrinoKnowledgeFetcher

CONNECT = os.environ.get("SQLASSERT_TRINO")
# One schema per pytest-xdist worker, so workers never drop each other's.
SCHEMA = f"sqlassert_catalog_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"

pytestmark = pytest.mark.skipif(CONNECT is None, reason="SQLASSERT_TRINO is not set")


def _run(connection: Any, statements: list[str]) -> None:
    cursor = connection.cursor()
    for statement in statements:
        cursor.execute(statement)
        cursor.fetchall()


@pytest.fixture(scope="module")
def connection() -> Iterator[Any]:
    trino = pytest.importorskip("trino")
    assert CONNECT is not None
    connection = trino.dbapi.connect(**json.loads(CONNECT), catalog="memory", schema=SCHEMA)
    drop = [f"DROP VIEW IF EXISTS memory.{SCHEMA}.paid", f"DROP TABLE IF EXISTS memory.{SCHEMA}.customers", f"DROP SCHEMA IF EXISTS memory.{SCHEMA}"]
    _run(connection, drop)
    _run(connection, [
        f"CREATE SCHEMA memory.{SCHEMA}",
        f"CREATE TABLE memory.{SCHEMA}.customers (id INTEGER NOT NULL, email VARCHAR(50) NOT NULL, code CHAR(3), note VARCHAR)",
        f"CREATE VIEW memory.{SCHEMA}.paid AS SELECT id, email FROM customers",
    ])
    yield connection
    _run(connection, drop)


def _outcome(connection: Any, sql: str) -> Outcome:
    report = analyze_database(sql, TrinoKnowledgeFetcher(connection), dialect="trino")
    assert report.diagnostics == ()
    assertion, = report.assertions
    return assertion.outcome


def test_not_null_columns_are_knowledge(connection):
    assert _outcome(connection, "SELECT email FROM customers /**ASSERT NOT NULL(email)**/") == Outcome.PROVED
    assert _outcome(connection, "SELECT note FROM customers /**ASSERT NOT NULL(note)**/") == Outcome.UNKNOWN


def test_varchar_is_compared_by_identity_and_char_is_not(connection):
    no_b = "SELECT {0} FROM customers WHERE {0} IN ('a', 'b') AND {0} = 'B' /**ASSERT ENUM({0} IN ('a'))**/"

    assert _outcome(connection, no_b.format("note")) == Outcome.PROVED
    assert _outcome(connection, no_b.format("code")) == Outcome.UNKNOWN


def test_a_view_and_a_name_qualified_by_its_catalog_resolve(connection):
    assert _outcome(connection, "SELECT email FROM paid /**ASSERT NOT NULL(email)**/") == Outcome.PROVED
    sql = f"SELECT id FROM memory.{SCHEMA}.customers /**ASSERT NOT NULL(id)**/"
    assert _outcome(connection, sql) == Outcome.PROVED
