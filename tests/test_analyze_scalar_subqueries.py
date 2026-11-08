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

from sqlassert import Outcome, analyze, suggest
from sqlassert.ir.convert import IrParser
from sqlassert.smells import Concern
from sqlassert.sql_parse import SqlParser

SCHEMA = """
CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER, PRIMARY KEY (a, b));
CREATE TABLE r (k INTEGER PRIMARY KEY, g INTEGER, x INTEGER NOT NULL, y INTEGER);
"""


def outcomes(sql: str) -> list[Outcome]:
    report = analyze(SCHEMA + sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


@pytest.mark.parametrize(
    ("label", "sql", "expected"),
    [
        # R10: equal to one value on every row, a is constant, so b alone is a key.
        ("aggregate", "SELECT a, b FROM t WHERE a = (SELECT max(x) FROM r) /**ASSERT UNIQUE(b)**/", Outcome.PROVED),
        ("filtered to a key", "SELECT a, b FROM t WHERE a = (SELECT x FROM r WHERE k = 1) /**ASSERT UNIQUE(b)**/", Outcome.PROVED),
        # More than one row fails the query, so LIMIT 1 changes no result.
        ("any body", "SELECT a, b FROM t WHERE a = (SELECT x FROM r) /**ASSERT UNIQUE(b)**/", Outcome.PROVED),
        ("star", "SELECT * FROM t WHERE a = (SELECT max(x) FROM r) /**ASSERT UNIQUE(b)**/", Outcome.PROVED),
        ("in the select list", "SELECT a, b, (SELECT max(x) FROM r) AS m FROM t /**ASSERT DETERMINES(a -> m)**/", Outcome.PROVED),
        # Correlated: at most one row per outer row, so t's key is kept.
        ("correlated", "SELECT a, b, (SELECT r.y FROM r WHERE r.k = t.c) AS m FROM t /**ASSERT UNIQUE(a, b)**/", Outcome.PROVED),
        ("correlated, unqualified", "SELECT a, b, (SELECT y FROM r WHERE k = c) AS m FROM t /**ASSERT UNIQUE(a, b)**/", Outcome.PROVED),
        # A left join pads with NULL: an empty body makes the value NULL.
        ("may be NULL", "SELECT a, b, (SELECT max(x) FROM r) AS m FROM t /**ASSERT NOT NULL(m)**/", Outcome.UNKNOWN),
        ("correlated may be NULL", "SELECT a, (SELECT r.x FROM r WHERE r.k = t.c) AS m FROM t /**ASSERT NOT NULL(m)**/", Outcome.UNKNOWN),
        # IS NULL keeps the rows a padded join would: a left join, not an inner one.
        ("IS NULL keeps rows", "SELECT a, b FROM t WHERE (SELECT max(x) FROM r) IS NULL /**ASSERT UNIQUE(a, b)**/", Outcome.PROVED),
        ("no constant without the subquery", "SELECT a, b FROM t /**ASSERT UNIQUE(b)**/", Outcome.UNKNOWN),
    ],
)
def test_scalar_subqueries(label: str, sql: str, expected: Outcome):
    assert outcomes(sql) == [expected], label


def test_a_where_subquery_adds_no_output_column():
    program = IrParser("duckdb").parse(SqlParser("duckdb").parse(SCHEMA + "SELECT * FROM t WHERE a = (SELECT max(x) FROM r)")).program
    assert program.root is not None
    assert [column.name for column in program.root.output_columns] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "sql",
    [
        # A correlated aggregate is one value per outer row, not a join.
        "SELECT a, b FROM t WHERE a = (SELECT max(r.x) FROM r WHERE r.g = t.c) /**ASSERT UNIQUE(b)**/",
        # A nested subquery stays opaque.
        "SELECT a, b FROM t WHERE a = (SELECT max(x) FROM r WHERE k IN (SELECT c FROM t)) /**ASSERT UNIQUE(b)**/",
    ],
)
def test_unlowered_scalar_subqueries_stay_opaque(sql: str):
    assert outcomes(sql) == [Outcome.UNKNOWN]


@pytest.mark.parametrize(
    ("sql", "smelly"),
    [
        ("SELECT a FROM t WHERE a = (SELECT max(x) FROM r)", False),
        ("SELECT a FROM t WHERE a = (SELECT x FROM r WHERE k = 1)", False),
        ("SELECT a FROM t WHERE a = (SELECT x FROM r)", True),
        ("SELECT a, (SELECT r.x FROM r WHERE r.k = t.c) AS m FROM t", False),
        ("SELECT a, (SELECT r.x FROM r WHERE r.g = t.c) AS m FROM t", True),
    ],
)
def test_a_scalar_subquery_not_proved_to_return_one_row_is_a_smell(sql: str, smelly: bool):
    smells = [smell for smell in suggest(SCHEMA + sql).smells if smell.concern is Concern.SCALAR_SUBQUERY]
    assert bool(smells) is smelly
    if smelly:
        assert smells[0].site.label == "scalar subquery"
        assert "more than one row" in smells[0].message
