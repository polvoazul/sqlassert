"""Database Knowledge discovery: catalog metadata bound to IR nodes before the engine runs.

Database adapters live in submodules, such as `sqlassert.discovery.duckdb`
and `sqlassert.discovery.postgres`, so importing this package never imports
a database driver. `information_schema.py` is the base of every DB-API one.
"""

from sqlassert.discovery.fetcher import (
    CatalogFacts,
    DatabaseColumn,
    DatabaseForeignKey,
    DatabaseHydration,
    DatabaseKnowledgeFetcher,
    DatabaseMetadataError,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
    DatabaseViewDefinition,
)

__all__ = [
    "CatalogFacts",
    "DatabaseColumn",
    "DatabaseForeignKey",
    "DatabaseHydration",
    "DatabaseKnowledgeFetcher",
    "DatabaseMetadataError",
    "DatabaseRelationKind",
    "DatabaseRelationMetadata",
    "DatabaseViewDefinition",
]
