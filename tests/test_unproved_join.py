"""Why a join has no proved Join Cardinality (`unproved_join`)."""

import pytest

from sqlassert import ir
from sqlassert.ir.convert import IrParser
from sqlassert.python_engine import Engine
from sqlassert.rules.join_cardinality import UnprovedJoin, unproved_join
from sqlassert.sql_parse import SqlParser

SCHEMA = """
CREATE TABLE a (id INTEGER PRIMARY KEY, k INTEGER, ts TIMESTAMP, doc VARCHAR, amount DECIMAL(10, 2));
CREATE TABLE b (id INTEGER PRIMARY KEY, k INTEGER, start_ts TIMESTAMP, end_ts TIMESTAMP, day DATE, amount DECIMAL(10, 2));
CREATE TABLE f (k REAL, x INTEGER);
"""


def classify(sql: str) -> UnprovedJoin | None:
    """The last join written, the outermost."""
    conversion = IrParser("duckdb").parse(SqlParser("duckdb").parse(SCHEMA + sql))
    program = conversion.program
    joins = list(dict.fromkeys(node for _, node in program.attachment_sites if isinstance(node, ir.Join)))
    with Engine().session(program, conversion.knowledge):
        return unproved_join(joins[-1])


CASES = [
    ("proved", "SELECT a.id FROM a JOIN b ON a.id = b.id", None),
    ("semi join", "SELECT a.id FROM a SEMI JOIN b ON a.k = b.k", None),
    ("table function", "SELECT a.id FROM a LEFT JOIN json_each(a.doc) AS e ON true", UnprovedJoin("unsupported", construct="a table function")),
    (
        "inside an opaque SELECT",
        "SELECT a.k, unnest([1, 2]) AS v FROM a JOIN b ON a.id = b.id GROUP BY a.k",
        UnprovedJoin("unsupported"),
    ),
    ("cross join", "SELECT a.id FROM a CROSS JOIN b", UnprovedJoin("unconstrained")),
    ("range join", "SELECT a.id FROM a JOIN b ON a.ts >= b.start_ts AND a.ts < b.end_ts", UnprovedJoin("unconstrained")),
    ("ambiguous column", "SELECT a.id FROM a JOIN missing AS m ON ts = m.k", UnprovedJoin("unresolved-key")),
    (
        "VALUES key of mixed literals",
        "SELECT a.id FROM a JOIN (VALUES (1), (1.5)) AS v (k) ON a.k = v.k",
        UnprovedJoin("untyped-key", "values"),
    ),
    (
        "VALUES key that is an expression",
        "SELECT a.id FROM a JOIN (VALUES (1 + 1), (3)) AS v (k) ON a.k = v.k",
        UnprovedJoin("untyped-key", "values"),
    ),
    (
        "typed VALUES key against a decimal",
        "SELECT a.id FROM a JOIN (VALUES (1), (2)) AS v (k) ON a.amount = v.k",
        UnprovedJoin("untyped-key", "type-mismatch"),
    ),
    ("typed VALUES key", "SELECT a.id FROM a JOIN (VALUES (1), (2)) AS v (k) ON a.k = v.k", None),
    (
        "computed key",
        "SELECT a.id FROM a JOIN (SELECT date_trunc('month', ts) AS day FROM a) AS m ON m.day = a.id",
        UnprovedJoin("untyped-key", "computed"),
    ),
    (
        "opaque relation key",
        "SELECT a.id FROM a JOIN (missing JOIN b ON true) AS m ON a.k = m.k",
        UnprovedJoin("untyped-key", "opaque-relation"),
    ),
    (
        "opaque SELECT",
        "SELECT a.id FROM a JOIN (SELECT * FROM b JOIN f USING (k) QUALIFY row_number() OVER (PARTITION BY k ORDER BY id) = 1) AS m "
        "ON a.k = m.k",
        UnprovedJoin("unsupported", construct="a star over a USING join"),
    ),
    (
        "key read from an opaque SELECT",
        "SELECT a.id FROM a JOIN (SELECT k FROM (SELECT * FROM b JOIN f USING (k) QUALIFY row_number() OVER (PARTITION BY k ORDER BY id) = 1)) "
        "AS m ON a.k = m.k",
        UnprovedJoin("untyped-key", "opaque-relation", "a star over a USING join"),
    ),
    (
        "key read through a QUALIFY that drops rows",
        "SELECT a.id FROM a JOIN (SELECT k FROM b QUALIFY rank() OVER (ORDER BY id) = 1) AS m ON a.k = m.k",
        UnprovedJoin("no-unique-set"),
    ),
    (
        "partition key that is not selected",
        "SELECT a.id FROM a JOIN (SELECT k FROM b QUALIFY row_number() OVER (PARTITION BY k, day ORDER BY id) = 1) AS m ON a.k = m.k",
        UnprovedJoin("no-unique-set"),
    ),
    (
        "set operation key",
        "SELECT a.id FROM a JOIN (SELECT * FROM a UNION ALL BY NAME SELECT * FROM b) AS s ON a.k = s.k",
        UnprovedJoin("untyped-key", "set-operation"),
    ),
    ("undeclared table key", "SELECT a.id FROM a JOIN missing AS m ON a.k = m.k", UnprovedJoin("untyped-key", "table")),
    ("type mismatch", "SELECT a.id FROM a JOIN f ON a.k = f.k", UnprovedJoin("untyped-key", "type-mismatch")),
    ("expression key", "SELECT a.id FROM a JOIN b ON b.day = CAST(a.ts AS DATE)", UnprovedJoin("expression-key")),
    ("negated key", "SELECT a.id FROM a JOIN b ON a.amount = -b.amount", UnprovedJoin("expression-key")),
    ("no unique set", "SELECT a.id FROM a JOIN b ON a.k = b.k", UnprovedJoin("no-unique-set")),
]


@pytest.mark.parametrize(("label", "sql", "expected"), CASES, ids=[label for label, _, _ in CASES])
def test_unproved_join(label: str, sql: str, expected: UnprovedJoin | None):
    assert classify(sql) == expected
