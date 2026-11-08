"""`MySQLKnowledgeFetcher` over a scripted DB-API connection: each catalog
query is answered with the rows MySQL 8's information_schema returns for
`SCHEMA` below. No MySQL runs here; the queries' SQL is not checked, only how
their rows become metadata and proofs."""

from __future__ import annotations

from typing import Any

from sqlassert import Outcome, analyze_database
from sqlassert.discovery import DatabaseRelationKind
from sqlassert.discovery.mysql import MySQLKnowledgeFetcher

# CREATE TABLE customers (id INT PRIMARY KEY, email VARCHAR(50) NOT NULL UNIQUE,
#     status ENUM('new', 'paid') NOT NULL, code VARCHAR(10) COLLATE utf8mb4_0900_bin,
#     UNIQUE KEY lower_email ((lower(email))));
# CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT NOT NULL,
#     FOREIGN KEY (customer_id) REFERENCES customers (id));
# CREATE VIEW paid AS SELECT id, email FROM customers WHERE status = 'paid';
ANSWERS: dict[str, list[tuple[Any, ...]]] = {
    "SELECT DATABASE()": [("shop",)],
    "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE": [
        ("shop", "customers", "BASE TABLE"), ("shop", "orders", "BASE TABLE"), ("shop", "paid", "VIEW"), ("archive", "orders", "BASE TABLE"),
    ],
    "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME": [
        ("shop", "customers", "id", "NO", "int", None),
        ("shop", "customers", "email", "NO", "varchar(50)", "utf8mb4_0900_ai_ci"),
        ("shop", "customers", "status", "NO", "enum('new','paid')", "utf8mb4_0900_ai_ci"),
        ("shop", "customers", "code", "YES", "varchar(10)", "utf8mb4_0900_bin"),
        ("shop", "orders", "id", "NO", "int", None),
        ("shop", "orders", "customer_id", "NO", "int", None),
    ],
    "SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME": [
        ("shop", "customers", "PRIMARY", "id"),
        ("shop", "customers", "email", "email"),
        ("shop", "customers", "lower_email", None),
        ("shop", "orders", "PRIMARY", "id"),
    ],
    "SELECT TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME": [
        ("shop", "orders", "orders_ibfk_1", "customer_id", "shop", "customers", "id"),
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


def test_tables_have_columns_collations_and_keys_over_plain_columns():
    connection = _Connection()
    found = MySQLKnowledgeFetcher(connection).fetch_relation_metadata(("customers", "shop.orders", "missing"))

    customers, orders = found["customers"], found["shop.orders"]
    assert customers.identity == "def.shop.customers" and customers.kind is DatabaseRelationKind.TABLE
    assert customers.primary_key == ("id",) and customers.unique_sets == (("email",),)  # Not the functional index.
    assert [(column.name, column.nullable, column.collation) for column in customers.columns] == [
        ("id", False, None), ("email", False, "utf8mb4_0900_ai_ci"), ("status", False, "utf8mb4_0900_ai_ci"), ("code", True, "utf8mb4_0900_bin"),
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


def test_collations_from_the_catalog_decide_identity():
    def outcome(column: str) -> Outcome:
        sql = f"SELECT {column} FROM customers WHERE {column} IN ('a', 'b') AND {column} = 'B' /**ASSERT ENUM({column} IN ('a'))**/"
        assertion, = analyze_database(sql, MySQLKnowledgeFetcher(_Connection()), dialect="mysql").assertions
        return assertion.outcome

    assert outcome("email") is Outcome.UNKNOWN  # Case-insensitive.
    assert outcome("code") is Outcome.PROVED  # Binary, NO PAD.


def test_an_enum_from_the_catalog_may_hold_the_empty_string():
    def outcome(values: str) -> Outcome:
        sql = f"SELECT status FROM customers /**ASSERT ENUM(status IN ({values}))**/"
        assertion, = analyze_database(sql, MySQLKnowledgeFetcher(_Connection()), dialect="mysql").assertions
        return assertion.outcome

    assert outcome("'new', 'paid'") is Outcome.UNKNOWN
    assert outcome("'new', 'paid', ''") is Outcome.PROVED
