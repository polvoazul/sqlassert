"""`PostgresKnowledgeFetcher` against a real PostgreSQL, through `psql`.

Runs only when `SQLASSERT_PSQL` names a psql command line for a scratch
database, for example `docker exec -i pg psql -U postgres`; it creates and
drops its own schema. No Python driver is needed: `_PsqlConnection` is a
DB-API shim that runs each query through psql and reads its rows as JSON.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

from sqlassert import Outcome, analyze_database
from sqlassert.discovery.postgres import PostgresKnowledgeFetcher

PSQL = os.environ.get("SQLASSERT_PSQL")

SCHEMA = """
DROP SCHEMA IF EXISTS sqlassert_catalog CASCADE;
CREATE SCHEMA sqlassert_catalog;
SET search_path TO sqlassert_catalog;
CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT COLLATE "C", amount NUMERIC);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers (id), code TEXT, note TEXT,
    UNIQUE (code) DEFERRABLE
);
CREATE UNIQUE INDEX orders_note ON orders (note) INCLUDE (code);
CREATE TABLE shadow (id INTEGER, customer_id INTEGER);
ALTER TABLE shadow ADD CONSTRAINT shadow_customer FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;
CREATE TABLE unenforced (id INTEGER, customer_id INTEGER REFERENCES customers (id) NOT ENFORCED);
CREATE TABLE parent (id INTEGER PRIMARY KEY);
CREATE TABLE child () INHERITS (parent);
CREATE VIEW customer_orders AS SELECT o.id, o.customer_id, c.email FROM orders o JOIN customers c ON c.id = o.customer_id;
"""


class _PsqlCursor:
    def __init__(self, command: list[str]):
        self.command = command
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, operation: str, parameters: Any = (), /) -> None:
        sql = operation % tuple(_literal(value) for value in parameters)
        query = f"SET search_path TO sqlassert_catalog, public; SELECT coalesce(json_agg(q), '[]') FROM ({sql}) q"
        output = subprocess.run(self.command + ["-At", "-c", query], capture_output=True, text=True, check=True).stdout
        # Each row is a JSON object in column order, whose names may repeat.
        rows = json.loads(output.split("\n", 1)[1], object_pairs_hook=lambda pairs: tuple(value for _, value in pairs))
        self.rows = list(rows)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        pass


class _PsqlConnection:
    def __init__(self, command: str):
        self.command = shlex.split(command)

    def cursor(self) -> _PsqlCursor:
        return _PsqlCursor(self.command)


def _literal(value: Any) -> str:
    if isinstance(value, list):
        return "ARRAY[" + ", ".join(_literal(item) for item in value) + "]::text[]"
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture(scope="module")
def fetcher() -> Iterator[PostgresKnowledgeFetcher]:
    if not PSQL:
        pytest.skip("SQLASSERT_PSQL names no psql command")
    command = shlex.split(PSQL)
    subprocess.run(command + ["-v", "ON_ERROR_STOP=1", "-q", "-c", SCHEMA], capture_output=True, text=True, check=True)
    yield PostgresKnowledgeFetcher(_PsqlConnection(PSQL))
    subprocess.run(command + ["-q", "-c", "DROP SCHEMA sqlassert_catalog CASCADE"], capture_output=True, check=True)


def test_tables_have_their_columns_types_and_enforced_keys(fetcher: PostgresKnowledgeFetcher):
    found = fetcher.fetch_relation_metadata(("customers", "orders", "sqlassert_catalog.shadow", "unenforced", "parent", "missing"))

    customers, orders = found["customers"], found["orders"]
    assert customers.schema == "sqlassert_catalog" and customers.primary_key == ("id",) and customers.unique_sets == (("email",),)
    assert [(column.name, column.nullable, column.data_type, column.collation) for column in customers.columns] == [
        ("id", False, "integer", None), ("email", False, "text", None), ("name", True, "text", None), ("amount", True, "numeric", None),
    ]
    # The DEFERRABLE UNIQUE is left out; the unique index keeps its key column only.
    assert orders.unique_sets == (("note",),)
    reference, = orders.foreign_keys
    assert reference.columns == ("customer_id",) and reference.referenced_columns == ("id",)
    assert reference.referenced.endswith(".sqlassert_catalog.customers")
    assert found["sqlassert_catalog.shadow"].foreign_keys == ()  # NOT VALID
    assert found["unenforced"].foreign_keys == ()  # NOT ENFORCED
    assert found["parent"].primary_key == () and all(column.nullable for column in found["parent"].columns)  # Inherited.
    assert "missing" not in found


def test_a_view_is_read_from_its_stored_definition(fetcher: PostgresKnowledgeFetcher):
    view = fetcher.fetch_relation_metadata(("customer_orders",))["customer_orders"]

    definition = fetcher.fetch_view_definition(view)
    assert definition.sql.startswith('CREATE VIEW "customer_orders" AS')
    assert "JOIN customers" in definition.sql


def test_analysis_proves_from_the_catalog(fetcher: PostgresKnowledgeFetcher):
    sql = """
    SELECT o.id, c.email FROM orders o /**ASSERT EXACTLY ONE**/ JOIN customers c ON c.id = o.customer_id
    /**ASSERT NOT NULL UNIQUE(id)**/
    """
    report = analyze_database(sql, fetcher, dialect="postgres")

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED, Outcome.PROVED]
    assert report.diagnostics == ()


def test_a_view_hydrates_through_its_definition(fetcher: PostgresKnowledgeFetcher):
    report = analyze_database("SELECT id, email FROM customer_orders /**ASSERT UNIQUE(id)**/", fetcher, dialect="postgres")

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]


def test_an_unscaled_numeric_from_the_catalog_is_not_compared_by_identity(fetcher: PostgresKnowledgeFetcher):
    sql = "SELECT amount, CAST(amount AS TEXT) AS t FROM customers /**ASSERT DETERMINES(amount -> t)**/"

    assert [assertion.outcome for assertion in analyze_database(sql, fetcher, dialect="postgres").assertions] == [Outcome.UNKNOWN]


class _ScriptedCursor:
    """Answers each catalog query with the rows PostgreSQL 18 returns for a
    `customers` table, a `parent` other tables inherit from, and a foreign
    table `remote`, so the mapping is tested without a server."""

    ANSWERS: dict[str, list[tuple[Any, ...]]] = {
        "SELECT current_database()": [("shop", ["pg_catalog", "public"], 180003)],
        "SELECT n.nspname, c.relname, c.relkind": [
            ("public", "customers", "r", False), ("public", "parent", "r", True), ("public", "remote", "f", False),
        ],
        "SELECT n.nspname, c.relname, a.attname": [
            ("public", "customers", "id", False, "integer", None),
            ("public", "customers", "name", True, "text", "und-ci"),
            ("public", "parent", "id", False, "integer", None),
            ("public", "remote", "id", False, "integer", None),
        ],
        "SELECT n.nspname, c.relname, k.contype": [
            ("public", "customers", "p", ["id"], None, None, []), ("public", "parent", "p", ["id"], None, None, []),
            ("public", "remote", "u", ["id"], None, None, []),
        ],
        "SELECT n.nspname, c.relname, ARRAY": [],
    }

    def __init__(self, log: list[str]):
        self.log = log
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, operation: str, parameters: Any = (), /) -> None:
        self.log.append(operation)
        self.rows = next(rows for prefix, rows in self.ANSWERS.items() if operation.strip().startswith(prefix))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        pass


class _ScriptedConnection:
    def __init__(self) -> None:
        self.log: list[str] = []

    def cursor(self) -> _ScriptedCursor:
        return _ScriptedCursor(self.log)


def test_scripted_catalog_rows_become_metadata():
    connection = _ScriptedConnection()
    found = PostgresKnowledgeFetcher(connection).fetch_relation_metadata(("customers", "public.parent", "remote"))

    customers = found["customers"]
    assert customers.identity == "shop.public.customers" and customers.primary_key == ("id",)
    assert [column.collation for column in customers.columns] == [None, "und-ci"]
    # An inheritance parent and a foreign table are columns only, all nullable.
    for name in ("public.parent", "remote"):
        assert found[name].primary_key == () and found[name].unique_sets == ()
        assert all(column.nullable for column in found[name].columns)
    assert any("k.conenforced" in operation for operation in connection.log)  # PostgreSQL 18.
