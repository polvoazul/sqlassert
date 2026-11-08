"""The warehouse fetchers (Snowflake, BigQuery, Databricks, Redshift, Trino) over
scripted DB-API connections. No warehouse runs here: each catalog query is
answered with the rows that warehouse returns for a `customers` table and an
`orders` table whose foreign key references it, so what is tested is the
queries' shape (where they read, their parameter style) and how their rows
become metadata and proofs. Warehouse keys are not enforced, so they prove
only under `trust_unenforced_keys`."""

from __future__ import annotations

from typing import Any

import pytest

from sqlassert import Outcome, analyze_database
from sqlassert.diagnostics import UNENFORCED_CONSTRAINT
from sqlassert.discovery import DatabaseKnowledgeFetcher, DatabaseMetadataError, DatabaseRelationKind
from sqlassert.discovery.bigquery import BigQueryKnowledgeFetcher
from sqlassert.discovery.databricks import DatabricksKnowledgeFetcher
from sqlassert.discovery.information_schema import InformationSchemaKnowledgeFetcher
from sqlassert.discovery.redshift import RedshiftKnowledgeFetcher
from sqlassert.discovery.snowflake import SnowflakeKnowledgeFetcher
from sqlassert.discovery.trino import TrinoKnowledgeFetcher

type Answers = list[tuple[str, list[tuple[Any, ...]]]]


class _Cursor:
    def __init__(self, answers: Answers, log: list[tuple[str, Any]]):
        self.answers = answers
        self.log = log
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, operation: str, parameters: Any = (), /) -> None:
        self.log.append((operation, parameters))
        self.rows = next(rows for marker, rows in self.answers if marker in operation)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, answers: Answers):
        self.answers = answers
        self.log: list[tuple[str, Any]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.answers, self.log)


EXACTLY_ONE = "SELECT o.id, c.email FROM orders o /**ASSERT EXACTLY ONE**/ JOIN customers c ON c.id = o.customer_id"


def _outcomes(fetcher: DatabaseKnowledgeFetcher, dialect: str, *, trust: bool = False, sql: str = EXACTLY_ONE) -> list[Outcome]:
    report = analyze_database(sql, fetcher, dialect=dialect, trust_unenforced_keys=trust)
    assert report.diagnostics == ()
    return [assertion.outcome for assertion in report.assertions]


def _standard(catalog: str, schema: str) -> Answers:
    """What a standard INFORMATION_SCHEMA answers."""
    return [
        ("FROM INFORMATION_SCHEMA.TABLES", [
            (catalog, schema, "customers", "BASE TABLE"), (catalog, schema, "orders", "BASE TABLE"), (catalog, schema, "paid", "VIEW"),
        ]),
        ("IS_NULLABLE, DATA_TYPE", [
            ("customers", "id", "NO", "BIGINT"), ("customers", "email", "NO", "STRING"),
            ("orders", "id", "NO", "BIGINT"), ("orders", "customer_id", "NO", "BIGINT"),
        ]),
        ("REFERENTIAL_CONSTRAINTS", [("orders", "orders_customer", "customer_id", catalog, schema, "customers", "id")]),
        ("TABLE_CONSTRAINTS", [
            ("customers", "customers_pk", "PRIMARY KEY", "id"), ("orders", "orders_pk", "PRIMARY KEY", "id"),
        ]),
        ("VIEW_DEFINITION", [("SELECT id, email FROM customers",)]),
    ]


def test_databricks_reads_the_standard_views_with_qmark_parameters():
    connection = _Connection([("current_catalog()", [("main", "shop")]), *_standard("main", "shop")])
    fetcher = DatabricksKnowledgeFetcher(connection)

    found = fetcher.fetch_relation_metadata(("customers", "orders", "main.shop.paid", "other.shop.customers"))

    assert found["customers"].identity == "main.shop.customers" and found["customers"].primary_key == ("id",)
    reference, = found["orders"].foreign_keys
    assert (reference.columns, reference.referenced, reference.referenced_columns) == (("customer_id",), "main.shop.customers", ("id",))
    assert found["main.shop.paid"].kind is DatabaseRelationKind.VIEW
    assert "other.shop.customers" not in found  # Another catalog is not looked up.
    assert all("%s" not in operation for operation, _ in connection.log)
    assert any("?" in operation for operation, _ in connection.log)


def test_unenforced_catalog_keys_prove_only_when_trusted_which_is_the_default():
    def fetcher() -> DatabricksKnowledgeFetcher:
        return DatabricksKnowledgeFetcher(_Connection([("current_catalog()", [("main", "shop")]), *_standard("main", "shop")]))

    report = analyze_database(EXACTLY_ONE, fetcher(), dialect="databricks", trust_unenforced_keys=False)

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert {warning.code for warning in report.warnings} == {UNENFORCED_CONSTRAINT}
    assert any("trust_unenforced_keys=True" in warning.message for warning in report.warnings)
    assert _outcomes(fetcher(), "databricks", trust=True) == [Outcome.PROVED]
    assert [assertion.outcome for assertion in analyze_database(EXACTLY_ONE, fetcher(), dialect="databricks").assertions] == [Outcome.PROVED]


def test_not_null_from_a_warehouse_catalog_is_knowledge():
    connection = _Connection([("current_catalog()", [("main", "shop")]), *_standard("main", "shop")])

    assert _outcomes(DatabricksKnowledgeFetcher(connection), "databricks", sql="SELECT email FROM customers /**ASSERT NOT NULL(email)**/") == [Outcome.PROVED]


def test_a_view_body_is_wrapped_in_create_view():
    fetcher = DatabricksKnowledgeFetcher(_Connection([("current_catalog()", [("main", "shop")]), *_standard("main", "shop")]))
    view = fetcher.fetch_relation_metadata(("paid",))["paid"]

    assert fetcher.fetch_view_definition(view).sql == "CREATE VIEW `paid` AS SELECT id, email FROM customers"
    assert _outcomes(fetcher, "databricks", trust=True, sql="SELECT id FROM paid /**ASSERT UNIQUE(id)**/") == [Outcome.PROVED]


SNOWFLAKE: Answers = [
    ("CURRENT_DATABASE()", [("SHOP", "PUBLIC")]),
    ("FROM INFORMATION_SCHEMA.TABLES", [
        ("SHOP", "PUBLIC", "CUSTOMERS", "BASE TABLE"), ("SHOP", "PUBLIC", "ORDERS", "BASE TABLE"), ("SHOP", "PUBLIC", "PAID", "VIEW"),
    ]),
    ("IS_NULLABLE, DATA_TYPE", [
        ("CUSTOMERS", "ID", "NO", "NUMBER"), ("CUSTOMERS", "EMAIL", "NO", "TEXT"), ("CUSTOMERS", "CODE", "YES", "TEXT"),
        ("ORDERS", "ID", "NO", "NUMBER"), ("ORDERS", "CUSTOMER_ID", "NO", "NUMBER"),
    ]),
    # created_on, database, schema, table, column, key_sequence, constraint_name, rely, comment
    ("SHOW PRIMARY KEYS", [
        (None, "SHOP", "PUBLIC", "CUSTOMERS", "ID", 1, "PK_CUSTOMERS", "false", None),
        (None, "SHOP", "PUBLIC", "ORDERS", "ID", 1, "PK_ORDERS", "false", None),
        (None, "SHOP", "PUBLIC", "ELSEWHERE", "ID", 1, "PK_ELSEWHERE", "false", None),
    ]),
    ("SHOW UNIQUE KEYS", [
        (None, "SHOP", "PUBLIC", "CUSTOMERS", "CODE", 2, "UQ_EMAIL_CODE", "false", None),
        (None, "SHOP", "PUBLIC", "CUSTOMERS", "EMAIL", 1, "UQ_EMAIL_CODE", "false", None),
    ]),
    # created_on, pk database, schema, table, column, fk database, schema, table, column, key_sequence,
    # update_rule, delete_rule, fk_name, pk_name, deferrability, rely, comment
    ("SHOW IMPORTED KEYS", [
        (None, "SHOP", "PUBLIC", "CUSTOMERS", "ID", "SHOP", "PUBLIC", "ORDERS", "CUSTOMER_ID", 1,
         "NO ACTION", "NO ACTION", "FK_CUSTOMER", "PK_CUSTOMERS", "NOT DEFERRABLE", "false", None),
    ]),
    ("VIEW_DEFINITION", [("create or replace view PAID as select id, email from customers;",)]),
]


def test_snowflake_reads_keys_from_show_and_views_as_stored():
    connection = _Connection(SNOWFLAKE)
    fetcher = SnowflakeKnowledgeFetcher(connection)

    found = fetcher.fetch_relation_metadata(("customers", "public.orders", "paid"))

    customers, orders = found["customers"], found["public.orders"]
    assert customers.identity == "SHOP.PUBLIC.CUSTOMERS"
    assert customers.primary_key == ("ID",) and customers.unique_sets == (("EMAIL", "CODE"),)  # In key_sequence order.
    reference, = orders.foreign_keys
    assert (reference.columns, reference.referenced, reference.referenced_columns) == (("CUSTOMER_ID",), "SHOP.PUBLIC.CUSTOMERS", ("ID",))
    assert any('SHOW PRIMARY KEYS IN SCHEMA "SHOP"."PUBLIC"' in operation for operation, _ in connection.log)
    assert fetcher.fetch_view_definition(found["paid"]).sql == "create or replace view PAID as select id, email from customers"
    assert _outcomes(SnowflakeKnowledgeFetcher(_Connection(SNOWFLAKE)), "snowflake") == [Outcome.UNKNOWN]
    assert _outcomes(SnowflakeKnowledgeFetcher(_Connection(SNOWFLAKE)), "snowflake", trust=True) == [Outcome.PROVED]


def test_bigquery_reads_each_datasets_own_information_schema():
    answers: Answers = [
        ("TABLE_TYPE", [
            ("proj", "shop", "customers", "BASE TABLE"), ("proj", "shop", "orders", "BASE TABLE"),
        ]),
        ("IS_NULLABLE, DATA_TYPE", [
            ("customers", "id", "NO", "INT64"), ("customers", "region", "NO", "STRING"),
            ("orders", "id", "NO", "INT64"), ("orders", "customer_id", "NO", "INT64"), ("orders", "region", "NO", "STRING"),
        ]),
        ("CONSTRAINT_TYPE = 'FOREIGN KEY'", [
            ("orders", "orders_customer", "customer_id"),
            ("orders", "orders_region", "customer_id"), ("orders", "orders_region", "region"),
        ]),
        ("CONSTRAINT_COLUMN_USAGE", [("orders_customer", "proj", "shop", "customers", "id")]),
        ("TABLE_CONSTRAINTS", [("customers", "customers.pk$", "PRIMARY KEY", "id")]),
    ]
    connection = _Connection(answers)
    fetcher = BigQueryKnowledgeFetcher(connection, "proj", "shop")

    found = fetcher.fetch_relation_metadata(("customers", "shop.orders", "other.shop.orders"))

    assert found["customers"].identity == "proj.shop.customers" and found["customers"].primary_key == ("id",)
    # The two-column foreign key is skipped: CONSTRAINT_COLUMN_USAGE does not order its columns.
    reference, = found["shop.orders"].foreign_keys
    assert (reference.columns, reference.referenced, reference.referenced_columns) == (("customer_id",), "proj.shop.customers", ("id",))
    # Any project may be named; each dataset is read where it lives.
    assert any("`proj`.`shop`.INFORMATION_SCHEMA.TABLES" in operation for operation, _ in connection.log)
    assert any("`other`.`shop`.INFORMATION_SCHEMA.TABLES" in operation for operation, _ in connection.log)


def test_redshift_reads_view_text_from_pg_get_viewdef():
    connection = _Connection([
        ("current_schema()", [("dev", "public")]),
        ("pg_get_viewdef", [(" SELECT customers.id FROM customers;",)]),
        *_standard("dev", "public"),
    ])
    fetcher = RedshiftKnowledgeFetcher(connection)

    view = fetcher.fetch_relation_metadata(("paid",))["paid"]

    assert fetcher.fetch_view_definition(view).sql == 'CREATE VIEW "paid" AS SELECT customers.id FROM customers'
    assert _outcomes(RedshiftKnowledgeFetcher(_Connection(connection.answers)), "redshift", trust=True) == [Outcome.PROVED]


def test_trino_reads_each_catalogs_own_information_schema_and_no_keys():
    connection = _Connection([
        ("CURRENT_CATALOG", [("memory", "shop")]),
        ("information_schema.TABLES", [("memory", "shop", "customers", "BASE TABLE"), ("memory", "shop", "orders", "BASE TABLE"), ("other", "shop", "customers", "BASE TABLE")]),
        *_standard("memory", "shop"),
    ])
    fetcher = TrinoKnowledgeFetcher(connection)

    found = fetcher.fetch_relation_metadata(("customers", "orders", "other.shop.customers"))

    assert found["customers"].identity == "memory.shop.customers" and found["customers"].primary_key == ()
    assert found["orders"].foreign_keys == ()
    assert found["other.shop.customers"].identity == "other.shop.customers"
    assert any('"memory".information_schema.TABLES' in operation for operation, _ in connection.log)
    assert any('"other".information_schema.TABLES' in operation for operation, _ in connection.log)
    assert not any("CONSTRAINT" in operation for operation, _ in connection.log)
    assert all("%s" not in operation for operation, _ in connection.log)
    assert _outcomes(TrinoKnowledgeFetcher(_Connection(connection.answers)), "trino", trust=True) == [Outcome.UNKNOWN]


def test_names_that_differ_only_in_case_do_not_resolve():
    class Standard(InformationSchemaKnowledgeFetcher):
        pass

    answers: Answers = [
        ("CURRENT_CATALOG", [("db", "main")]),
        ("FROM INFORMATION_SCHEMA.TABLES", [("db", "main", "Orders", "BASE TABLE"), ("db", "main", "orders", "BASE TABLE")]),
    ]

    assert Standard(_Connection(answers)).fetch_relation_metadata(("orders",)) == {}


def test_a_key_with_an_expression_part_is_not_a_key():
    answers: Answers = [
        ("CURRENT_CATALOG", [("db", "main")]),
        ("FROM INFORMATION_SCHEMA.TABLES", [("db", "main", "t", "BASE TABLE")]),
        ("IS_NULLABLE, DATA_TYPE", [("t", "a", "NO", "INT")]),
        ("REFERENTIAL_CONSTRAINTS", []),
        ("TABLE_CONSTRAINTS", [("t", "t_a_lower_b", "UNIQUE", "a"), ("t", "t_a_lower_b", "UNIQUE", None)]),
    ]

    class Standard(InformationSchemaKnowledgeFetcher):
        pass

    assert Standard(_Connection(answers)).fetch_relation_metadata(("t",))["t"].unique_sets == ()


def test_a_trino_session_without_a_catalog_is_a_metadata_error():
    fetcher = TrinoKnowledgeFetcher(_Connection([("CURRENT_CATALOG", [(None, None)])]))

    with pytest.raises(DatabaseMetadataError, match="no current catalog"):
        fetcher.fetch_relation_metadata(("customers",))


def test_a_catalog_keeps_the_warnings_about_the_sql_itself():
    connection = _Connection([
        ("CURRENT_CATALOG", [("memory", "shop")]),
        ("information_schema.TABLES", [("memory", "shop", "events", "BASE TABLE")]),
        ("IS_NULLABLE, DATA_TYPE", [("events", "id", "NO", "BIGINT"), ("events", "at", "YES", "TIMESTAMP(3) WITH TIME ZONE")]),
        ("VIEW_DEFINITION", []),
    ])
    sql = "SELECT id FROM events WHERE CAST(at AS VARCHAR) = '2024-01-01 00:00:00.000 UTC' /**ASSERT NOT NULL(id)**/"

    report = analyze_database(sql, TrinoKnowledgeFetcher(connection), dialect="trino")

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert [warning.code for warning in report.warnings] == ["zoned-timestamp-cast"]
