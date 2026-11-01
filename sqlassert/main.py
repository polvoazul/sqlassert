"""Compose parsing, IR conversion, and property analysis into a Report."""

from __future__ import annotations

from sqlassert.ir.convert import IrParser
from sqlassert.python_engine import Engine
from sqlassert.reporting import Report
from sqlassert.sql_parse import SqlParser

DEFAULT_DIALECT = "duckdb"


def analyze(sql: str, *, dialect: str = DEFAULT_DIALECT) -> Report:
    """Prove the Unique Join Assertions and Unique Set Assertions in one SQL Program.

    Database knowledge gatherers bind their results to this Program before it
    reaches the engine.
    """
    sql_parser = SqlParser(dialect)
    ir_parser = IrParser(dialect)

    ast = sql_parser.parse(sql)
    conversion = ir_parser.parse(ast)
    diagnostics = ast.diagnostics + conversion.diagnostics
    return Engine().run(conversion.program, conversion.knowledge, diagnostics)
