"""Trino: the standard discovery, each catalog's own `information_schema`, over the `trino` client.

Trino federates catalogs, each with its own `catalog.information_schema`;
a name may be qualified by any catalog. Its `information_schema` has
`TABLES`, `COLUMNS` and `VIEWS` but no constraint views: Trino has no key
constraints, and does not surface those a connector's source enforces, so a
Trino catalog reports NOT NULLs and no keys. That loses proofs, never
soundness. Parameters are `?` (the client's qmark style).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlassert.discovery.fetcher import DatabaseForeignKey, DatabaseMetadataError, DatabaseRelationMetadata
from sqlassert.discovery.information_schema import InformationSchemaKnowledgeFetcher, Key, quoted


class TrinoKnowledgeFetcher(InformationSchemaKnowledgeFetcher):
    type_dialect = "trino"
    paramstyle = "qmark"
    cross_catalog = True

    def session(self, scope: DatabaseRelationMetadata | None) -> tuple[str, tuple[str, ...]]:
        catalog, path = super().session(scope)
        if catalog is None:  # The client may connect without a catalog.
            raise DatabaseMetadataError("the Trino session has no current catalog: connect with catalog=")
        return catalog, path

    def information_schema(self, catalog: str, schema: str) -> str:
        return f"{quoted(catalog)}.information_schema"

    def keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[Key]:
        return []

    def foreign_keys(self, catalog: str, schema: str, tables: Sequence[str]) -> list[tuple[str, DatabaseForeignKey]]:
        return []
