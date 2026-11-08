"""Amazon Redshift: the standard discovery, over any DB-API connection (redshift_connector, psycopg2).

Redshift keeps PostgreSQL 8's `information_schema` and has no indexes, so
there are no unique indexes to read. Its keys are not enforced: they hold
only under `trust_unenforced_keys` (`dialects.py`). Overrides:

- session: `current_database()` and `current_schema()`. An unqualified name
  is looked up in the current schema only, so a table found later on the
  search path is reported missing, never mistaken;
- view definitions: `pg_get_viewdef`, as PostgreSQL, since
  `information_schema.views` shows a view's text to its owner only. A view
  reads the tables it was created over, so its names resolve as the
  Program's do.

Like PostgreSQL's, Redshift's `information_schema` may list constraints only
for tables the role holds a privilege on other than SELECT; a read-only role
then sees no keys, which loses proofs, never soundness.
"""

from __future__ import annotations

from sqlassert.discovery.fetcher import DatabaseRelationMetadata
from sqlassert.discovery.information_schema import InformationSchemaKnowledgeFetcher
from sqlassert.discovery.postgres import VIEW


class RedshiftKnowledgeFetcher(InformationSchemaKnowledgeFetcher):
    type_dialect = "redshift"
    session_sql = "SELECT current_database(), current_schema()"

    def session(self, scope: DatabaseRelationMetadata | None) -> tuple[str, tuple[str, ...]]:
        return super().session(None)

    def view_definition(self, relation: DatabaseRelationMetadata) -> str | None:
        rows = self._rows(VIEW, (relation.schema, relation.name))
        return rows[0][0] if rows else None
