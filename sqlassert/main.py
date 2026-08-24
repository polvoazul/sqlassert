"""The public analysis seam.

`analyze` composes the whole pipeline — parse, convert, ground, solve, report —
and returns a durable Report without printing. It is the only behavioural seam
the tests use, so parser, IR, facts, and rules can all change beneath it.
"""

from __future__ import annotations

from sqlassert.engine import Engine
from sqlassert.ir.convert import IrParser
from sqlassert.knowledge import Knowledge
from sqlassert.reporting import Report, Reporter
from sqlassert.sql_parse import SqlParser

DEFAULT_DIALECT = "duckdb"


def analyze(sql: str, *, knowledge: Knowledge | None = None, dialect: str = DEFAULT_DIALECT) -> Report:
    """Prove the Unique Join Assertions in one SQL Program.

    `knowledge` supplies facts about relations the program does not declare;
    omitting it behaves as empty Knowledge.
    """
    sql_parser = SqlParser(dialect)
    ir_parser = IrParser(dialect)

    ast = sql_parser.parse(sql)
    ir = ir_parser.parse(ast)

    reporter = Reporter(ir.program.assertions, ir_parser.origins)
    engine = Engine(reporter, ir_parser.names)

    engine.run(ir.program, ir.knowledge.merge(knowledge))
    return reporter.report(ast.diagnostics + ir.diagnostics)
