"""BigQuery: the standard discovery, one dataset's `INFORMATION_SCHEMA` at a time.

Over `google.cloud.bigquery.dbapi` (`%s` parameters). BigQuery has no
current dataset a query can ask for, so the fetcher is given the project and
the default dataset unqualified names resolve in. Every view is read from
`project.dataset.INFORMATION_SCHEMA`; a name may be qualified by any project.

BigQuery has no `REFERENTIAL_CONSTRAINTS`: a foreign key's referenced table
and columns come from `CONSTRAINT_COLUMN_USAGE`, which does not order them,
so a foreign key over more than one column is skipped. Keys are not
enforced: they hold only under `trust_unenforced_keys` (`dialects.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlassert.discovery.fetcher import DatabaseForeignKey, DatabaseRelationMetadata
from sqlassert.discovery.information_schema import (
    Connection,
    InformationSchemaKnowledgeFetcher,
    foreign_keys_from_pairs,
    placeholders,
    quoted,
)


class BigQueryKnowledgeFetcher(InformationSchemaKnowledgeFetcher):
    type_dialect = "bigquery"
    cross_catalog = True

    def __init__(self, connection: Connection, project: str, dataset: str | None = None):
        super().__init__(connection)
        self.project = project
        self.dataset = dataset

    def session(self, scope: DatabaseRelationMetadata | None) -> tuple[str, tuple[str, ...]]:
        if scope is not None:
            return scope.catalog, (scope.schema,)
        return self.project, (self.dataset,) if self.dataset else ()

    def information_schema(self, catalog: str, schema: str) -> str:
        return f"{quoted(catalog, '`')}.{quoted(schema, '`')}.INFORMATION_SCHEMA"

    def foreign_keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseForeignKey]]:
        where = self.information_schema(catalog, schema)
        own_sql = (
            "SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.COLUMN_NAME "
            f"FROM {where}.TABLE_CONSTRAINTS c JOIN {where}.KEY_COLUMN_USAGE k "
            "ON k.CONSTRAINT_NAME = c.CONSTRAINT_NAME AND k.TABLE_NAME = c.TABLE_NAME "
            f"WHERE c.CONSTRAINT_TYPE = 'FOREIGN KEY' AND c.TABLE_NAME IN ({placeholders(tables)}) "
            "ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION"
        )
        own: dict[str, tuple[str, list[str]]] = {}
        for table, constraint, column in self._rows(own_sql, tuple(tables)):
            own.setdefault(constraint, (table, []))[1].append(column)
        single = sorted(constraint for constraint, (_, members) in own.items() if len(members) == 1)
        if not single:
            return []
        referenced_sql = (
            "SELECT CONSTRAINT_NAME, TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME "
            f"FROM {where}.CONSTRAINT_COLUMN_USAGE WHERE CONSTRAINT_NAME IN ({placeholders(single)})"
        )
        referenced: dict[str, list[tuple[Any, ...]]] = {}
        for constraint, *target in self._rows(referenced_sql, tuple(single)):
            referenced.setdefault(constraint, []).append(tuple(target))
        return foreign_keys_from_pairs([
            (own[constraint][0], constraint, own[constraint][1][0], *referenced[constraint][0])
            for constraint in single if len(referenced.get(constraint, ())) == 1
        ])
