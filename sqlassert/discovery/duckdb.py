"""DuckDB: the standard discovery, with DuckDB's `duckdb_*()` catalog functions.

Every hook reads `duckdb_tables()`, `duckdb_views()`, `duckdb_columns()` or
`duckdb_constraints()`; it never scans a table. Any DuckDB connection works
(the `duckdb` module's, or one from any other client exposing `execute`), and
sqlassert does not import a DuckDB driver.

Queries run on the connection itself, not on a cursor: a DuckDB cursor is a
new session, without the connection's temporary tables, `USE` or
`search_path`, which decide what an unqualified name denotes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from sqlassert.discovery.fetcher import (
    DatabaseColumn,
    DatabaseForeignKey,
    DatabaseMetadataError,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
)
from sqlassert.discovery.information_schema import FoundRelation, InformationSchemaKnowledgeFetcher, Key

_SESSION = "SELECT current_database(), current_schema(), current_setting('search_path')"

_RELATIONS = """
SELECT database_name, schema_name, table_name, 'table' FROM duckdb_tables()
WHERE lower(database_name) = %s AND lower(schema_name) = %s AND list_contains(%s, lower(table_name))
UNION ALL
SELECT database_name, schema_name, view_name, 'view' FROM duckdb_views()
WHERE NOT internal AND lower(database_name) = %s AND lower(schema_name) = %s AND list_contains(%s, lower(view_name))
"""

_TABLES = "database_name = %s AND schema_name = %s AND list_contains(%s, table_name)"

_COLUMNS = f"""
SELECT table_name, column_name, is_nullable, data_type FROM duckdb_columns()
WHERE {_TABLES}
ORDER BY table_name, column_index
"""

# duckdb_columns() drops a column's COLLATE; the stored CREATE TABLE keeps it.
_TABLE_SQL = f"SELECT table_name, sql FROM duckdb_tables() WHERE {_TABLES}"

# A foreign key's referenced table is in the referencing table's own catalog
# and schema: DuckDB rejects foreign keys across them.
_CONSTRAINTS = f"""
SELECT table_name, constraint_type, constraint_column_names, referenced_table, referenced_column_names
FROM duckdb_constraints()
WHERE constraint_type IN ({{kinds}}) AND {_TABLES}
ORDER BY table_name, constraint_index
"""

_VIEW = "SELECT sql FROM duckdb_views() WHERE database_name = %s AND schema_name = %s AND view_name = %s"


class DuckDBConnection(Protocol):
    def execute(self, query: str, parameters: Any = ..., /) -> Any: ...


class DuckDBKnowledgeFetcher(InformationSchemaKnowledgeFetcher):
    type_dialect = "duckdb"
    cross_catalog = True
    _search_path: tuple[tuple[str, str], ...] = ()

    def __init__(self, connection: DuckDBConnection):
        self.database = connection

    def session(self, scope: DatabaseRelationMetadata | None) -> tuple[str, tuple[str, ...]]:
        """Where an unqualified name is looked up: a view's own schema first,
        then temporary objects, then the session's path. DuckDB's path spans
        catalogs, so `candidates` reads it from `_search_path`."""
        (database, schema, configured), = self._rows(_SESSION)
        path: list[tuple[str, str]] = [(scope.catalog, scope.schema)] if scope is not None else []
        path.append(("temp", "main"))
        for entry in filter(None, (entry.strip() for entry in configured.split(","))):
            qualifier, _, name = entry.rpartition(".")
            path.append((qualifier or database, name))
        path.append((database, schema))
        self._search_path = tuple((catalog.lower(), schema.lower()) for catalog, schema in path)
        return database, tuple(schema for _, schema in self._search_path)

    def candidates(self, name: str, catalog: str, path: tuple[str, ...]) -> list[tuple[str, str, str]]:
        """Every catalog path ``name`` may denote, in DuckDB's binding order."""
        match name.lower().split("."):
            case [table]:
                return [(searched, schema, table) for searched, schema in self._search_path]
            case [qualifier, table]:
                # A schema in a searched catalog first, then a catalog's default schema.
                catalogs = dict.fromkeys(searched for searched, _ in self._search_path)
                return [(searched, qualifier, table) for searched in catalogs] + [(qualifier, "main", table)]
            case [named, schema, table]:
                return [(named, schema, table)]
            case _:
                return []

    def relations(self, catalog: str, schema: str, names: Sequence[str]) -> list[FoundRelation]:
        lookup = (catalog.lower(), schema.lower(), list(names))
        return [
            FoundRelation(database, found_schema, name, DatabaseRelationKind(kind))
            for database, found_schema, name, kind in self._rows(_RELATIONS, lookup * 2)
        ]

    def columns(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseColumn]]:
        lookup = (catalog, schema, list(tables))
        collated = self._collated(catalog, schema, lookup)
        return [
            (table, DatabaseColumn(column, nullable, None if (table, column.lower()) in collated else data_type))
            for table, column, nullable, data_type in self._rows(_COLUMNS, lookup)
        ]

    def keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[Key]:
        return [
            Key(table, tuple(members), kind == "PRIMARY KEY")
            for table, kind, members, *_ in self._rows(_CONSTRAINTS.format(kinds="'PRIMARY KEY', 'UNIQUE'"), (catalog, schema, list(tables)))
        ]

    def foreign_keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseForeignKey]]:
        return [
            (table, DatabaseForeignKey(tuple(members), f"{catalog}.{schema}.{referenced}", tuple(referenced_members)))
            for table, _, members, referenced, referenced_members
            in self._rows(_CONSTRAINTS.format(kinds="'FOREIGN KEY'"), (catalog, schema, list(tables)))
        ]

    def view_definition(self, relation: DatabaseRelationMetadata) -> str | None:
        rows = self._rows(_VIEW, (relation.catalog, relation.schema, relation.name))
        return rows[0][0] if rows else None

    def _collated(self, catalog: str, schema: str, lookup: tuple[Any, ...]) -> set[tuple[str, str]]:
        """Each (table, lowercase column) declared with a COLLATE: sqlassert
        supports the default collation only, so such a column is untyped. The
        session's `default_collation` is not read: text without a COLLATE has
        the engine's shipped (binary) default (docs/dialects.md)."""
        collated: set[tuple[str, str]] = set()
        for table, sql in self._rows(_TABLE_SQL, lookup):
            try:
                create = sqlglot.parse_one(sql, read="duckdb")
            except SqlglotError as error:
                raise DatabaseMetadataError(f"definition of table {catalog}.{schema}.{table} could not be parsed: {error}") from error
            collated.update(
                (table, definition.name.lower()) for definition in create.find_all(exp.ColumnDef)
                if any(isinstance(constraint.args.get("kind"), exp.CollateColumnConstraint) for constraint in definition.constraints)
            )
        return collated

    def _rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """The rows of one catalog query, run on the connection (see the module
        docstring). Any driver error is a DatabaseMetadataError."""
        try:
            return [tuple(row) for row in self.database.execute(sql.replace("%s", "?"), list(parameters)).fetchall()]
        except Exception as error:
            raise DatabaseMetadataError(str(error)) from error
