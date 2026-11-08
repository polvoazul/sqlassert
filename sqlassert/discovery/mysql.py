"""The MySQL catalog boundary: name resolution, metadata, and view definitions.

Reads `information_schema` only, over any DB-API connection (PyMySQL,
mysqlclient, mysql-connector); it never scans a table. Every text column
carries its collation, since MySQL's default ones ignore case. Keys are the
unique indexes over plain columns (a prefix index is unique on the prefix, so
on the column too); foreign keys are those the catalog lists, which only
engines that enforce them (InnoDB, NDB) keep. `foreign_key_checks = 0` lets a
session load rows that violate one, which no catalog records.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlassert.discovery.dbapi import DbApiKnowledgeFetcher, placeholders
from sqlassert.discovery.fetcher import (
    DatabaseColumn,
    DatabaseForeignKey,
    DatabaseMetadataError,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
    DatabaseViewDefinition,
)

type _Path = tuple[str, str]

# MySQL has one catalog, which information_schema names `def`.
CATALOG = "def"

_RELATIONS = "SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE LOWER(TABLE_NAME) IN ({names})"

_COLUMNS = """
SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, IS_NULLABLE, COLUMN_TYPE, COLLATION_NAME FROM information_schema.COLUMNS
WHERE (TABLE_SCHEMA, TABLE_NAME) IN ({tables})
ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
"""

# A functional key part has no COLUMN_NAME.
_UNIQUE_INDEXES = """
SELECT TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS
WHERE NON_UNIQUE = 0 AND (TABLE_SCHEMA, TABLE_NAME) IN ({tables})
ORDER BY TABLE_SCHEMA, TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX
"""

_FOREIGN_KEYS = """
SELECT TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM information_schema.KEY_COLUMN_USAGE
WHERE REFERENCED_TABLE_NAME IS NOT NULL AND (TABLE_SCHEMA, TABLE_NAME) IN ({tables})
ORDER BY TABLE_SCHEMA, TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION
"""

_VIEW = "SELECT VIEW_DEFINITION FROM information_schema.VIEWS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"


class MySQLKnowledgeFetcher(DbApiKnowledgeFetcher):
    type_dialect = "mysql"

    def fetch_relation_metadata(
        self, names: tuple[str, ...], scope: DatabaseRelationMetadata | None = None
    ) -> Mapping[str, DatabaseRelationMetadata]:
        # A stored view definition qualifies every table it reads; an
        # unqualified name in the Program is in the current database.
        (database,), = self._rows("SELECT DATABASE()")
        if scope is not None:
            database = scope.schema
        unqualified = sorted({name.rsplit(".", 1)[-1].lower() for name in names})
        found: dict[_Path, tuple[_Path, DatabaseRelationKind]] = {}
        for schema, table, kind in self._rows(_RELATIONS.format(names=placeholders(len(unqualified))), tuple(unqualified)):
            relation_kind = DatabaseRelationKind.VIEW if kind == "VIEW" else DatabaseRelationKind.TABLE
            found[(schema.lower(), table.lower())] = ((schema, table), relation_kind)

        resolved = {
            name: found[path] for name in names
            if (path := _candidate(name, database)) is not None and path in found
        }
        tables = sorted({path for path, kind in resolved.values() if kind is DatabaseRelationKind.TABLE})
        columns: dict[_Path, list[DatabaseColumn]] = {}
        primary_keys: dict[_Path, tuple[str, ...]] = {}
        unique_sets: dict[_Path, list[tuple[str, ...]]] = {}
        foreign_keys: dict[_Path, list[DatabaseForeignKey]] = {}
        if tables:
            where = ", ".join(["(%s, %s)"] * len(tables))
            parameters = tuple(part for path in tables for part in path)
            for schema, table, column, nullable, data_type, collation in self._rows(_COLUMNS.format(tables=where), parameters):
                columns.setdefault((schema, table), []).append(DatabaseColumn(column, nullable == "YES", data_type, collation))
            indexes: dict[tuple[str, str, str], list[str | None]] = {}
            for schema, table, index, column in self._rows(_UNIQUE_INDEXES.format(tables=where), parameters):
                indexes.setdefault((schema, table, index), []).append(column)
            for (schema, table, index), members in indexes.items():
                if any(member is None for member in members):
                    continue  # Unique on an expression, not on columns.
                key = tuple(member for member in members if member is not None)
                if index == "PRIMARY":
                    primary_keys[(schema, table)] = key
                else:
                    unique_sets.setdefault((schema, table), []).append(key)
            references: dict[tuple[str, str, str], list[tuple[str, str, str, str]]] = {}
            for schema, table, constraint, column, referenced_schema, referenced, referenced_column in self._rows(
                _FOREIGN_KEYS.format(tables=where), parameters
            ):
                references.setdefault((schema, table, constraint), []).append((column, referenced_schema, referenced, referenced_column))
            for (schema, table, _), pairs in references.items():
                targets = {(referenced_schema, referenced) for _, referenced_schema, referenced, _ in pairs}
                if len(targets) != 1:
                    raise DatabaseMetadataError(f"foreign key of {schema}.{table} references more than one table")
                (referenced_schema, referenced), = targets
                foreign_keys.setdefault((schema, table), []).append(DatabaseForeignKey(
                    tuple(column for column, _, _, _ in pairs), f"{CATALOG}.{referenced_schema}.{referenced}",
                    tuple(referenced_column for _, _, _, referenced_column in pairs),
                ))

        metadata = {
            path: DatabaseRelationMetadata(
                CATALOG, path[0], path[1], kind, tuple(columns.get(path, ())), primary_keys.get(path, ()),
                tuple(unique_sets.get(path, ())), tuple(foreign_keys.get(path, ())),
            )
            for path, kind in resolved.values()
        }
        return {name: metadata[path] for name, (path, _) in resolved.items()}

    def fetch_view_definition(self, relation: DatabaseRelationMetadata) -> DatabaseViewDefinition:
        rows = self._rows(_VIEW, (relation.schema, relation.name))
        if not rows or not rows[0][0]:
            # Empty without the SHOW VIEW privilege.
            raise DatabaseMetadataError(f"definition of view {relation.identity} is not readable")
        return DatabaseViewDefinition(sql=f"CREATE VIEW `{relation.name.replace('`', '``')}` AS {rows[0][0]}")


def _candidate(name: str, database: str | None) -> _Path | None:
    """The (schema, table) ``name`` denotes: unqualified, in the current database."""
    match name.lower().split("."):
        case [table] if database is not None:
            return (database.lower(), table)
        case [schema, table]:
            return (schema, table)
        case [catalog, schema, table] if catalog == CATALOG:
            return (schema, table)
        case _:
            return None
