"""Snowflake: the standard discovery, with `SHOW ... KEYS` for the keys.

Over `snowflake-connector-python` (`%s` parameters). Snowflake's
`INFORMATION_SCHEMA` has `TABLE_CONSTRAINTS` and `REFERENTIAL_CONSTRAINTS`
but no `KEY_COLUMN_USAGE`, so a constraint's columns come from `SHOW PRIMARY
KEYS`, `SHOW UNIQUE KEYS` and `SHOW IMPORTED KEYS` instead. Keys are not
enforced: they hold only under `trust_unenforced_keys` (`dialects.py`); the
`rely` column those commands return is not read. `VIEWS.VIEW_DEFINITION` is
the whole `CREATE VIEW`, and names inside it resolve in the view's own
database and schema. Names qualified by another database are not looked up.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlassert.discovery.fetcher import DatabaseForeignKey
from sqlassert.discovery.information_schema import (
    InformationSchemaKnowledgeFetcher,
    Key,
    foreign_keys_from_pairs,
    key_over_columns,
    quoted,
)

# Positions in the rows of SHOW PRIMARY KEYS and SHOW UNIQUE KEYS.
_KEY_TABLE, _KEY_COLUMN, _KEY_SEQUENCE, _KEY_NAME = 3, 4, 5, 6
# Positions in the rows of SHOW IMPORTED KEYS.
_PK_CATALOG, _PK_SCHEMA, _PK_TABLE, _PK_COLUMN, _FK_TABLE, _FK_COLUMN, _FK_SEQUENCE, _FK_NAME = 1, 2, 3, 4, 7, 8, 9, 12


class SnowflakeKnowledgeFetcher(InformationSchemaKnowledgeFetcher):
    type_dialect = "snowflake"
    session_sql = "SELECT CURRENT_DATABASE(), CURRENT_SCHEMA()"

    def keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[Key]:
        keys: list[Key] = []
        for command, primary in (("PRIMARY KEYS", True), ("UNIQUE KEYS", False)):
            grouped: dict[tuple[str, str], list[tuple[int, str | None]]] = {}
            for row in self._show(command, catalog, schema, tables, _KEY_TABLE):
                grouped.setdefault((row[_KEY_TABLE], row[_KEY_NAME]), []).append((int(row[_KEY_SEQUENCE]), row[_KEY_COLUMN]))
            for (table, _), parts in grouped.items():
                key = key_over_columns(table, [column for _, column in sorted(parts)], primary)
                if key is not None:
                    keys.append(key)
        return keys

    def foreign_keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseForeignKey]]:
        rows = sorted(
            self._show("IMPORTED KEYS", catalog, schema, tables, _FK_TABLE),
            key=lambda row: (row[_FK_TABLE], row[_FK_NAME], int(row[_FK_SEQUENCE])),
        )
        return foreign_keys_from_pairs([
            (row[_FK_TABLE], row[_FK_NAME], row[_FK_COLUMN], row[_PK_CATALOG], row[_PK_SCHEMA], row[_PK_TABLE], row[_PK_COLUMN])
            for row in rows
        ])

    def _show(self, command: str, catalog: str, schema: str, tables: Sequence[str], table_position: int) -> list[tuple[Any, ...]]:
        """The rows of `SHOW <command> IN SCHEMA` for ``tables``. SHOW takes no parameters."""
        wanted = set(tables)
        rows = self._rows(f"SHOW {command} IN SCHEMA {quoted(catalog)}.{quoted(schema)}")
        return [row for row in rows if row[table_position] in wanted]
