"""Compose parsing, IR conversion, and property analysis into a Report."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlassert.ir.convert import IrParser
from sqlassert.python_engine import Engine
from sqlassert.reporting import Report
from sqlassert.sql_parse import SqlParser

if TYPE_CHECKING:
    from sqlassert.discovery import DatabaseKnowledgeFetcher

DEFAULT_DIALECT = "duckdb"


def analyze(sql: str, *, dialect: str = DEFAULT_DIALECT) -> Report:
    """Prove the Unique Join Assertions and Unique Set Assertions in one SQL Program.

    Pure: the Program is the only source of Knowledge. `analyze_database`
    adds a database catalog. Where a catalog would have helped (a table with
    no `CREATE`, a column only a catalog can resolve), the Report warns.
    """
    sql_parser = SqlParser(dialect)
    ir_parser = IrParser(dialect)

    ast = sql_parser.parse(sql)
    conversion = ir_parser.parse(ast)
    diagnostics = ast.diagnostics + conversion.diagnostics
    return Engine().run(conversion.program, conversion.knowledge, diagnostics, conversion.warnings)


def analyze_database(sql: str, fetcher: DatabaseKnowledgeFetcher, *, dialect: str = DEFAULT_DIALECT) -> Report:
    """`analyze`, with the external Relations the Program reaches hydrated from a catalog.

    The fetcher reads catalog metadata only, never rows, and binds it to the
    final IR before the engine runs once. No connection warnings: a relation
    the catalog lacks is already a diagnostic.
    """
    ast = SqlParser(dialect).parse(sql)
    hydration = fetcher.hydrate(ast, dialect=dialect)
    conversion = hydration.conversion
    knowledge = conversion.knowledge + fetcher.bind(conversion.program, hydration)
    diagnostics = ast.diagnostics + hydration.diagnostics + conversion.diagnostics
    return Engine().run(conversion.program, knowledge, diagnostics)
