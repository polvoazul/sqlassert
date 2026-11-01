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
CREATE TABLE items (id INTEGER PRIMARY KEY, qty INTEGER, size INTEGER, ratio FLOAT, flag BOOLEAN, tag VARCHAR);
CREATE TABLE people (name VARCHAR COLLATE NOCASE);
"""

# A source of statuses the author vouches for.
DECLARED = "WITH o AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('new', 'paid'))**/) "

# A source of quantities and flags the author vouches for.
TYPED = "WITH i AS (SELECT * FROM items /**DECLARE ENUM(qty IN (1, 2, 3))**/ /**DECLARE ENUM(flag IN (TRUE))**/) "


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


TYPED_PROVED = [
    ("numeric literal", "SELECT 1 AS n FROM orders /**ASSERT ENUM(n IN (1, 2))**/"),
    ("negative numeric literal", "SELECT -1 AS n FROM orders /**ASSERT ENUM(n IN (-1, 0))**/"),
    ("decimal literal", "SELECT 2.5 AS n FROM orders /**ASSERT ENUM(n IN (2.5))**/"),
    ("boolean literal", "SELECT TRUE AS f FROM orders /**ASSERT ENUM(f IN (TRUE, FALSE))**/"),
    ("integer spellings are one value", "SELECT 007 AS n FROM orders /**ASSERT ENUM(n IN (7))**/"),
    ("coalesce of typed literals", "SELECT coalesce(NULL, 3, 4) AS n FROM orders /**ASSERT ENUM(n IN (3, 4))**/"),
    ("where numeric equality", "SELECT qty FROM items WHERE qty = 3 /**ASSERT ENUM(qty IN (3))**/"),
    ("where numeric in list", "SELECT qty FROM items WHERE qty IN (1, 2) /**ASSERT ENUM(qty IN (1, 2, 3))**/"),
    ("where boolean equality", "SELECT flag FROM items WHERE flag = FALSE /**ASSERT ENUM(flag IN (FALSE))**/"),
    ("declared numeric", TYPED + "SELECT qty FROM i /**ASSERT ENUM(qty IN (3, 2, 1))**/"),
    ("declared boolean", TYPED + "SELECT flag FROM i /**ASSERT ENUM(flag IN (TRUE))**/"),
    ("declared and narrowed", TYPED + "SELECT qty FROM i WHERE qty IN (2, 5) /**ASSERT ENUM(qty IN (2))**/"),
    ("across a numeric equality", TYPED + "SELECT size FROM i WHERE size = qty /**ASSERT ENUM(size IN (1, 2, 3))**/"),
    ("view", "CREATE VIEW v AS SELECT qty FROM items /**DECLARE ENUM(qty IN (1, 2))**/; SELECT qty FROM v /**ASSERT ENUM(qty IN (1, 2))**/"),
    (
        "mixed types",
        "WITH m AS (SELECT * FROM items /**DECLARE ENUM(tag IN ('a', 1, TRUE))**/) SELECT tag FROM m /**ASSERT ENUM(tag IN (TRUE, 1, 'a'))**/",
    ),
    (
        "a cast-based equality does not change binary text comparison",
        "WITH a AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('01'))**/ /**DECLARE ENUM(channel IN ('1'))**/), "
        "j AS (SELECT a.status, a.channel FROM a JOIN items ON a.status = items.qty AND items.qty = a.channel) "
        "SELECT status FROM j WHERE status = channel /**ASSERT ENUM(status IN ('zz'))**/",
    ),
]

TYPED_UNKNOWN = [
    ("numeric one is not string one", "SELECT 1 AS n FROM orders /**ASSERT ENUM(n IN ('1'))**/"),
    ("string one is not numeric one", "SELECT '1' AS n FROM orders /**ASSERT ENUM(n IN (1))**/"),
    ("boolean true is not numeric one", "SELECT TRUE AS f FROM orders /**ASSERT ENUM(f IN (1))**/"),
    ("decimal scale is part of the literal", "SELECT 1.0 AS n FROM orders /**ASSERT ENUM(n IN (1))**/"),
    ("exponent literal is not the integer", "SELECT 1e0 AS n FROM orders /**ASSERT ENUM(n IN (1))**/"),
    ("declared, subset asserted", TYPED + "SELECT qty FROM i /**ASSERT ENUM(qty IN (1, 2))**/"),
    ("table column", "SELECT qty FROM items /**ASSERT ENUM(qty IN (1))**/"),
    # Two literals that differ can both equal one value, so their domains
    # meeting is not an empty intersection.
    (
        "float rounding",
        "WITH r AS (SELECT * FROM items /**DECLARE ENUM(ratio IN (0.1))**/) SELECT ratio FROM r WHERE ratio = 0.100000001 /**ASSERT ENUM(ratio IN (5))**/",
    ),
    (
        "text cast to a numeric literal's scale",
        "WITH t AS (SELECT * FROM items /**DECLARE ENUM(tag IN (1))**/) SELECT tag FROM t WHERE tag = 1.4 /**ASSERT ENUM(tag IN (5))**/",
    ),
    (
        "a string and a number may meet",
        "WITH t AS (SELECT * FROM items /**DECLARE ENUM(qty IN ('1'))**/) SELECT qty FROM t WHERE qty = 1 /**ASSERT ENUM(qty IN (5))**/",
    ),
    # Distinct strings are one value only for text compared by identity.
    (
        "two strings may equal one integer",
        "WITH t AS (SELECT * FROM items /**DECLARE ENUM(qty IN ('01'))**/) SELECT qty FROM t WHERE qty = '1' /**ASSERT ENUM(qty IN ('2'))**/",
    ),
    (
        "two strings may equal under a collation",
        "WITH p AS (SELECT * FROM people /**DECLARE ENUM(name IN ('a'))**/) SELECT name FROM p WHERE name = 'A' /**ASSERT ENUM(name IN ('b'))**/",
    ),
]


@pytest.mark.parametrize(("label", "sql"), TYPED_PROVED)
def test_typed_enum_is_proved(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


@pytest.mark.parametrize(("label", "sql"), TYPED_UNKNOWN)
def test_typed_enum_is_not_guessed(label: str, sql: str):
    assert outcomes(sql) == [Outcome.UNKNOWN], label


def test_strings_meet_conservatively_on_a_column_of_unknown_type():
    # Without a CREATE, the column's type and collation are unknown.
    sql = "WITH t AS (SELECT * FROM ext /**DECLARE ENUM(v IN ('01'))**/) SELECT v FROM t WHERE v = '1' /**ASSERT ENUM(v IN ('2'))**/"

    assert [assertion.outcome for assertion in analyze(sql).assertions] == [Outcome.UNKNOWN]


def test_a_cast_based_equality_does_not_transfer_a_domain():
    sql = "WITH t AS (SELECT qty, tag FROM items /**DECLARE ENUM(qty IN ('01'))**/) SELECT tag FROM t WHERE qty = tag /**ASSERT ENUM(tag IN ('01'))**/"
    assert outcomes(sql) == [Outcome.UNKNOWN]


def test_distinct_numeric_literals_do_not_meet():
    # Integers 2 and 3 never equal one value, so the declared domain narrows.
    sql = TYPED + "SELECT qty FROM i WHERE qty IN (3, 4) /**ASSERT ENUM(qty IN (3))**/"

    assert outcomes(sql) == [Outcome.PROVED]


def test_an_unknown_report_lists_mixed_types_in_a_fixed_order():
    sql = (
        "WITH m AS (SELECT * FROM items /**DECLARE ENUM(tag IN (10, 'b', 2, TRUE, 'a', FALSE, -1.5))**/) "
        "SELECT tag FROM m /**ASSERT ENUM(tag IN ('zzz'))**/"
    )
    report = analyze(f"{SCHEMA} {sql}")

    assert "tag may be FALSE, TRUE, -1.5, 2, 10, 'a', 'b', which the assertion does not cover" in explanation(report.assertions[0])


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
        ("/**ASSERT ENUM(status IN ('a', NULL))**/", "NULL is never"),
        ("/**ASSERT ENUM(status IN ('a', 'a'))**/", "lists 'a' more than once"),
        ("/**ASSERT ENUM(status IN (1, 2, 1))**/", "lists 1 more than once"),
        ("/**ASSERT ENUM(status IN (7, 007))**/", "lists 7 more than once"),
        ("/**ASSERT ENUM(status IN (TRUE, true))**/", "lists TRUE more than once"),
        ("/**ASSERT ENUM(status IN (DATE '2020-01-01'))**/", "lists CAST('2020-01-01' AS DATE), which is not a string, numeric, or boolean literal"),
        ("/**ASSERT ENUM(status IN (1 + 1))**/", "lists 1 + 1, which is not"),
        ("/**ASSERT ENUM(status IN (channel))**/", "lists channel, which is not"),
        ("/**ASSERT ENUM(status IN (-'a'))**/", "which is not"),
        ("/**ASSERT ENUM(status ('a'))**/", "must be written"),
        ("/**ASSERT ENUM(status, channel IN ('a'))**/", "must be written"),
    ],
)
def test_a_malformed_enum_marker_is_diagnosed(marker: str, message: str):
    report = analyze(f"{SCHEMA} SELECT status, channel FROM orders {marker}")

    diagnostic, = report.diagnostics
    assert diagnostic.code == UNATTACHED_MARKER
    assert message in diagnostic.message


def test_values_of_different_types_spelled_alike_are_not_duplicates():
    report = analyze(f"{SCHEMA} SELECT status FROM orders /**ASSERT ENUM(status IN (1, '1', TRUE, 'TRUE', 1.0))**/")

    assert report.diagnostics == ()
