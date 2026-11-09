"""The catalog boundary of any database with a standard `INFORMATION_SCHEMA`, over DB-API.

`InformationSchemaKnowledgeFetcher` resolves names and reads the standard
views (`TABLES`, `COLUMNS`, `TABLE_CONSTRAINTS`, `KEY_COLUMN_USAGE`,
`REFERENTIAL_CONSTRAINTS`, `VIEWS`) through any DB-API 2.0 connection; it
never imports a driver and never scans a table. A subclass per dialect
(`discovery/duckdb.py`, `postgres.py`, `mysql.py`, `redshift.py`, `snowflake.py`,
`bigquery.py`, `databricks.py`, `trino.py`) overrides only the hooks whose standard view
is missing or incomplete there, and the parameter style.

What it reports, and what it leaves out:

- Every column has the dialect's default collation: per-column and
  per-table collations are not read (Postgres reports a nondeterministic one,
  which would otherwise be taken as comparing by identity).
- Primary keys, unique constraints, unique indexes and foreign keys are
  reported as declared; the catalog carries no per-constraint trust. Whether
  they are Knowledge is the dialect's `DialectSemantics` decision, applied
  by `DatabaseKnowledgeFetcher.bind`: where the dialect does not enforce them
  they hold only under `trust_unenforced_keys`.
- A unique index with a `WHERE` or an expression key part is never a key.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from sqlglot import exp

from sqlassert.discovery.fetcher import (
    DatabaseColumn,
    DatabaseForeignKey,
    DatabaseKnowledgeFetcher,
    DatabaseMetadataError,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
    DatabaseViewDefinition,
)


class Cursor(Protocol):
    def execute(self, operation: str, parameters: Any = ..., /) -> Any: ...
    def fetchall(self) -> Any: ...
    def close(self) -> Any: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...


@dataclass(frozen=True)
class FoundRelation:
    """A catalog Relation a name resolved to. `facts_hold` is False when its
    keys and NOT NULLs do not cover every row it returns (a Postgres
    inheritance parent, a foreign table): it is then columns only."""

    catalog: str
    schema: str
    name: str
    kind: DatabaseRelationKind
    facts_hold: bool = True


@dataclass(frozen=True)
class Key:
    table: str
    columns: tuple[str, ...]
    primary: bool = False


type _Key = tuple[str, str, str]


class InformationSchemaKnowledgeFetcher(DatabaseKnowledgeFetcher):
    """Standard `INFORMATION_SCHEMA` discovery; one subclass per dialect."""

    # The DB-API parameter style of the dialect's drivers: `%s` or `?`.
    paramstyle: Literal["format", "qmark"] = "format"
    # One row: the current catalog and the schema unqualified names resolve in.
    session_sql: str = "SELECT CURRENT_CATALOG, CURRENT_SCHEMA"
    # Whether a name may be qualified by a catalog other than the current one.
    cross_catalog: bool = False
    # Whether names that differ only in case are distinct relations, an
    # unquoted name folded to lower case (PostgreSQL). A name's quoting is not
    # known here, so it denotes a relation spelled as it is or as it folds,
    # and only when no other relation on its lookup path is spelled either way.
    case_sensitive: bool = False

    def __init__(self, connection: Connection):
        self.connection = connection

    # Hooks a dialect overrides where its standard views fall short.

    def session(self, scope: DatabaseRelationMetadata | None) -> tuple[str, tuple[str, ...]]:
        """The current catalog and the schemas an unqualified name is looked
        up in, in order. Inside a view, its own catalog and schema."""
        if scope is not None:
            return scope.catalog, (scope.schema,)
        (catalog, schema), = self._rows(self.session_sql)
        return catalog, (schema,) if schema else ()

    def information_schema(self, catalog: str, schema: str) -> str:
        """Where the standard views of ``catalog``.``schema`` are."""
        return "INFORMATION_SCHEMA"

    def relations(self, catalog: str, schema: str, names: Sequence[str]) -> list[FoundRelation]:
        """The Relations of ``schema`` whose lower-cased name is in ``names``
        (lower-cased), matched case-insensitively."""
        sql = (
            f"SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE FROM {self.information_schema(catalog, schema)}.TABLES "
            f"WHERE LOWER(TABLE_SCHEMA) = %s AND LOWER(TABLE_NAME) IN ({placeholders(names)})"
        )
        return [
            FoundRelation(found_catalog, found_schema, name, DatabaseRelationKind.VIEW if kind == "VIEW" else DatabaseRelationKind.TABLE)
            for found_catalog, found_schema, name, kind in self._rows(sql, (schema.lower(), *names))
        ]

    def columns(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseColumn]]:
        sql = (
            f"SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE, DATA_TYPE FROM {self.information_schema(catalog, schema)}.COLUMNS "
            f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders(tables)}) ORDER BY TABLE_NAME, ORDINAL_POSITION"
        )
        return [
            (table, DatabaseColumn(column, nullable == "YES", data_type))
            for table, column, nullable, data_type in self._rows(sql, (schema, *tables))
        ]

    def keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[Key]:
        """Primary keys, unique constraints and unique indexes over plain columns."""
        where = self.information_schema(catalog, schema)
        sql = f"""
SELECT c.TABLE_NAME, c.CONSTRAINT_NAME, c.CONSTRAINT_TYPE, k.COLUMN_NAME
FROM {where}.TABLE_CONSTRAINTS c JOIN {where}.KEY_COLUMN_USAGE k
    ON k.CONSTRAINT_SCHEMA = c.CONSTRAINT_SCHEMA AND k.CONSTRAINT_NAME = c.CONSTRAINT_NAME
    AND k.TABLE_SCHEMA = c.TABLE_SCHEMA AND k.TABLE_NAME = c.TABLE_NAME
WHERE c.CONSTRAINT_TYPE IN ('PRIMARY KEY', 'UNIQUE') AND c.TABLE_SCHEMA = %s AND c.TABLE_NAME IN ({placeholders(tables)})
ORDER BY c.TABLE_NAME, c.CONSTRAINT_NAME, k.ORDINAL_POSITION
"""
        grouped: dict[tuple[str, str], tuple[bool, list[str | None]]] = {}
        for table, constraint, kind, column in self._rows(sql, (schema, *tables)):
            grouped.setdefault((table, constraint), (kind == "PRIMARY KEY", []))[1].append(column)
        return [key for (table, _), (primary, members) in grouped.items() if (key := key_over_columns(table, members, primary)) is not None]

    def foreign_keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseForeignKey]]:
        """Each foreign key's columns paired by position with the columns of
        the unique constraint it references. One whose referenced columns do
        not resolve to one table is left out."""
        where = self.information_schema(catalog, schema)
        sql = f"""
SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME, u.TABLE_CATALOG, u.TABLE_SCHEMA, u.TABLE_NAME, u.COLUMN_NAME
FROM {where}.REFERENTIAL_CONSTRAINTS r
JOIN {where}.KEY_COLUMN_USAGE k
    ON k.CONSTRAINT_CATALOG = r.CONSTRAINT_CATALOG AND k.CONSTRAINT_SCHEMA = r.CONSTRAINT_SCHEMA AND k.CONSTRAINT_NAME = r.CONSTRAINT_NAME
LEFT JOIN {where}.KEY_COLUMN_USAGE u
    ON u.CONSTRAINT_CATALOG = r.UNIQUE_CONSTRAINT_CATALOG AND u.CONSTRAINT_SCHEMA = r.UNIQUE_CONSTRAINT_SCHEMA
    AND u.CONSTRAINT_NAME = r.UNIQUE_CONSTRAINT_NAME AND u.ORDINAL_POSITION = k.POSITION_IN_UNIQUE_CONSTRAINT
WHERE k.TABLE_SCHEMA = %s AND k.TABLE_NAME IN ({placeholders(tables)})
ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION
"""
        return foreign_keys_from_pairs(self._rows(sql, (schema, *tables)))

    def view_definition(self, relation: DatabaseRelationMetadata) -> str | None:
        """The view's stored text: its body, or a whole `CREATE VIEW`."""
        sql = f"SELECT VIEW_DEFINITION FROM {self.information_schema(relation.catalog, relation.schema)}.VIEWS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s"
        rows = self._rows(sql, (relation.schema, relation.name))
        return rows[0][0] if rows else None

    # The assembly every dialect shares.

    def fetch_relation_metadata(
        self, names: tuple[str, ...], scope: DatabaseRelationMetadata | None = None
    ) -> Mapping[str, DatabaseRelationMetadata]:
        catalog, path = self.session(scope)
        candidates = {name: self.candidates(name, catalog, path) for name in names}
        groups: dict[tuple[str, str], tuple[str, str, set[str]]] = {}
        for options in candidates.values():
            for candidate_catalog, schema, table in options:
                groups.setdefault((candidate_catalog.lower(), schema.lower()), (candidate_catalog, schema, set()))[2].add(table.lower())
        found: dict[_Key, list[FoundRelation]] = {}
        for group_catalog, schema, tables in groups.values():
            for relation in self.relations(group_catalog, schema, sorted(tables)):
                listed = found.setdefault((relation.catalog.lower(), relation.schema.lower(), relation.name.lower()), [])
                if relation not in listed:
                    listed.append(relation)
        resolved: dict[str, FoundRelation] = {}
        for name, options in candidates.items():
            match = self._match(options, found)
            if match is not None:
                resolved[name] = match

        tables: dict[tuple[str, str], set[str]] = {}
        for relation in resolved.values():
            if relation.kind is DatabaseRelationKind.TABLE:
                tables.setdefault((relation.catalog, relation.schema), set()).add(relation.name)
        columns: dict[_Key, list[DatabaseColumn]] = {}
        keys: dict[_Key, list[Key]] = {}
        foreign_keys: dict[_Key, list[DatabaseForeignKey]] = {}
        for (table_catalog, schema), group in tables.items():
            listed = sorted(group)
            for table, column in self.columns(table_catalog, schema, listed):
                columns.setdefault((table_catalog, schema, table), []).append(column)
            for key in self.keys(table_catalog, schema, listed):
                keys.setdefault((table_catalog, schema, key.table), []).append(key)
            for table, foreign_key in self.foreign_keys(table_catalog, schema, listed):
                foreign_keys.setdefault((table_catalog, schema, table), []).append(foreign_key)

        metadata: dict[str, DatabaseRelationMetadata] = {}
        for name, relation in resolved.items():
            path_key = (relation.catalog, relation.schema, relation.name)
            if relation.kind is DatabaseRelationKind.VIEW:
                metadata[name] = DatabaseRelationMetadata(*path_key, DatabaseRelationKind.VIEW)
                continue
            own = tuple(columns.get(path_key, ()))
            if not relation.facts_hold:
                own = tuple(replace(column, nullable=True) for column in own)
                metadata[name] = DatabaseRelationMetadata(*path_key, DatabaseRelationKind.TABLE, own)
                continue
            table_keys = keys.get(path_key, [])
            metadata[name] = DatabaseRelationMetadata(
                *path_key, DatabaseRelationKind.TABLE, own,
                primary_key=next((key.columns for key in table_keys if key.primary), ()),
                unique_sets=tuple(key.columns for key in table_keys if not key.primary),
                foreign_keys=tuple(foreign_keys.get(path_key, ())),
            )
        return metadata

    def fetch_view_definition(self, relation: DatabaseRelationMetadata) -> DatabaseViewDefinition:
        text = self.view_definition(relation)
        if not text or not text.strip():
            # Hidden from a reader who does not own the view, in most dialects.
            raise DatabaseMetadataError(f"definition of view {relation.identity} is not readable")
        text = text.strip().rstrip(";")
        if text.upper().startswith("CREATE"):
            return DatabaseViewDefinition(sql=text)
        name = exp.to_identifier(relation.name, quoted=True).sql(dialect=self.type_dialect)
        return DatabaseViewDefinition(sql=f"CREATE VIEW {name} AS {text}")

    def _match(self, options: list[tuple[str, str, str]], found: Mapping[_Key, list[FoundRelation]]) -> FoundRelation | None:
        """The relation the first of ``options`` with one names, or None when
        a name cannot tell the relations it may denote apart."""
        if not self.case_sensitive:
            # Names that differ only in case cannot be told apart.
            listed = next((found[key] for option in options if (key := _lower(option)) in found), [])
            return listed[0] if len(listed) == 1 else None
        spelled = [
            [
                relation for relation in found.get(_lower(option), ())
                if relation.schema in {schema, schema.lower()} and relation.name in {table, table.lower()}
            ]
            for option in options for _, schema, table in [option]
        ]
        if len({relation.name for listed in spelled for relation in listed}) > 1:
            return None  # `"T"` and `t` are both on the path: whether the name was quoted decides.
        listed = next((listed for listed in spelled if listed), [])
        return listed[0] if len(listed) == 1 else None

    def candidates(self, name: str, catalog: str, path: tuple[str, ...]) -> list[tuple[str, str, str]]:
        """Every (catalog, schema, table) ``name`` may denote, in lookup order."""
        match name.split("."):
            case [table]:
                return [(catalog, schema, table) for schema in path]
            case [schema, table]:
                return [(catalog, schema, table)]
            case [named, schema, table] if named.lower() == catalog.lower():
                return [(catalog, schema, table)]
            case [named, schema, table] if self.cross_catalog:
                return [(named, schema, table)]
            case _:
                return []

    def _rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """The rows of one catalog query. Any driver error is a
        DatabaseMetadataError: DB-API drivers share no exception base class."""
        if self.paramstyle == "qmark":
            sql = sql.replace("%s", "?")
        try:
            cursor = self.connection.cursor()
            try:
                cursor.execute(sql, parameters)
                return [tuple(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
        except Exception as error:
            raise DatabaseMetadataError(str(error)) from error


def foreign_keys_from_pairs(rows: Sequence[tuple[Any, ...]]) -> list[tuple[str, DatabaseForeignKey]]:
    """Foreign keys from rows of (table, constraint, column, referenced
    catalog, schema, table, column), ordered by constraint and position. One
    whose referenced columns are missing or span tables is left out: a
    foreign key over part of its columns does not hold."""
    grouped: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for table, constraint, *pair in rows:
        grouped.setdefault((table, constraint), []).append(tuple(pair))
    found: list[tuple[str, DatabaseForeignKey]] = []
    for (table, _), pairs in grouped.items():
        targets = {(catalog, schema, referenced) for _, catalog, schema, referenced, _ in pairs}
        if len(targets) != 1 or any(part is None for pair in pairs for part in pair):
            continue
        (catalog, schema, referenced), = targets
        found.append((table, DatabaseForeignKey(
            tuple(pair[0] for pair in pairs), f"{catalog}.{schema}.{referenced}", tuple(pair[4] for pair in pairs),
        )))
    return found


def key_over_columns(table: str, members: Sequence[str | None], primary: bool = False) -> Key | None:
    """A key over plain columns; None when a part is an expression (no column name)."""
    if not members or any(member is None for member in members):
        return None
    return Key(table, tuple(member for member in members if member is not None), primary)


def quoted(identifier: str, quote: str = '"') -> str:
    return quote + identifier.replace(quote, quote * 2) + quote


def placeholders(values: Sequence[Any]) -> str:
    """`%s, %s, ...`: one placeholder per value, for an IN list."""
    return ", ".join(["%s"] * len(values))


def _lower(path: tuple[str, str, str]) -> _Key:
    return (path[0].lower(), path[1].lower(), path[2].lower())
