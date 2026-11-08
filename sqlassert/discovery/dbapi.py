"""Catalog queries over a DB-API 2.0 connection, for the Postgres and MySQL adapters.

Any driver works (psycopg, psycopg2, PyMySQL, mysqlclient, mysql-connector):
the adapters pass parameters with the `%s` placeholder those drivers share,
and never import a driver themselves.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlassert.discovery.fetcher import DatabaseKnowledgeFetcher, DatabaseMetadataError


class Cursor(Protocol):
    def execute(self, operation: str, parameters: Any = ..., /) -> Any: ...
    def fetchall(self) -> Any: ...
    def close(self) -> Any: ...


class Connection(Protocol):
    def cursor(self) -> Cursor: ...


class DbApiKnowledgeFetcher(DatabaseKnowledgeFetcher):
    """A catalog read through one DB-API connection; subclasses write its queries."""

    def __init__(self, connection: Connection):
        self.connection = connection

    def _rows(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        """The rows of one catalog query. Any driver error is a
        DatabaseMetadataError: DB-API drivers share no exception base class."""
        try:
            cursor = self.connection.cursor()
            try:
                cursor.execute(sql, parameters)
                return [tuple(row) for row in cursor.fetchall()]
            finally:
                cursor.close()
        except Exception as error:
            raise DatabaseMetadataError(str(error)) from error


def placeholders(count: int) -> str:
    """`%s, %s, ...`: one placeholder per value, for an IN list."""
    return ", ".join(["%s"] * count)
