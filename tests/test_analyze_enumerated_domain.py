"""Enumerated Domains: `/**ASSERT ENUM(col IN ('a', 'b'))**/` and its `DECLARE` form.

An Enumerated Domain bounds a column's non-null values to a finite set. It
comes from literals, declarations, external Knowledge, and filters, and
crosses value equalities. NULL is never in a domain: `NOT NULL` governs it.
"""

from __future__ import annotations

import pytest

from sqlassert import AssertionReport, EnumeratedDomain, Outcome, analyze
from sqlassert.diagnostics import UNATTACHED_MARKER, UNKNOWN_ASSERTED_COLUMN
from sqlassert.ir import LiteralValue
from sqlassert.ir.convert import IrParser
from sqlassert.python_engine import Engine
from sqlassert.sql_parse import SqlParser

SCHEMA = """
CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR, channel VARCHAR, kind VARCHAR);
CREATE TABLE codes (code VARCHAR, label VARCHAR);
"""

# A source of statuses the author vouches for.
DECLARED = "WITH o AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('new', 'paid'))**/) "


def outcomes(sql: str) -> list[Outcome]:
    report = analyze(f"{SCHEMA} {sql}")
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


def explanation(assertion: AssertionReport) -> str:
    assert assertion.evidence is not None
    return assertion.evidence.explain()


PROVED = [
    ("string literal", "SELECT 'a' AS tag FROM orders /**ASSERT ENUM(tag IN ('a', 'b'))**/"),
    ("null literal is outside every domain", "SELECT NULL AS tag FROM orders /**ASSERT ENUM(tag IN ('a'))**/"),
    ("coalesce of literals", "SELECT coalesce(NULL, 'b') AS tag FROM orders /**ASSERT ENUM(tag IN ('a', 'b'))**/"),
    ("declared, same set", DECLARED + "SELECT status FROM o /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ("declared, superset asserted", DECLARED + "SELECT status FROM o /**ASSERT ENUM(status IN ('paid', 'new', 'void'))**/"),
    ("renamed", DECLARED + "SELECT status AS state FROM o /**ASSERT ENUM(state IN ('new', 'paid'))**/"),
    ("filtered", DECLARED + "SELECT status FROM o WHERE id > 3 /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ("alias", DECLARED + "SELECT x.status FROM o AS x /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ("nested cte", DECLARED + ", p AS (SELECT status FROM o) SELECT status FROM p /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ("subquery", DECLARED + "SELECT status FROM (SELECT status FROM o) AS s /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ("grouping key", DECLARED + "SELECT status, count(*) AS n FROM o GROUP BY status /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ("distinct", DECLARED + "SELECT DISTINCT status FROM o /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ("left join, padded side", DECLARED + "SELECT o.status FROM codes c LEFT JOIN o ON c.code = o.channel /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ("where equality to a literal", "SELECT status FROM orders WHERE status = 'paid' /**ASSERT ENUM(status IN ('paid'))**/"),
    ("literal on the left", "SELECT status FROM orders WHERE 'paid' = status /**ASSERT ENUM(status IN ('paid'))**/"),
    ("where in list", "SELECT status FROM orders WHERE status IN ('new', 'paid') /**ASSERT ENUM(status IN ('new', 'paid', 'void'))**/"),
    ("inner join ON literal", "SELECT o.status FROM codes c JOIN orders o ON o.status = 'new' /**ASSERT ENUM(status IN ('new'))**/"),
    ("left join ON literal, padded side", "SELECT o.status FROM codes c LEFT JOIN orders o ON o.status = 'new' /**ASSERT ENUM(status IN ('new'))**/"),
    ("declared and narrowed", DECLARED + "SELECT status FROM o WHERE status IN ('paid', 'void') /**ASSERT ENUM(status IN ('paid'))**/"),
    ("across a where equality", DECLARED + "SELECT orders.channel FROM o JOIN orders ON TRUE WHERE orders.channel = o.status /**ASSERT ENUM(channel IN ('new', 'paid'))**/"),
    ("across an inner join", DECLARED + "SELECT c.code FROM o JOIN codes c ON c.code = o.status /**ASSERT ENUM(code IN ('new', 'paid'))**/"),
    ("across an equality to a narrowed column", "SELECT channel FROM orders WHERE channel = status AND status = 'x' /**ASSERT ENUM(channel IN ('x'))**/"),
    ("view", "CREATE VIEW v AS SELECT status FROM orders /**DECLARE ENUM(status IN ('new'))**/; SELECT status FROM v /**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    (
        "intersecting declarations across an equality",
        "WITH a AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('x', 'y'))**/ /**DECLARE ENUM(channel IN ('y', 'z'))**/) "
        "SELECT status FROM a WHERE status = channel /**ASSERT ENUM(status IN ('y'))**/",
    ),
    (
        "intersect keeps the left domain",
        DECLARED + ", i AS (SELECT status FROM o INTERSECT SELECT status FROM orders) SELECT status FROM i /**ASSERT ENUM(status IN ('new', 'paid'))**/",
    ),
]


@pytest.mark.parametrize(("label", "sql"), PROVED)
def test_enum_is_proved(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


UNKNOWN = [
    ("table column", "SELECT status FROM orders /**ASSERT ENUM(status IN ('new'))**/"),
    ("literal outside the set", "SELECT 'c' AS tag FROM orders /**ASSERT ENUM(tag IN ('a', 'b'))**/"),
    ("declared, subset asserted", DECLARED + "SELECT status FROM o /**ASSERT ENUM(status IN ('new'))**/"),
    ("opaque expression", DECLARED + "SELECT upper(status) AS status FROM o /**ASSERT ENUM(status IN ('NEW', 'PAID'))**/"),
    ("coalesce with an unbounded argument", "SELECT coalesce(status, 'a') AS tag FROM orders /**ASSERT ENUM(tag IN ('a'))**/"),
    ("left join ON literal, preserved side", "SELECT o.status FROM orders o LEFT JOIN codes c ON o.status = 'new' /**ASSERT ENUM(status IN ('new'))**/"),
    ("outer join ON never crosses", DECLARED + "SELECT c.code FROM codes c LEFT JOIN o ON c.code = o.status /**ASSERT ENUM(code IN ('new', 'paid'))**/"),
    ("disjunction", "SELECT status FROM orders WHERE status = 'a' OR id = 1 /**ASSERT ENUM(status IN ('a'))**/"),
    ("negated in list", "SELECT status FROM orders WHERE status NOT IN ('a') /**ASSERT ENUM(status IN ('b'))**/"),
    ("union", "WITH u AS (SELECT 'a' AS tag UNION ALL SELECT 'b') SELECT tag FROM u /**ASSERT ENUM(tag IN ('a', 'b'))**/"),
    ("aggregate", DECLARED + "SELECT max(status) AS m FROM o /**ASSERT ENUM(m IN ('new', 'paid'))**/"),
]


@pytest.mark.parametrize(("label", "sql"), UNKNOWN)
def test_enum_is_not_guessed(label: str, sql: str):
    assert outcomes(sql) == [Outcome.UNKNOWN], label


def test_an_unknown_report_names_every_value_the_assertion_does_not_cover():
    report = analyze(f"{SCHEMA} {DECLARED}SELECT status FROM o /**ASSERT ENUM(status IN ('void'))**/")

    assertion, = report.assertions
    assert assertion.outcome is Outcome.UNKNOWN
    assert "status may be 'new', 'paid', which the assertion does not cover" in explanation(assertion)


def test_an_unknown_report_says_when_no_domain_is_known():
    report = analyze(f"{SCHEMA} SELECT status FROM orders /**ASSERT ENUM(status IN ('new'))**/")

    assert "No finite domain is known for status" in explanation(report.assertions[0])


def test_a_declaration_produces_no_assertion_and_is_shown_as_declared():
    report = analyze(f"{SCHEMA} {DECLARED}SELECT status FROM o /**ASSERT ENUM(status IN ('new', 'paid'))**/")

    assertion, = report.assertions
    assert "Declared EnumeratedDomain" in explanation(assertion)


def test_a_declaration_does_not_claim_its_values_occur_or_that_the_column_is_non_null():
    sql = DECLARED + "SELECT status FROM o /**ASSERT NOT NULL(status)**/"

    assert outcomes(sql) == [Outcome.UNKNOWN]


def test_not_null_and_enum_together_bound_every_value():
    sql = DECLARED + ", p AS (SELECT status FROM o /**DECLARE NOT NULL(status)**/) SELECT status FROM p /**ASSERT ENUM(status IN ('new', 'paid'))**/ /**ASSERT NOT NULL(status)**/"

    assert outcomes(sql) == [Outcome.PROVED, Outcome.PROVED]


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


def test_an_enum_marker_on_a_missing_column_is_diagnosed():
    report = analyze(f"{SCHEMA} SELECT status FROM orders /**ASSERT ENUM(nope IN ('a'))**/")

    assert [d.code for d in report.diagnostics] == [UNKNOWN_ASSERTED_COLUMN]


@pytest.mark.parametrize(
    ("marker", "message"),
    [
        ("/**ASSERT ENUM(status IN ())**/", "lists no values"),
        ("/**ASSERT ENUM(status IN (1, 2))**/", "only string literals"),
        ("/**ASSERT ENUM(status IN ('a', NULL))**/", "NULL is never"),
        ("/**ASSERT ENUM(status IN ('a', 'a'))**/", "more than once"),
        ("/**ASSERT ENUM(status ('a'))**/", "must be written"),
        ("/**ASSERT ENUM(status, channel IN ('a'))**/", "must be written"),
    ],
)
def test_a_malformed_enum_marker_is_diagnosed(marker: str, message: str):
    report = analyze(f"{SCHEMA} SELECT status, channel FROM orders {marker}")

    diagnostic, = report.diagnostics
    assert diagnostic.code == UNATTACHED_MARKER
    assert message in diagnostic.message
