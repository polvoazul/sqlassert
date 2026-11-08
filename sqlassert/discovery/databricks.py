"""Databricks (Unity Catalog): the standard discovery, over `databricks-sql-connector`.

Unity Catalog's `information_schema` has every standard view this reads, so
only the session query and the parameter style (`?`) differ. Primary and
foreign keys are informational: they hold only under
`trust_unenforced_keys` (`dialects.py`); the `RELY` a constraint may carry is
not read. Names qualified by another catalog are not looked up.
"""

from __future__ import annotations

from sqlassert.discovery.information_schema import InformationSchemaKnowledgeFetcher


class DatabricksKnowledgeFetcher(InformationSchemaKnowledgeFetcher):
    type_dialect = "databricks"
    paramstyle = "qmark"
    session_sql = "SELECT current_catalog(), current_schema()"
