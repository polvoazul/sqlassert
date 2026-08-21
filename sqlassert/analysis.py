"""The public analysis seam.

`analyze` composes the whole pipeline — parse, bind, ground, solve, report — and
returns a durable Report without printing. It is the only behavioural seam the
tests use, so parser, IR, facts, and rules can all change beneath it.
"""

from __future__ import annotations

from sqlassert import engine
from sqlassert.facts import ground_facts
from sqlassert.binding import bind
from sqlassert.knowledge import Knowledge
from sqlassert.naming import ConstantNames
from sqlassert.parsing import parse_program
from sqlassert.provenance import OriginRegistry
from sqlassert.reporting import Report, Reporter

DEFAULT_DIALECT = "duckdb"


def analyze(sql: str, knowledge: Knowledge | None = None, dialect: str = DEFAULT_DIALECT) -> Report:
    """Prove the Unique Join Assertions in one SQL Program.

    `knowledge` supplies facts about relations the program does not declare;
    omitting it behaves as empty Knowledge.
    """
    names = ConstantNames()
    origins = OriginRegistry()

    parsed = parse_program(sql, dialect)
    bound = bind(parsed, names, origins, dialect)
    facts = ground_facts(bound.program, bound.knowledge.merge(knowledge), names)

    reporter = Reporter(bound.program.assertions, origins, facts.key_columns)
    engine.run(facts, reporter)
    return reporter.report(parsed.diagnostics + bound.diagnostics)
