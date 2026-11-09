"""PostgreSQL: the standard discovery, with `pg_catalog` where `information_schema` falls short.

PostgreSQL's `information_schema` lists a constraint only to a role that owns
its table or holds a privilege on it other than SELECT, and a view's text only
to its owner, so a read-only role would see no keys. Every hook here reads
`pg_catalog` instead, over any DB-API connection (psycopg, psycopg2).

Only constraints that hold on every row are reported: validated, enforced
(PostgreSQL 18 has NOT ENFORCED), not DEFERRABLE, and, for a foreign key,
onto a table without row-level security, which may hide a referenced row
from the reader. Unique indexes no constraint owns count, over their key
columns (not INCLUDE ones), when valid, immediate, with no `WHERE` and no
expression. A table other tables inherit from returns their rows too, which
its own keys and NOT NULLs do not cover, so it is columns only, as are
foreign tables, whose constraints are never checked. A NOT NULL added NOT
VALID does not count. A column's collation is reported only when it is
nondeterministic: the dialect's default compares by identity, and such a
collation does not.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlassert.discovery.fetcher import (
    DatabaseColumn,
    DatabaseForeignKey,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
)
from sqlassert.discovery.information_schema import FoundRelation, InformationSchemaKnowledgeFetcher, Key

_SESSION = "SELECT current_database(), current_schemas(true), current_setting('server_version_num')::int"

_RELATION = "pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace"
_TABLES = "n.nspname = %s AND c.relname = ANY(%s::text[])"

# 'r' table, 'p' partitioned table, 'v' view, 'm' materialized view, 'f' foreign table.
_RELATIONS = f"""
SELECT current_database(), n.nspname, c.relname, c.relkind, c.relkind = 'r' AND c.relhassubclass
FROM {_RELATION}
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND lower(n.nspname) = %s AND lower(c.relname) = ANY(%s::text[])
"""

_COLUMNS = f"""
SELECT c.relname, a.attname,
    NOT a.attnotnull OR EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint k
        WHERE k.conrelid = c.oid AND k.contype = 'n' AND NOT k.convalidated AND a.attnum = ANY(k.conkey)
    ),
    pg_catalog.format_type(a.atttypid, a.atttypmod),
    CASE WHEN a.attcollation = 0 OR co.collisdeterministic THEN NULL ELSE co.collname END
FROM pg_catalog.pg_attribute a
JOIN {_RELATION} ON c.oid = a.attrelid
LEFT JOIN pg_catalog.pg_collation co ON co.oid = a.attcollation
WHERE a.attnum > 0 AND NOT a.attisdropped AND {_TABLES}
ORDER BY c.relname, a.attnum
"""


def _names(keys: str, relation: str) -> str:
    return f"""ARRAY(
        SELECT a.attname FROM unnest({keys}) WITH ORDINALITY AS key(attnum, position)
        JOIN pg_catalog.pg_attribute a ON a.attrelid = {relation} AND a.attnum = key.attnum
        ORDER BY key.position
    )"""


_CONSTRAINTS = f"""
SELECT c.relname, k.contype, {_names("k.conkey", "k.conrelid")}, current_database(), rn.nspname, r.relname, {_names("k.confkey", "k.confrelid")}
FROM pg_catalog.pg_constraint k
JOIN {_RELATION} ON c.oid = k.conrelid
LEFT JOIN pg_catalog.pg_class r ON r.oid = k.confrelid
LEFT JOIN pg_catalog.pg_namespace rn ON rn.oid = r.relnamespace
WHERE k.contype IN ({{kinds}}) AND k.convalidated AND NOT k.condeferrable {{enforced}}
    AND NOT (k.contype = 'f' AND r.relrowsecurity) AND {_TABLES}
ORDER BY c.relname, k.conname
"""

_UNIQUE_INDEXES = f"""
SELECT c.relname, {_names("(i.indkey::int2[])[0:i.indnkeyatts - 1]", "i.indrelid")}
FROM pg_catalog.pg_index i
JOIN {_RELATION} ON c.oid = i.indrelid
WHERE i.indisunique AND i.indisvalid AND i.indimmediate AND i.indpred IS NULL AND i.indexprs IS NULL
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conindid = i.indexrelid) AND {_TABLES}
ORDER BY c.relname, i.indexrelid
"""

VIEW = f"""
SELECT pg_catalog.pg_get_viewdef(c.oid, true)
FROM {_RELATION}
WHERE n.nspname = %s AND c.relname = %s AND c.relkind = 'v'
"""


class PostgresKnowledgeFetcher(InformationSchemaKnowledgeFetcher):
    type_dialect = "postgres"
    case_sensitive = True
    _enforced = False

    def session(self, scope: DatabaseRelationMetadata | None) -> tuple[str, tuple[str, ...]]:
        # A stored view definition names relations as the session's search path
        # sees them (pg_get_viewdef qualifies the rest), so `scope` changes nothing.
        (database, search_path, version), = self._rows(_SESSION)
        self._enforced = version >= 180000
        return database, tuple(search_path)

    def relations(self, catalog: str, schema: str, names: Sequence[str]) -> list[FoundRelation]:
        return [
            FoundRelation(
                database, found_schema, name, DatabaseRelationKind.VIEW if kind == "v" else DatabaseRelationKind.TABLE,
                facts_hold=not inherited and kind != "f",
            )
            for database, found_schema, name, kind, inherited in self._rows(_RELATIONS, (schema.lower(), list(names)))
        ]

    def columns(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseColumn]]:
        return [
            (table, DatabaseColumn(column, nullable, data_type, collation))
            for table, column, nullable, data_type, collation in self._rows(_COLUMNS, (schema, list(tables)))
        ]

    def keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[Key]:
        keys = [
            Key(table, tuple(members), kind == "p")
            for table, kind, members, *_ in self._rows(self._constraints("'p', 'u'"), (schema, list(tables)))
        ]
        keys.extend(Key(table, tuple(members)) for table, members in self._rows(_UNIQUE_INDEXES, (schema, list(tables))))
        return keys

    def foreign_keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseForeignKey]]:
        return [
            (table, DatabaseForeignKey(tuple(members), f"{database}.{referenced_schema}.{referenced}", tuple(referenced_members)))
            for table, _, members, database, referenced_schema, referenced, referenced_members
            in self._rows(self._constraints("'f'"), (schema, list(tables)))
        ]

    def view_definition(self, relation: DatabaseRelationMetadata) -> str | None:
        rows = self._rows(VIEW, (relation.schema, relation.name))
        return rows[0][0] if rows else None

    def _constraints(self, kinds: str) -> str:
        return _CONSTRAINTS.format(kinds=kinds, enforced="AND k.conenforced" if self._enforced else "")
