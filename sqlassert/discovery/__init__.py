"""Database Knowledge discovery: catalog metadata bound to IR nodes before the engine runs.

Database adapters live in submodules, such as `sqlassert.discovery.duckdb`,
so importing this package never imports a database driver.
"""

from sqlassert.discovery.fetcher import (
    DatabaseColumn,
    DatabaseHydration,
    DatabaseKnowledgeFetcher,
    DatabaseMetadataError,
    DatabaseRelationKind,
    DatabaseRelationMetadata,
    DatabaseViewDefinition,
)

__all__ = [
    "DatabaseColumn",
    "DatabaseHydration",
    "DatabaseKnowledgeFetcher",
    "DatabaseMetadataError",
    "DatabaseRelationKind",
    "DatabaseRelationMetadata",
    "DatabaseViewDefinition",
]
