"""Compose parsing, IR conversion, and property analysis into a Report."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from sqlassert import diagnostics as diag
from sqlassert.ir.convert import IrParser
from sqlassert.python_engine import Engine
from sqlassert.reporting import Report
from sqlassert.sql_parse import SqlParser

if TYPE_CHECKING:
    from sqlassert.discovery import DatabaseKnowledgeFetcher

DEFAULT_DIALECT = "duckdb"


def analyze(sql: str, *, dialect: str = DEFAULT_DIALECT, trust_unenforced_keys: bool = True) -> Report:
    """Prove the Unique Join Assertions and Unique Set Assertions in one SQL Program.

    Pure: the Program is the only source of Knowledge. `analyze_database`
    adds a database catalog. Where a catalog would have helped (a table with
    no `CREATE`, a column only a catalog can resolve), the Report warns.
    `trust_unenforced_keys` (on by default) makes the keys the dialect does
    not enforce Property Declarations, as `RELY` does; off, they are ignored
    with an `unenforced-constraint` warning (`docs/dialects.md`).
    """
    sql_parser = SqlParser(dialect)
    ir_parser = IrParser(dialect, trust_unenforced_keys)

    ast = sql_parser.parse(sql)
    conversion = ir_parser.parse(ast)
    diagnostics = ast.diagnostics + conversion.diagnostics
    return Engine().run(conversion.program, conversion.knowledge, diagnostics, conversion.warnings)


def analyze_database(
    sql: str, fetcher: DatabaseKnowledgeFetcher, *, dialect: str = DEFAULT_DIALECT, trust_unenforced_keys: bool = True,
) -> Report:
    """`analyze`, with the external Relations the Program reaches hydrated from a catalog.

    The fetcher reads catalog metadata only, never rows, and binds it to the
    final IR before the engine runs once. No connection warnings: a relation
    the catalog lacks is already a diagnostic. A catalog key the dialect does
    not enforce warns, unless `trust_unenforced_keys` declares it, and so do
    the SQL's own warnings (`diagnostics.SQL_WARNINGS`).
    """
    ast = SqlParser(dialect).parse(sql)
    hydration = fetcher.hydrate(ast, dialect=dialect, trust_unenforced_keys=trust_unenforced_keys)
    conversion = hydration.conversion
    facts = fetcher.bind(conversion.program, hydration)
    program = replace(conversion.program, declarations=conversion.program.declarations + facts.declarations)
    diagnostics = ast.diagnostics + hydration.diagnostics + conversion.diagnostics
    warnings = (*facts.warnings, *(warning for warning in conversion.warnings if warning.code in diag.SQL_WARNINGS))
    return Engine().run(program, conversion.knowledge + facts.knowledge, diagnostics, warnings)
