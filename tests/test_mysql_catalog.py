"""`MySQLKnowledgeFetcher` over a scripted DB-API connection: each catalog
query is answered with the rows MySQL 8's information_schema returns for
the schema below. No MySQL runs here; the queries' SQL is not checked, only how
their rows become metadata and proofs."""

from __future__ import annotations

from typing import Any

from sqlassert import Outcome, analyze_database
from sqlassert.discovery import DatabaseRelationKind
from sqlassert.discovery.mysql import MySQLKnowledgeFetcher

# CREATE TABLE customers (id INT PRIMARY KEY, email VARCHAR(50) NOT NULL UNIQUE,
#     status ENUM('new', 'paid') NOT NULL, code VARCHAR(10) COLLATE utf8mb4_0900_bin,
#     UNIQUE KEY lower_email ((lower(email))), UNIQUE KEY lower_code (code, (lower(email))));
# CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT NOT NULL,
#     FOREIGN KEY (customer_id) REFERENCES customers (id));
# CREATE VIEW paid AS SELECT id, email FROM customers WHERE status = 'paid';
ANSWERS: dict[str, list[tuple[Any, ...]]] = {
    "SELECT 'def', DATABASE()": [("def", "shop")],
    "SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE": [
        ("def", "shop", "customers", "BASE TABLE"), ("def", "shop", "orders", "BASE TABLE"), ("def", "shop", "paid", "VIEW"),
    ],
    "SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE, COLUMN_TYPE": [
        ("customers", "id", "NO", "int"),
        ("customers", "email", "NO", "varchar(50)"),
        ("customers", "status", "NO", "enum('new','paid')"),
        ("customers", "code", "YES", "varchar(10)"),
        ("orders", "id", "NO", "int"),
        ("orders", "customer_id", "NO", "int"),
    ],
    "SELECT TABLE_NAME, INDEX_NAME": [
        ("customers", "PRIMARY", "id"),
        ("customers", "email", "email"),
        ("customers", "lower_email", None),
        ("customers", "lower_code", "code"),
        ("customers", "lower_code", None),
        ("orders", "PRIMARY", "id"),
    ],
    "SELECT TABLE_NAME, CONSTRAINT_NAME": [
        ("orders", "orders_ibfk_1", "customer_id", "def", "shop", "customers", "id"),
    ],
    "SELECT VIEW_DEFINITION": [
        ("select `shop`.`customers`.`id` AS `id`,`shop`.`customers`.`email` AS `email` from `shop`.`customers` "
         "where (`shop`.`customers`.`status` = 'paid')",),
    ],
}


class _Cursor:
    def __init__(self, log: list[tuple[str, Any]]):
        self.log = log
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, operation: str, parameters: Any = (), /) -> None:
        self.log.append((operation, parameters))
        self.rows = next(rows for prefix, rows in ANSWERS.items() if operation.strip().startswith(prefix))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self) -> None:
        self.log: list[tuple[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.log)


def test_tables_have_columns_and_keys_over_plain_columns():
    connection = _Connection()
    found = MySQLKnowledgeFetcher(connection).fetch_relation_metadata(("customers", "shop.orders", "missing"))

    customers, orders = found["customers"], found["shop.orders"]
    assert customers.identity == "def.shop.customers" and customers.kind is DatabaseRelationKind.TABLE
    # Neither index with a functional key part is a key, nor its plain columns alone.
    assert customers.primary_key == ("id",) and customers.unique_sets == (("email",),)
    # Columns have the dialect's default collation: per-column ones are not read.
    assert [(column.name, column.nullable, column.data_type, column.collation) for column in customers.columns] == [
        ("id", False, "int", None), ("email", False, "varchar(50)", None), ("status", False, "enum('new','paid')", None),
        ("code", True, "varchar(10)", None),
    ]
    reference, = orders.foreign_keys
    assert (reference.columns, reference.referenced, reference.referenced_columns) == (("customer_id",), "def.shop.customers", ("id",))
    assert "missing" not in found
    # Names are values, never SQL text.
    assert all("customers" not in operation for operation, _ in connection.log)


def test_analysis_proves_from_the_catalog():
    sql = "SELECT o.id, c.email FROM orders o /**ASSERT EXACTLY ONE**/ JOIN customers c ON c.id = o.customer_id"
    report = analyze_database(sql, MySQLKnowledgeFetcher(_Connection()), dialect="mysql")

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert report.diagnostics == ()


def test_a_view_hydrates_through_its_definition():
    sql = "SELECT id, email FROM paid /**ASSERT NOT NULL UNIQUE(id)**/"
    report = analyze_database(sql, MySQLKnowledgeFetcher(_Connection()), dialect="mysql")

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert report.diagnostics == ()


def test_text_from_the_catalog_has_the_default_collation():
    """MySQL's default collations ignore case, so equal text is not identical
    text, even for a column declared with a binary collation."""
    sql = "SELECT code FROM customers WHERE code IN ('a', 'b') AND code = 'B' /**ASSERT ENUM(code IN ('a'))**/"
    assertion, = analyze_database(sql, MySQLKnowledgeFetcher(_Connection()), dialect="mysql").assertions

    assert assertion.outcome is Outcome.UNKNOWN


def test_an_enum_from_the_catalog_may_hold_the_empty_string():
    def outcome(values: str) -> Outcome:
        sql = f"SELECT status FROM customers /**ASSERT ENUM(status IN ({values}))**/"
        assertion, = analyze_database(sql, MySQLKnowledgeFetcher(_Connection()), dialect="mysql").assertions
        return assertion.outcome

    assert outcome("'new', 'paid'") is Outcome.UNKNOWN
    assert outcome("'new', 'paid', ''") is Outcome.PROVED
