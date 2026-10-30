"""Compose parsing, IR conversion, encoding, solving, and public-property reporting."""

from __future__ import annotations

from sqlassert.clingo_engine import Engine as ClingoEngine
from sqlassert.facts import encode
from sqlassert.ir.convert import IrParser
from sqlassert.reporting import Report, Reporter
from sqlassert.sql_parse import SqlParser

DEFAULT_DIALECT = "duckdb"


def analyze(sql: str, *, dialect: str = DEFAULT_DIALECT, engine: str = "python") -> Report:
    """Prove the Unique Join Assertions and Unique Set Assertions in one SQL Program.

    Database knowledge gatherers bind their results to this Program before it
    reaches the engine.
    """
    sql_parser = SqlParser(dialect)
    ir_parser = IrParser(dialect)

    ast = sql_parser.parse(sql)
    conversion = ir_parser.parse(ast)
    diagnostics = ast.diagnostics + conversion.diagnostics
    if engine == "python":
        from sqlassert.python_engine import Engine as PythonEngine

        return PythonEngine().run(conversion.program, conversion.knowledge, diagnostics)
    if engine != "clingo":
        raise ValueError(f"unknown property engine: {engine}")
    encoding = encode(conversion.program, conversion.knowledge)

    reporter = Reporter(encoding)
    solver = ClingoEngine()

    solver.run(encoding, on_solution_callback=reporter.on_model)
    return reporter.report(
        conversion.program.assertions,
        diagnostics,
        conversion.program.named_relations,
    )
