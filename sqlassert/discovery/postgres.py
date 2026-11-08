"""The PostgreSQL catalog boundary: name resolution, metadata, and view definitions.

Reads `pg_catalog` only, over any DB-API connection (psycopg, psycopg2); it
never scans a table. Only constraints that hold on every row are Knowledge:
validated, enforced (PostgreSQL 18 has NOT ENFORCED), not DEFERRABLE, and,
for a foreign key, onto a table without row-level security, which may hide a
referenced row from the reader. A table other tables inherit from returns
their rows too, which its own keys and NOT NULLs do not cover, so it is
columns only, as are foreign tables, whose constraints are never checked.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlassert.discovery.dbapi import DbApiKnowledgeFetcher
from sqlassert.discovery.fetcher import (
    DatabaseColumn,
    DatabaseForeignKey,
    DatabaseMetadataError,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
    DatabaseViewDefinition,
)

type _Path = tuple[str, str]

_SESSION = "SELECT current_database(), current_schemas(true), current_setting('server_version_num')::int"

# 'r' table, 'p' partitioned table, 'v' view, 'm' materialized view, 'f' foreign table.
_RELATIONS = """
SELECT n.nspname, c.relname, c.relkind, c.relkind = 'r' AND c.relhassubclass
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND lower(c.relname) = ANY(%s::text[])
"""

_TABLES = "(n.nspname, c.relname) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"

# A NOT NULL added NOT VALID (PostgreSQL 18) sets attnotnull before it is
# checked. A deterministic collation compares equal only identical text.
_COLUMNS = f"""
SELECT n.nspname, c.relname, a.attname,
    NOT a.attnotnull OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint k
        WHERE k.conrelid = c.oid AND k.contype = 'n' AND NOT k.convalidated AND a.attnum = ANY(k.conkey)
    ),
    pg_catalog.format_type(a.atttypid, a.atttypmod),
    CASE WHEN a.attcollation = 0 OR co.collisdeterministic THEN NULL ELSE co.collname END
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation
WHERE a.attnum > 0 AND NOT a.attisdropped AND {_TABLES}
ORDER BY n.nspname, c.relname, a.attnum
"""


def _names(keys: str, relation: str) -> str:
    return f"""ARRAY(
        SELECT a.attname FROM unnest({keys}) WITH ORDINALITY AS key(attnum, position)
        JOIN pg_catalog.pg_attribute a ON a.attrelid = {relation} AND a.attnum = key.attnum
        ORDER BY key.position
    )"""


_CONSTRAINTS = f"""
SELECT n.nspname, c.relname, k.contype, {_names("k.conkey", "k.conrelid")}, rn.nspname, r.relname, {_names("k.confkey", "k.confrelid")}
FROM pg_catalog.pg_constraint k
JOIN pg_catalog.pg_class c ON c.oid = k.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
LEFT JOIN pg_catalog.pg_class r ON r.oid = k.confrelid
LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid = r.relnamespace
WHERE k.contype IN ('p', 'u', 'f') AND k.convalidated AND NOT k.condeferrable {{enforced}}
    AND NOT (k.contype = 'f' AND r.relrowsecurity) AND {_TABLES}
ORDER BY n.nspname, c.relname, k.conname
"""

# A unique index no constraint owns: over plain key columns (not INCLUDE
# ones), on every row (no WHERE), checked immediately.
_UNIQUE_INDEXES = f"""
SELECT n.nspname, c.relname, {_names("(i.indkey::int2[])[0:i.indnkeyatts - 1]", "i.indrelid")}
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class c ON c.oid = i.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE i.indisunique AND i.indisvalid AND i.indimmediate AND i.indpred IS NULL AND i.indexprs IS NULL
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conindid = i.indexrelid) AND {_TABLES}
ORDER BY n.nspname, c.relname, i.indexrelid
"""

_VIEW = """
SELECT pg_catalog.pg_get_viewdef(c.oid, true)
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'v'
"""


class PostgresKnowledgeFetcher(DbApiKnowledgeFetcher):
    type_dialect = "postgres"

    def fetch_relation_metadata(
        self, names: tuple[str, ...], scope: DatabaseRelationMetadata | None = None
    ) -> Mapping[str, DatabaseRelationMetadata]:
        # A stored view definition names relations as the session's search path
        # sees them (pg_get_viewdef qualifies the rest), so `scope` changes nothing.
        (database, search_path, version), = self._rows(_SESSION)
        unqualified = sorted({name.rsplit(".", 1)[-1].lower() for name in names})
        found: dict[_Path, tuple[_Path, str, bool]] = {}
        for schema, table, kind, inherited in self._rows(_RELATIONS, (unqualified,)):
            found[(schema.lower(), table.lower())] = ((schema, table), kind, inherited)

        resolved: dict[str, tuple[_Path, str, bool]] = {}
        for name in names:
            match = next((found[path] for path in _candidates(name, database, search_path) if path in found), None)
            if match is not None:
                resolved[name] = match

        tables = sorted({path for path, kind, _ in resolved.values() if kind != "v"})
        schemas, relations = [path[0] for path in tables], [path[1] for path in tables]
        columns: dict[_Path, list[DatabaseColumn]] = {}
        keys: dict[_Path, list[tuple[str, ...]]] = {}
        primary_keys: dict[_Path, tuple[str, ...]] = {}
        foreign_keys: dict[_Path, list[DatabaseForeignKey]] = {}
        if tables:
            for schema, table, column, nullable, data_type, collation in self._rows(_COLUMNS, (schemas, relations)):
                columns.setdefault((schema, table), []).append(DatabaseColumn(column, nullable, data_type, collation))
            enforced = "AND k.conenforced" if version >= 180000 else ""
            for schema, table, kind, members, referenced_schema, referenced, referenced_members in self._rows(
                _CONSTRAINTS.format(enforced=enforced), (schemas, relations)
            ):
                path = (schema, table)
                match kind:
                    case "p":
                        primary_keys[path] = tuple(members)
                    case "u":
                        keys.setdefault(path, []).append(tuple(members))
                    case "f":
                        foreign_keys.setdefault(path, []).append(
                            DatabaseForeignKey(tuple(members), f"{database}.{referenced_schema}.{referenced}", tuple(referenced_members))
                        )
                    case _:
                        raise DatabaseMetadataError(f"unexpected constraint type {kind}")
            for schema, table, members in self._rows(_UNIQUE_INDEXES, (schemas, relations)):
                keys.setdefault((schema, table), []).append(tuple(members))

        metadata: dict[_Path, DatabaseRelationMetadata] = {}
        for path, kind, inherited in resolved.values():
            if kind == "v":
                metadata[path] = DatabaseRelationMetadata(database, path[0], path[1], DatabaseRelationKind.VIEW)
                continue
            own = tuple(columns.get(path, ()))
            if inherited or kind == "f":
                # Columns only: no fact of the table covers every row it returns.
                own = tuple(DatabaseColumn(column.name, True, column.data_type, column.collation) for column in own)
                metadata[path] = DatabaseRelationMetadata(database, path[0], path[1], DatabaseRelationKind.TABLE, own)
                continue
            metadata[path] = DatabaseRelationMetadata(
                database, path[0], path[1], DatabaseRelationKind.TABLE, own,
                primary_key=primary_keys.get(path, ()), unique_sets=tuple(keys.get(path, ())),
                foreign_keys=tuple(foreign_keys.get(path, ())),
            )
        return {name: metadata[path] for name, (path, _, _) in resolved.items()}

    def fetch_view_definition(self, relation: DatabaseRelationMetadata) -> DatabaseViewDefinition:
        rows = self._rows(_VIEW, (relation.schema, relation.name))
        if not rows or rows[0][0] is None:
            raise DatabaseMetadataError(f"view {relation.identity} is no longer in the catalog")
        body = rows[0][0].strip().rstrip(";")
        return DatabaseViewDefinition(sql=f"CREATE VIEW {_quoted(relation.name)} AS {body}")


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _candidates(name: str, database: str, search_path: list[str]) -> list[_Path]:
    """Every (schema, table) ``name`` may denote, in PostgreSQL's lookup order."""
    parts = name.lower().split(".")
    match parts:
        case [table]:
            return [(schema.lower(), table) for schema in search_path]
        case [schema, table]:
            return [(schema, table)]
        case [catalog, schema, table] if catalog == database.lower():
            return [(schema, table)]
        case _:
            return []
