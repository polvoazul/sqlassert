"""The public analysis seam.

`analyze` composes the whole pipeline — parse, convert, ground, solve, report —
and returns a durable Report without printing. It is the only behavioural seam
the tests use, so parser, IR, facts, and rules can all change beneath it.
"""

from __future__ import annotations

from sqlassert.engine import Engine
from sqlassert.facts import encode
from sqlassert.ir.convert import IrParser
from sqlassert.reporting import Report, Reporter
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
    encoding = encode(conversion.program, conversion.knowledge)

    reporter = Reporter(encoding)
    engine = Engine()

    engine.run(encoding, on_solution_callback=reporter.on_model)
    return reporter.report(
        conversion.program.assertions,
        ast.diagnostics + conversion.diagnostics,
        conversion.program.named_relations,
    )
