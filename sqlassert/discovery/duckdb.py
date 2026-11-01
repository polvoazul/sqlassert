"""The DuckDB catalog boundary: name resolution, metadata, and view definitions.

Reads DuckDB's `duckdb_*()` catalog functions only; it never scans a table
or executes a query over user data.
"""

from __future__ import annotations

from collections.abc import Mapping

import duckdb

from sqlassert.discovery.fetcher import (
    DatabaseColumn,
    DatabaseKnowledgeFetcher,
    DatabaseMetadataError,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
    DatabaseViewDefinition,
)

type _Path = tuple[str, str, str]

_RELATIONS = """
SELECT database_name, schema_name, table_name, 'table' FROM duckdb_tables()
WHERE lower(table_name) IN (SELECT unnest($names))
UNION ALL
SELECT database_name, schema_name, view_name, 'view' FROM duckdb_views()
WHERE NOT internal AND lower(view_name) IN (SELECT unnest($names))
"""

_COLUMNS = """
SELECT database_name, schema_name, table_name, column_name, is_nullable FROM duckdb_columns()
WHERE list_contains($paths, [database_name, schema_name, table_name])
ORDER BY database_name, schema_name, table_name, column_index
"""

_KEYS = """
SELECT database_name, schema_name, table_name, constraint_type, constraint_column_names FROM duckdb_constraints()
WHERE constraint_type IN ('PRIMARY KEY', 'UNIQUE') AND list_contains($paths, [database_name, schema_name, table_name])
ORDER BY database_name, schema_name, table_name, constraint_index
"""

_VIEW = "SELECT sql FROM duckdb_views() WHERE database_name = ? AND schema_name = ? AND view_name = ?"


class DuckDBKnowledgeFetcher(DatabaseKnowledgeFetcher):
    def __init__(self, connection: duckdb.DuckDBPyConnection):
        self.connection = connection

    def fetch_relation_metadata(
        self, names: tuple[str, ...], scope: DatabaseRelationMetadata | None = None
    ) -> Mapping[str, DatabaseRelationMetadata]:
        try:
            return self._fetch_relation_metadata(names, scope)
        except duckdb.Error as error:
            raise DatabaseMetadataError(str(error)) from error

    def fetch_view_definition(self, relation: DatabaseRelationMetadata) -> DatabaseViewDefinition:
        try:
            row = self.connection.execute(_VIEW, [relation.catalog, relation.schema, relation.name]).fetchone()
        except duckdb.Error as error:
            raise DatabaseMetadataError(str(error)) from error
        if row is None:
            raise DatabaseMetadataError(f"view {relation.identity} is no longer in the catalog")
        return DatabaseViewDefinition(sql=row[0])

    def _fetch_relation_metadata(
        self, names: tuple[str, ...], scope: DatabaseRelationMetadata | None
    ) -> dict[str, DatabaseRelationMetadata]:
        unqualified = sorted({name.rsplit(".", 1)[-1].lower() for name in names})
        catalog: dict[_Path, tuple[_Path, DatabaseRelationKind]] = {}
        for database, schema, table, kind in self.connection.execute(_RELATIONS, {"names": unqualified}).fetchall():
            path = (database, schema, table)
            catalog[_lower(path)] = (path, DatabaseRelationKind(kind))

        search_path = self._search_path(scope)
        resolved: dict[str, tuple[_Path, DatabaseRelationKind]] = {}
        for name in names:
            found = next((catalog[path] for path in _candidates(name, search_path) if path in catalog), None)
            if found is not None:
                resolved[name] = found

        tables = sorted({path for path, kind in resolved.values() if kind is DatabaseRelationKind.TABLE})
        columns: dict[_Path, list[DatabaseColumn]] = {}
        primary_keys: dict[_Path, tuple[str, ...]] = {}
        unique_sets: dict[_Path, list[tuple[str, ...]]] = {}
        if tables:
            paths = [list(path) for path in tables]
            for database, schema, table, column, nullable in self.connection.execute(_COLUMNS, {"paths": paths}).fetchall():
                columns.setdefault((database, schema, table), []).append(DatabaseColumn(name=column, nullable=nullable))
            for database, schema, table, constraint, members in self.connection.execute(_KEYS, {"paths": paths}).fetchall():
                if constraint == "PRIMARY KEY":
                    primary_keys[(database, schema, table)] = tuple(members)
                else:
                    unique_sets.setdefault((database, schema, table), []).append(tuple(members))

        metadata = {
            path: DatabaseRelationMetadata(
                catalog=path[0], schema=path[1], name=path[2], kind=kind,
                columns=tuple(columns.get(path, ())), primary_key=primary_keys.get(path, ()), unique_sets=tuple(unique_sets.get(path, ())),
            )
            for path, kind in resolved.values()
        }
        return {name: metadata[path] for name, (path, _) in resolved.items()}

    def _search_path(self, scope: DatabaseRelationMetadata | None) -> tuple[tuple[str, str], ...]:
        """Where an unqualified name is looked up: a view's own schema first, then temporary objects, then the session's path."""
        row = self.connection.execute("SELECT current_database(), current_schema(), current_setting('search_path')").fetchone()
        assert row is not None
        database, schema, configured = row
        path: list[tuple[str, str]] = [(scope.catalog, scope.schema)] if scope is not None else []
        path.append(("temp", "main"))
        for entry in filter(None, (entry.strip() for entry in configured.split(","))):
            qualifier, _, name = entry.rpartition(".")
            path.append((qualifier or database, name))
        path.append((database, schema))
        return tuple((catalog.lower(), schema.lower()) for catalog, schema in path)


def _lower(path: _Path) -> _Path:
    return (path[0].lower(), path[1].lower(), path[2].lower())


def _candidates(name: str, search_path: tuple[tuple[str, str], ...]) -> list[_Path]:
    """Every catalog path ``name`` may denote, in DuckDB's binding order."""
    parts = name.lower().split(".")
    match parts:
        case [table]:
            return [(catalog, schema, table) for catalog, schema in search_path]
        case [qualifier, table]:
            # A schema in a searched catalog first, then a catalog's default schema.
            return [(catalog, qualifier, table) for catalog in dict.fromkeys(catalog for catalog, _ in search_path)] + [(qualifier, "main", table)]
        case [catalog, schema, table]:
            return [(catalog, schema, table)]
        case _:
            return []
