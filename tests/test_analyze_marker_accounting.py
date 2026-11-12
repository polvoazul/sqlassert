"""Every written marker is accounted for: it states an Assertion or a Property
Declaration, or it is reported by a Diagnostic on its own line.

A marker that vanishes reads to its author as a proof, so this is checked
across every marker kind and every place a marker can be written, including
places the analysis does not model.
"""

from __future__ import annotations

import pytest

from sqlassert.ir.convert import IrParser
from sqlassert.sql_parse import SqlParser

COLUMN_MARKERS = [
    "/**ASSERT UNIQUE(name)**/",
    "/**ASSERT NOT NULL UNIQUE(name)**/",
    "/**ASSERT NOT NULL(name)**/",
    "/**ASSERT FOREIGN KEY(name) REFERENCES c(id)**/",
    "/**ASSERT DETERMINES(name -> id)**/",
    "/**ASSERT ENUM(name IN ('x', 'y'))**/",
    "/**DECLARE UNIQUE(name)**/",
    "/**DECLARE NOT NULL UNIQUE(name)**/",
    "/**DECLARE NOT NULL(name)**/",
    "/**DECLARE FOREIGN KEY(name) REFERENCES c(id)**/",
    "/**DECLARE DETERMINES(name -> id)**/",
    "/**DECLARE ENUM(name IN ('x', 'y'))**/",
]

# Each `{m}` is where the marker is written, on a line of its own.
SELECT_CONTEXTS = {
    "declared, column present": "CREATE TABLE t (id INT, name TEXT);\nSELECT id, name FROM t\n{m}",
    "declared, column absent": "CREATE TABLE t (id INT);\nSELECT id FROM t\n{m}",
    "undeclared, one source": "SELECT * FROM a\n{m}",
    "undeclared, two sources, star": "SELECT * FROM a JOIN b ON a.id = b.aid\n{m}",
    "undeclared, two sources, listed": "SELECT a.id FROM a JOIN b ON a.id = b.aid\n{m}",
    "undeclared, DISTINCT star": "SELECT DISTINCT * FROM a JOIN b ON a.id = b.aid\n{m}",
    "view over two undeclared sources": "CREATE VIEW v AS SELECT * FROM a JOIN b ON a.id = b.aid\n{m};\nSELECT * FROM v",
    "unread view": "CREATE VIEW v AS SELECT * FROM a JOIN b ON a.id = b.aid\n{m};",
    "CTE": "WITH w AS (SELECT * FROM a JOIN b ON a.id = b.aid\n{m}\n) SELECT * FROM w",
    "unused CTE": "WITH w AS (SELECT * FROM a\n{m}\n) SELECT * FROM b",
    "FROM subquery": "SELECT * FROM (SELECT * FROM a JOIN b ON a.id = b.aid\n{m}\n) s",
    "WHERE subquery": "SELECT * FROM a WHERE id IN (SELECT name FROM b\n{m}\n)",
    "scalar subquery": "SELECT (SELECT max(name) AS name FROM b\n{m}\n) AS m FROM a",
    "no FROM": "SELECT 1 AS name\n{m}",
    "set operation arm": "SELECT name FROM a\n{m}\nUNION SELECT name FROM b",
    "HAVING": "SELECT name, count(*) AS n FROM a GROUP BY name HAVING count(*) > 1\n{m}",
}

JOIN_CONTEXTS = {
    "root": "SELECT * FROM a\n{m} JOIN b ON a.id = b.aid",
    "WHERE subquery": "SELECT * FROM a WHERE id IN (SELECT b.id FROM b\n{m} JOIN c ON b.id = c.id)",
    "unused CTE": "WITH w AS (SELECT * FROM a\n{m} JOIN b ON a.id = b.aid) SELECT * FROM c",
    "parenthesized join": "SELECT * FROM a JOIN (b\n{m} JOIN c ON b.id = c.id) ON a.id = b.aid",
    "aliased parenthesized join": "SELECT * FROM a JOIN (b\n{m} JOIN c ON b.id = c.id) AS j ON a.id = j.aid",
    "parenthesized FROM": "SELECT * FROM (a\n{m} JOIN b ON a.id = b.aid)",
    "join onto a parenthesized join": "SELECT * FROM a\n{m} JOIN (b JOIN c ON b.id = c.id) ON a.id = b.aid",
}

CASE_CONTEXTS = {
    "select list": "SELECT\n{m} CASE name WHEN 'x' THEN 1 END AS n FROM a",
    "inside an opaque expression": "SELECT upper(\n{m} CASE name WHEN 'x' THEN 'a' END) AS n FROM a",
    "WHERE subquery": "SELECT * FROM a WHERE id IN (SELECT\n{m} CASE name WHEN 'x' THEN 1 END FROM b)",
}


def _accounted_for(sql: str) -> bool:
    """Whether the one marker in `sql`, on the line after `{m}`'s line break,
    states something or is diagnosed on its line."""
    line = sql[: sql.index("/**")].count("\n") + 1
    dialect = "duckdb"
    ast = SqlParser(dialect).parse(sql)
    conversion = IrParser(dialect).parse(ast)
    program = conversion.program
    lines = {assertion.origin.line for assertion in program.assertions}
    lines |= {diagnostic.origin.line for diagnostic in (*ast.diagnostics, *conversion.diagnostics) if diagnostic.origin}
    return line in lines or bool(program.declarations)


def _cases(contexts: dict[str, str], markers: list[str]):
    return [pytest.param(context.format(m=marker), id=f"{label}: {marker}") for label, context in contexts.items() for marker in markers]


@pytest.mark.parametrize(
    "sql",
    _cases(SELECT_CONTEXTS, COLUMN_MARKERS)
    + _cases(JOIN_CONTEXTS, ["/**ASSERT MANY TO ONE**/", "/**ASSERT EXACTLY ONE**/"])
    + _cases(CASE_CONTEXTS, ["/**ASSERT EXHAUSTIVE**/"]),
)
def test_every_marker_states_something_or_is_diagnosed(sql: str):
    assert _accounted_for(sql), sql
