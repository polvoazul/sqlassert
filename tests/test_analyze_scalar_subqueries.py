"""Scalar subqueries as left joins (issue 38).

A scalar subquery with body B is a left join with B: no row makes it NULL,
which is a left join's padding. More than one row is a runtime error, so the
join is many to one, or the query fails. Uncorrelated, B sits under an
implied LIMIT 1, so its value is the same on every row: `a = (SELECT ...)`
makes `a` constant (Paulley's R10). Correlated, B's WHERE is the ON of a
dependent join.
"""

from __future__ import annotations

import pytest

from sqlassert import suggest
from sqlassert.ir.convert import IrParser
from sqlassert.hazards import Concern
from sqlassert.sql_parse import SqlParser

SCHEMA = """
CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER, PRIMARY KEY (a, b));
CREATE TABLE r (k INTEGER PRIMARY KEY, g INTEGER, x INTEGER NOT NULL, y INTEGER);
"""


def test_a_where_subquery_adds_no_output_column():
    program = IrParser("duckdb").parse(SqlParser("duckdb").parse(SCHEMA + "SELECT * FROM t WHERE a = (SELECT max(x) FROM r)")).program
    assert program.root is not None
    assert [column.name for column in program.root.output_columns] == ["a", "b", "c"]


@pytest.mark.parametrize(
    ("sql", "hazardous"),
    [
        ("SELECT a FROM t WHERE a = (SELECT max(x) FROM r)", False),
        ("SELECT a FROM t WHERE a = (SELECT x FROM r WHERE k = 1)", False),
        ("SELECT a FROM t WHERE a = (SELECT x FROM r)", True),
        ("SELECT a, (SELECT r.x FROM r WHERE r.k = t.c) AS m FROM t", False),
        ("SELECT a, (SELECT r.x FROM r WHERE r.g = t.c) AS m FROM t", True),
    ],
)
def test_a_scalar_subquery_not_proved_to_return_one_row_is_a_hazard(sql: str, hazardous: bool):
    hazards = [hazard for hazard in suggest(SCHEMA + sql).hazards if hazard.concern is Concern.SCALAR_SUBQUERY]
    assert bool(hazards) is hazardous
    if hazardous:
        assert hazards[0].site.label == "scalar subquery"
        assert "more than one row" in hazards[0].message
