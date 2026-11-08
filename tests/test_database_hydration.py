"""Generic Database Knowledge hydration, exercised with a fake fetcher.

The fetcher resolves the external Relations a SQL Program reaches, expands
catalog views recursively through the ordinary parser and linker, rebuilds the
final IR, and binds catalog metadata to that IR as Knowledge. The engine only
ever sees IR-bound Properties.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from sqlassert import Outcome
from sqlassert import ir
from sqlassert.diagnostics import (
    CATALOG_METADATA_UNAVAILABLE,
    CATALOG_RELATION_NOT_FOUND,
    INCOMPLETE_CATALOG_METADATA,
    RECURSIVE_VIEW_DEFINITION,
)
from sqlassert.discovery import (
    DatabaseColumn,
    DatabaseForeignKey,
    DatabaseKnowledgeFetcher,
    DatabaseMetadataError,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
    DatabaseViewDefinition,
)
from sqlassert.main import analyze_database
from sqlassert.properties import Property
from sqlassert.sql_parse import SqlParser


def table(name: str, *columns: str, nullable: tuple[str, ...] = (), primary_key: tuple[str, ...] = (), unique: tuple[tuple[str, ...], ...] = (), schema: str = "main") -> DatabaseRelationMetadata:
    return DatabaseRelationMetadata(
        catalog="db", schema=schema, name=name, kind=DatabaseRelationKind.TABLE,
        columns=tuple(DatabaseColumn(name=column, nullable=column in nullable, data_type="INTEGER") for column in columns),
        primary_key=primary_key, unique_sets=unique,
    )


def view(name: str, schema: str = "main") -> DatabaseRelationMetadata:
    return DatabaseRelationMetadata(catalog="db", schema=schema, name=name, kind=DatabaseRelationKind.VIEW)


class FakeFetcher(DatabaseKnowledgeFetcher):
    """Resolves a name first in the scope's schema, then in `main`, then as a full identity."""

    def __init__(self, relations: tuple[DatabaseRelationMetadata, ...] = (), views: Mapping[str, str] | None = None, fail: bool = False):
        self.relations = relations
        self.views = dict(views or {})
        self.fail = fail
        self.requested: list[str] = []
        self.view_requests: list[str] = []

    def fetch_relation_metadata(self, names, scope=None):
        if self.fail:
            raise DatabaseMetadataError("permission denied for schema main")
        self.requested.extend(names)
        by_path = {(relation.catalog, relation.schema, relation.name): relation for relation in self.relations}
        schemas = ([scope.schema] if scope is not None else []) + ["main"]
        found = {}
        for name in names:
            parts = tuple(name.lower().split("."))
            paths = {3: [parts], 2: [("db", *parts)], 1: [("db", schema, *parts) for schema in schemas]}[len(parts)]
            relation = next((by_path[path] for path in paths if path in by_path), None)
            if relation is not None:
                found[name] = relation
        return found

    def fetch_view_definition(self, relation):
        self.view_requests.append(relation.identity)
        return DatabaseViewDefinition(sql=self.views[relation.identity])


USERS = table("users", "id", "email", nullable=("email",), primary_key=("id",))
ORDERS = table("orders", "id", "user_id", nullable=("user_id",), primary_key=("id",))

UNIQUE_JOIN = """
SELECT *
FROM orders
/**ASSERT MANY TO ONE**/ JOIN users ON orders.user_id = users.id
"""


def test_a_unique_join_proves_against_a_catalog_primary_key():
    fetcher = FakeFetcher((USERS, ORDERS))

    report = analyze_database(UNIQUE_JOIN, fetcher)

    assert report.proved
    assert sorted(fetcher.requested) == ["orders", "users"]


def test_relations_are_found_through_the_preliminary_ir_so_a_cte_is_never_fetched():
    fetcher = FakeFetcher((USERS,))

    report = analyze_database(
        """
        WITH users AS (SELECT 1 AS id UNION ALL SELECT 1)
        SELECT * FROM users /**ASSERT UNIQUE(id)**/
        """,
        fetcher,
    )

    assert fetcher.requested == []
    assert not report.proved


def test_repeated_references_fetch_once_but_keep_distinct_aliases():
    fetcher = FakeFetcher((USERS,))
    sql = """
    SELECT *
    FROM users AS a
    /**ASSERT MANY TO ONE**/ JOIN users AS b ON a.id = b.id
    """

    hydration = fetcher.hydrate(SqlParser("duckdb").parse(sql), dialect="duckdb")

    assert fetcher.requested == ["users"]
    aliases = [relation for relation in hydration.conversion.program.relations() if isinstance(relation, ir.Alias)]
    assert len(aliases) == 2
    assert aliases[0].source is aliases[1].source
    assert analyze_database(sql, FakeFetcher((USERS,))).proved


def test_catalog_views_expand_recursively_through_the_parser_and_linker():
    fetcher = FakeFetcher(
        (ORDERS, USERS, view("active_users"), view("recent_users")),
        views={
            "db.main.active_users": "CREATE VIEW active_users AS SELECT * FROM recent_users WHERE id > 0",
            "db.main.recent_users": "CREATE VIEW recent_users AS SELECT id, email FROM users",
        },
    )

    report = analyze_database(
        """
        SELECT *
        FROM orders
        /**ASSERT MANY TO ONE**/ JOIN active_users ON orders.user_id = active_users.id
        """,
        fetcher,
    )

    assert report.proved
    assert fetcher.view_requests == ["db.main.active_users", "db.main.recent_users"]


def test_a_view_body_resolves_names_in_the_view_schema():
    """`s.v` reads `s.users`, which has no key, not `main.users`."""
    fetcher = FakeFetcher(
        (ORDERS, USERS, table("users", "id", schema="s"), view("v", schema="s")),
        views={"db.s.v": "CREATE VIEW s.v AS SELECT id FROM users"},
    )

    report = analyze_database(
        """
        SELECT *
        FROM orders
        /**ASSERT MANY TO ONE**/ JOIN s.v ON orders.user_id = v.id
        """,
        fetcher,
    )

    assert not report.proved


def test_recursive_catalog_views_terminate_with_a_diagnostic():
    fetcher = FakeFetcher(
        (ORDERS, view("a"), view("b")),
        views={
            "db.main.a": "CREATE VIEW a AS SELECT * FROM b",
            "db.main.b": "CREATE VIEW b AS SELECT * FROM a",
        },
    )

    report = analyze_database(
        """
        SELECT *
        FROM orders
        /**ASSERT MANY TO ONE**/ JOIN a ON orders.user_id = a.id
        """,
        fetcher,
    )

    assert not report.proved
    assert RECURSIVE_VIEW_DEFINITION in {diagnostic.code for diagnostic in report.diagnostics}
    assert sorted(fetcher.view_requests) == ["db.main.a", "db.main.b"]


def test_a_missing_relation_is_a_diagnostic_and_no_proof():
    report = analyze_database(UNIQUE_JOIN, FakeFetcher((ORDERS,)))

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert [diagnostic.code for diagnostic in report.diagnostics] == [CATALOG_RELATION_NOT_FOUND]


def test_a_view_reading_a_missing_relation_is_not_expanded():
    fetcher = FakeFetcher(
        (ORDERS, USERS, view("v")),
        views={"db.main.v": "CREATE VIEW v AS SELECT users.id FROM users JOIN ghosts ON users.id = ghosts.id"},
    )

    report = analyze_database("SELECT id FROM v /**ASSERT UNIQUE(id)**/", fetcher)

    assert not report.proved
    assert CATALOG_RELATION_NOT_FOUND in {diagnostic.code for diagnostic in report.diagnostics}


def test_a_catalog_failure_is_a_diagnostic_and_no_proof():
    report = analyze_database(UNIQUE_JOIN, FakeFetcher((USERS, ORDERS), fail=True))

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert {diagnostic.code for diagnostic in report.diagnostics} == {CATALOG_METADATA_UNAVAILABLE}


@pytest.mark.parametrize(
    "users",
    [
        table("users", "email", primary_key=("id",)),
        table("users", "id", unique=(("id", "missing"),)),
        table("users"),
    ],
    ids=["primary-key-names-unknown-column", "unique-set-names-unknown-column", "no-columns"],
)
def test_incomplete_metadata_is_a_diagnostic_and_no_proof(users):
    report = analyze_database(UNIQUE_JOIN, FakeFetcher((users, ORDERS)))

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert INCOMPLETE_CATALOG_METADATA in {diagnostic.code for diagnostic in report.diagnostics}


def test_sql_declared_relations_are_not_fetched_or_overwritten():
    fetcher = FakeFetcher((USERS, ORDERS))

    report = analyze_database("CREATE TABLE users (id INTEGER);" + UNIQUE_JOIN, fetcher)

    assert fetcher.requested == ["orders"]
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_composite_and_nullable_unique_sets_keep_current_semantics():
    memberships = table("memberships", "org_id", "user_id", nullable=("user_id",), unique=(("org_id", "user_id"),))

    unique = analyze_database("SELECT org_id, user_id FROM memberships /**ASSERT UNIQUE(org_id, user_id)**/", FakeFetcher((memberships,)))
    key = analyze_database("SELECT org_id, user_id FROM memberships /**ASSERT PRIMARY KEY(org_id, user_id)**/", FakeFetcher((memberships,)))

    assert unique.proved
    assert not key.proved


def test_a_candidate_key_assertion_uses_catalog_uniqueness_and_non_nullness():
    report = analyze_database("SELECT id, email FROM users /**ASSERT PRIMARY KEY(id)**/", FakeFetcher((USERS,)))

    assert report.proved


def test_bound_knowledge_holds_only_properties_over_final_ir_nodes():
    fetcher = FakeFetcher((USERS, ORDERS))
    hydration = fetcher.hydrate(SqlParser("duckdb").parse(UNIQUE_JOIN), dialect="duckdb")
    program = hydration.conversion.program

    facts = fetcher.bind(program, hydration)

    assert facts.knowledge and not facts.declarations
    for fact in facts.knowledge:
        assert isinstance(fact, Property)
        for value in vars(fact).values():
            members = value if isinstance(value, frozenset) else {value}
            for member in members:
                assert isinstance(member, ir.OutputColumn)
                assert member in program.column_to_parent_relation_index


def test_a_catalog_foreign_key_binds_when_its_referenced_relation_is_reached():
    payments = DatabaseRelationMetadata(
        catalog="db", schema="main", name="payments", kind=DatabaseRelationKind.TABLE,
        columns=(DatabaseColumn(name="user_id", nullable=True),),
        foreign_keys=(DatabaseForeignKey(("user_id",), "db.main.users", ("id",)),),
    )
    fetcher = FakeFetcher((USERS, payments))

    proved = analyze_database("SELECT * FROM payments /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/", fetcher)
    unreached = analyze_database("SELECT * FROM payments", fetcher)

    assert proved.proved
    assert unreached.proved
    assert "users" in fetcher.requested


def test_a_catalog_foreign_key_over_unlisted_columns_is_incomplete_metadata():
    payments = DatabaseRelationMetadata(
        catalog="db", schema="main", name="payments", kind=DatabaseRelationKind.TABLE,
        columns=(DatabaseColumn(name="user_id", nullable=True),),
        foreign_keys=(DatabaseForeignKey(("missing",), "db.main.users", ("id",)),),
    )

    report = analyze_database("SELECT * FROM payments", FakeFetcher((USERS, payments)))

    assert [d.code for d in report.diagnostics] == [INCOMPLETE_CATALOG_METADATA]
