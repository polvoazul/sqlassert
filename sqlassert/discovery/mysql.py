"""MySQL: the standard discovery, with `information_schema` columns the standard lacks.

Over any DB-API connection (PyMySQL, mysqlclient, mysql-connector). Overrides:

- columns: `COLUMN_TYPE`, which keeps an ENUM's members, not `DATA_TYPE`;
- keys: `STATISTICS`, the unique indexes. `KEY_COLUMN_USAGE` leaves out a
  functional key part, which would make an index over `(a, (lower(b)))` look
  like a key over `a`; an index with one is skipped. A prefix index is unique
  on the prefix, so on the column too;
- foreign keys: `KEY_COLUMN_USAGE`'s `REFERENCED_*` columns, since every
  primary key is named `PRIMARY`, which the standard join cannot tell apart.
  Only engines that enforce foreign keys (InnoDB, NDB) keep them in the
  catalog. `foreign_key_checks = 0` lets a session load rows that violate
  one, which no catalog records.

MySQL has one catalog, which `information_schema` names `def`. A stored view
definition qualifies every table it reads; an unqualified name in the Program
is in the current database.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlassert.discovery.fetcher import DatabaseColumn, DatabaseForeignKey
from sqlassert.discovery.information_schema import (
    InformationSchemaKnowledgeFetcher,
    Key,
    foreign_keys_from_pairs,
    key_over_columns,
    placeholders,
)


class MySQLKnowledgeFetcher(InformationSchemaKnowledgeFetcher):
    type_dialect = "mysql"
    session_sql = "SELECT 'def', DATABASE()"

    def columns(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseColumn]]:
        sql = (
            "SELECT TABLE_NAME, COLUMN_NAME, IS_NULLABLE, COLUMN_TYPE FROM information_schema.COLUMNS "
            f"WHERE TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders(tables)}) ORDER BY TABLE_NAME, ORDINAL_POSITION"
        )
        return [
            (table, DatabaseColumn(column, nullable == "YES", data_type))
            for table, column, nullable, data_type in self._rows(sql, (schema, *tables))
        ]

    def keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[Key]:
        sql = (
            "SELECT TABLE_NAME, INDEX_NAME, COLUMN_NAME FROM information_schema.STATISTICS "
            f"WHERE NON_UNIQUE = 0 AND TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders(tables)}) "
            "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX"
        )
        indexes: dict[tuple[str, str], list[str | None]] = {}
        for table, index, column in self._rows(sql, (schema, *tables)):
            indexes.setdefault((table, index), []).append(column)
        return [
            key for (table, index), members in indexes.items()
            if (key := key_over_columns(table, members, index == "PRIMARY")) is not None
        ]

    def foreign_keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseForeignKey]]:
        sql = (
            "SELECT TABLE_NAME, CONSTRAINT_NAME, COLUMN_NAME, 'def', REFERENCED_TABLE_SCHEMA, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            f"WHERE REFERENCED_TABLE_NAME IS NOT NULL AND TABLE_SCHEMA = %s AND TABLE_NAME IN ({placeholders(tables)}) "
            "ORDER BY TABLE_NAME, CONSTRAINT_NAME, ORDINAL_POSITION"
        )
        return foreign_keys_from_pairs(self._rows(sql, (schema, *tables)))
