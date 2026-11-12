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
# One schema per pytest-xdist worker, so workers never drop each other's.
CATALOG_SCHEMA = f"sqlassert_catalog_{os.environ.get('PYTEST_XDIST_WORKER', 'main')}"

SCHEMA = f"""
DROP SCHEMA IF EXISTS {CATALOG_SCHEMA} CASCADE;
CREATE SCHEMA {CATALOG_SCHEMA};
SET search_path TO {CATALOG_SCHEMA};
CREATE TABLE customers (id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT COLLATE "C", amount NUMERIC);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers (id), code TEXT, note TEXT,
    UNIQUE (code) DEFERRABLE
);
CREATE UNIQUE INDEX orders_note ON orders (note) INCLUDE (code);
CREATE UNIQUE INDEX customers_partial ON customers (name) WHERE amount > 0;
CREATE UNIQUE INDEX customers_lower ON customers (lower(name));
CREATE TABLE secret (id INTEGER PRIMARY KEY);
ALTER TABLE secret ENABLE ROW LEVEL SECURITY;
CREATE TABLE refs (secret_id INTEGER REFERENCES secret (id));
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
        query = f"SET search_path TO {CATALOG_SCHEMA}, public; SELECT coalesce(json_agg(q), '[]') FROM ({sql}) q"
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
    subprocess.run(command + ["-q", "-c", f"DROP SCHEMA {CATALOG_SCHEMA} CASCADE"], capture_output=True, check=True)


def test_tables_have_their_columns_types_and_enforced_keys(fetcher: PostgresKnowledgeFetcher):
    found = fetcher.fetch_relation_metadata(("customers", "orders", f"{CATALOG_SCHEMA}.shadow", "unenforced", "parent", "refs", "missing"))

    customers, orders = found["customers"], found["orders"]
    # The partial and the expression unique index are not keys.
    assert customers.schema == CATALOG_SCHEMA and customers.primary_key == ("id",) and customers.unique_sets == (("email",),)
    # A deterministic collation ("C") compares by identity: the column keeps its type.
    assert [(column.name, column.nullable, column.data_type) for column in customers.columns] == [
        ("id", False, "integer"), ("email", False, "text"), ("name", True, "text"), ("amount", True, "numeric"),
    ]
    # The DEFERRABLE UNIQUE is left out; the unique index keeps its key column only.
    assert orders.unique_sets == (("note",),)
    reference, = orders.foreign_keys
    assert reference.columns == ("customer_id",) and reference.referenced_columns == ("id",)
    assert reference.referenced.endswith(f".{CATALOG_SCHEMA}.customers")
    assert found[f"{CATALOG_SCHEMA}.shadow"].foreign_keys == ()  # NOT VALID
    assert found["unenforced"].foreign_keys == ()  # NOT ENFORCED
    assert found["refs"].foreign_keys == ()  # Onto a table with row-level security.
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

    ANSWERS: list[tuple[str, list[tuple[Any, ...]]]] = [
        ("current_schemas(true)", [("shop", ["pg_catalog", "public"], 180003)]),
        ("c.relhassubclass", [
            ("shop", "public", "customers", "r", False), ("shop", "public", "parent", "r", True),
            ("shop", "public", "remote", "f", False),
        ]),
        ("a.attname,", [
            ("customers", "id", False, "integer"),
            ("customers", "name", True, None),
            ("parent", "id", False, "integer"),
            ("remote", "id", False, "integer"),
        ]),
        ("k.contype IN ('p', 'u')", [
            ("customers", "p", ["id"], "shop", None, None, []), ("parent", "p", ["id"], "shop", None, None, []),
            ("remote", "u", ["id"], "shop", None, None, []),
        ]),
        ("k.contype IN ('f')", []),
        ("pg_catalog.pg_index", []),
    ]

    def __init__(self, log: list[str]):
        self.log = log
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, operation: str, parameters: Any = (), /) -> None:
        self.log.append(operation)
        self.rows = next(rows for marker, rows in self.ANSWERS if marker in operation)

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
    # A nondeterministic collation leaves the column untyped.
    assert [column.data_type for column in customers.columns] == ["integer", None]
    # An inheritance parent and a foreign table are columns only, all nullable.
    for name in ("public.parent", "remote"):
        assert found[name].primary_key == () and found[name].unique_sets == ()
        assert all(column.nullable for column in found[name].columns)
    assert any("k.conenforced" in operation for operation in connection.log)  # PostgreSQL 18.


class _CaseCursor(_ScriptedCursor):
    """A search path `s1, s2` with `s1."T"` (its key `id`) and `s2.t`, `s2."U"`
    (no keys): names that differ only in case are distinct relations."""

    ANSWERS = [
        ("current_schemas(true)", [("shop", ["pg_catalog", "s1", "s2"], 170000)]),
        ("c.relhassubclass", [
            ("shop", "s1", "T", "r", False), ("shop", "s2", "t", "r", False), ("shop", "s2", "U", "r", False),
        ]),
        ("a.attname,", [("T", "id", False, "integer"), ("t", "id", False, "integer"), ("U", "id", False, "integer")]),
        ("k.contype IN ('p', 'u')", [("T", "p", ["id"], "shop", None, None, [])]),
        ("k.contype IN ('f')", []),
        ("pg_catalog.pg_index", []),
    ]


class _CaseConnection(_ScriptedConnection):
    def cursor(self) -> _ScriptedCursor:
        return _CaseCursor(self.log)


def test_a_name_never_resolves_to_a_relation_spelled_in_another_case():
    found = PostgresKnowledgeFetcher(_CaseConnection()).fetch_relation_metadata(("t", "T", "u", "U"))
    # Unquoted t (and "t") is s2.t; s1."T" shadows it only under a case-insensitive lookup.
    # "T" may be either: written unquoted it means t. U is s2."U" whichever: unquoted u, U do not exist.
    assert {name: metadata.identity for name, metadata in found.items()} == {"t": "shop.s2.t", "U": "shop.s2.U"}
