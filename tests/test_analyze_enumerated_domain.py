"""Enumerated Domains: `/**ASSERT ENUM(col IN ('a', 'b'))**/` and its `DECLARE` form.

An Enumerated Domain bounds a column's non-null values to a finite set. It
comes from literals, declarations, external Knowledge, and filters, and
crosses value equalities. NULL is never in a domain: `NOT NULL` governs it.
"""

from __future__ import annotations


from sqlassert import AssertionReport, EnumeratedDomain, Outcome
from sqlassert.ir import LiteralValue
from sqlassert.ir.convert import IrParser
from sqlassert.python_engine import Engine
from sqlassert.sql_parse import SqlParser

SCHEMA = """
CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR, channel VARCHAR, kind VARCHAR);
CREATE TABLE codes (code VARCHAR, label VARCHAR);
CREATE TABLE items (id INTEGER PRIMARY KEY, qty INTEGER, size INTEGER, ratio FLOAT, flag BOOLEAN, tag VARCHAR);
"""

# A source of statuses the author vouches for.

# A source of quantities and flags the author vouches for.


def explanation(assertion: AssertionReport) -> str:
    assert assertion.evidence is not None
    return assertion.evidence.explain()


def test_external_knowledge_supplies_a_domain():
    sql = f"{SCHEMA} SELECT status FROM orders /**ASSERT ENUM(status IN ('new', 'paid'))**/"
    conversion = IrParser("duckdb").parse(SqlParser("duckdb").parse(sql))
    orders, = (relation for relation in conversion.program.named_relations if relation.name == "orders")
    status, = (column for column in orders.output_columns if column.name == "status")
    knowledge = (EnumeratedDomain(column=status, values=frozenset({LiteralValue(type="string", text="new")})),)

    without = Engine().run(conversion.program, conversion.knowledge)
    report = Engine().run(conversion.program, conversion.knowledge + knowledge)

    assert [a.outcome for a in without.assertions] == [Outcome.UNKNOWN]
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]
    assert "Accepted EnumeratedDomain" in explanation(report.assertions[0])


# Set operations (issue 31) ---------------------------------------------------


# An OR bounds a column to the union of its branches' bounds when every branch
# bounds it; a branch bounds it to what all its bounding parts allow.
