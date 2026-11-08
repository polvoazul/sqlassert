"""Exhaustive simple CASE: `/**ASSERT EXHAUSTIVE**/ CASE x WHEN ... END`.

The marker sits immediately before a simple `CASE` and asks for a proof that
every member of the operand's Enumerated Domain has an explicit `WHEN` arm.
`ELSE` never substitutes for an arm, and NULL is outside every domain. A CASE
whose `ELSE` is unreachable -- exhaustive over a non-null operand -- has the
domain of its `THEN` arms alone.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import UNANALYZED_ASSERTION, UNATTACHED_MARKER, UNRECOGNIZED_MARKER

SCHEMA = """
CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR, kind VARCHAR NOT NULL, qty INTEGER);
"""

# A source of statuses the author vouches for.
DECLARED = "WITH o AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('new', 'paid'))**/) "

# The same source, with every status present.
DECLARED_NOT_NULL = (
    "WITH o AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('new', 'paid'))**/ /**DECLARE NOT NULL(status)**/) "
)


def report(sql: str):
    return analyze(f"{SCHEMA} {sql}")


def outcomes(sql: str) -> list[Outcome]:
    result = report(sql)
    assert not result.diagnostics, result.diagnostics
    return [assertion.outcome for assertion in result.assertions]


PROVED = [
    ("every member has an arm", DECLARED + "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END AS n FROM o"),
    ("arms beyond the domain", DECLARED + "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'paid' THEN 1 WHEN 'void' THEN 0 WHEN 'new' THEN 2 END AS n FROM o"),
    ("with ELSE", DECLARED + "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 ELSE 0 END AS n FROM o"),
    ("narrowed by WHERE", "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 END AS n FROM orders WHERE status = 'new'"),
    ("numeric domain", "SELECT /**ASSERT EXHAUSTIVE**/ CASE qty WHEN 1 THEN 'one' WHEN 2 THEN 'two' END AS n FROM orders WHERE qty IN (1, 2)"),
    ("in WHERE", DECLARED + "SELECT id FROM o WHERE /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END = 1"),
    ("in a join's ON", DECLARED + "SELECT o.id FROM o JOIN orders p ON /**ASSERT EXHAUSTIVE**/ CASE o.status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END = p.qty"),
    ("in a view", "CREATE VIEW v AS " + DECLARED + "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END AS n FROM o; SELECT n FROM v"),
    ("grouping key operand", DECLARED + "SELECT status, /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END AS n FROM o GROUP BY status"),
]


@pytest.mark.parametrize(("label", "sql"), PROVED, ids=[label for label, _ in PROVED])
def test_exhaustive_case_is_proved(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED]


NOT_PROVED = [
    ("a member has no arm", DECLARED + "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 END AS n FROM o"),
    ("ELSE does not substitute", DECLARED + "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 ELSE 2 END AS n FROM o"),
    ("no known domain", "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 END AS n FROM orders"),
    ("a number never covers a string", "SELECT /**ASSERT EXHAUSTIVE**/ CASE qty WHEN '1' THEN 1 END AS n FROM orders WHERE qty = 1"),
    ("an integer never covers a decimal", "SELECT /**ASSERT EXHAUSTIVE**/ CASE qty WHEN 1 THEN 1 END AS n FROM orders WHERE qty = 1.0"),
]


@pytest.mark.parametrize(("label", "sql"), NOT_PROVED, ids=[label for label, _ in NOT_PROVED])
def test_exhaustive_case_is_not_guessed(label: str, sql: str):
    assert outcomes(sql) == [Outcome.UNKNOWN]


def test_an_unknown_report_names_every_member_without_an_arm():
    result = report(
        "WITH o AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('new', 'paid', 'void'))**/) "
        "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 ELSE 0 END AS n FROM o"
    )
    assertion, = result.assertions
    assert assertion.evidence is not None
    assert "status may be 'paid', 'void', which no WHEN arm lists" in assertion.evidence.explain()


def test_null_is_left_to_not_null():
    # NULL reaches ELSE, yet the CASE is exhaustive over its domain.
    sql = DECLARED + (
        "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END AS n, status FROM o "
        "/**ASSERT NOT NULL(status)**/"
    )
    assert outcomes(sql) == [Outcome.PROVED, Outcome.UNKNOWN]


def test_the_assertion_reports_the_case_and_its_line():
    result = report(DECLARED + "SELECT id,\n /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END AS n FROM o")
    assertion, = result.assertions
    assert assertion.origin.line == 4
    assert assertion.origin.detail.startswith("CASE status")


# Output domains -----------------------------------------------------------

DOMAIN_PROVED = [
    ("THEN literals and ELSE", "SELECT CASE status WHEN 'new' THEN 1 ELSE 0 END AS n FROM orders /**ASSERT ENUM(n IN (0, 1))**/"),
    ("no ELSE is NULL", "SELECT CASE status WHEN 'new' THEN 1 WHEN 'old' THEN 2 END AS n FROM orders /**ASSERT ENUM(n IN (1, 2))**/"),
    (
        "unreachable ELSE",
        DECLARED_NOT_NULL + "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 'a' WHEN 'paid' THEN 'b' ELSE 'z' END AS n FROM o "
        "/**ASSERT ENUM(n IN ('a', 'b'))**/",
    ),
    (
        "unreachable ELSE needs no marker",
        DECLARED_NOT_NULL + "SELECT CASE status WHEN 'new' THEN 'a' WHEN 'paid' THEN 'b' ELSE 'z' END AS n FROM o "
        "/**ASSERT ENUM(n IN ('a', 'b'))**/",
    ),
]


@pytest.mark.parametrize(("label", "sql"), DOMAIN_PROVED, ids=[label for label, _ in DOMAIN_PROVED])
def test_a_case_contributes_its_output_domain(label: str, sql: str):
    assert set(outcomes(sql)) == {Outcome.PROVED}


DOMAIN_NOT_PROVED = [
    ("ELSE is reachable on NULL", DECLARED + "SELECT CASE status WHEN 'new' THEN 'a' WHEN 'paid' THEN 'b' ELSE 'z' END AS n FROM o /**ASSERT ENUM(n IN ('a', 'b'))**/"),
    ("ELSE is reachable on a missing member", DECLARED_NOT_NULL + "SELECT CASE status WHEN 'new' THEN 'a' ELSE 'z' END AS n FROM o /**ASSERT ENUM(n IN ('a'))**/"),
    ("a THEN arm is not enumerated", "SELECT CASE status WHEN 'new' THEN kind END AS n FROM orders /**ASSERT ENUM(n IN ('a'))**/"),
    ("an operand-dependent ELSE", "SELECT CASE status WHEN 'new' THEN 'a' ELSE status END AS n FROM orders /**ASSERT ENUM(n IN ('a'))**/"),
]


@pytest.mark.parametrize(("label", "sql"), DOMAIN_NOT_PROVED, ids=[label for label, _ in DOMAIN_NOT_PROVED])
def test_a_case_output_domain_is_not_guessed(label: str, sql: str):
    assert outcomes(sql) == [Outcome.UNKNOWN]


def test_a_non_null_operand_without_else_has_the_then_domain():
    # `kind` is NOT NULL, but has no known domain: ELSE (NULL) adds nothing anyway.
    sql = "SELECT CASE kind WHEN 'a' THEN 1 END AS n FROM orders /**ASSERT ENUM(n IN (1))**/"
    assert outcomes(sql) == [Outcome.PROVED]


# Diagnostics ---------------------------------------------------------------

MALFORMED = [
    ("searched CASE", "SELECT /**ASSERT EXHAUSTIVE**/ CASE WHEN status = 'new' THEN 1 END AS n FROM orders", "searched CASE"),
    ("not a CASE", "SELECT /**ASSERT EXHAUSTIVE**/ status FROM orders", "is not followed by CASE"),
    ("duplicate arm", "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'new' THEN 2 END AS n FROM orders", "'new' more than once"),
    ("column WHEN value", "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN kind THEN 1 END AS n FROM orders", "kind, which is not a string, numeric, or boolean literal"),
    ("NULL WHEN value", "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN NULL THEN 1 END AS n FROM orders", "NULL"),
]


@pytest.mark.parametrize(("label", "sql", "message"), MALFORMED, ids=[label for label, _, _ in MALFORMED])
def test_a_malformed_exhaustive_case_is_diagnosed(label: str, sql: str, message: str):
    result = report(sql)
    diagnostic, = result.diagnostics
    assert diagnostic.code == UNATTACHED_MARKER
    assert message in diagnostic.message
    assert not result.proved


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT status FROM orders /**ASSERT EXHAUSTIVE**/",
        "SELECT * FROM orders a /**ASSERT EXHAUSTIVE**/ JOIN orders b ON a.id = b.id",
        "SELECT * FROM orders WHERE id = 1 /**ASSERT EXHAUSTIVE**/ ORDER BY 1",
    ],
    ids=["after a select", "before a join", "before ORDER BY"],
)
def test_an_exhaustive_marker_at_an_invalid_attachment_site_is_diagnosed(sql: str):
    result = report(sql)
    diagnostic, = result.diagnostics
    assert diagnostic.code == UNATTACHED_MARKER
    assert "must sit immediately before a simple CASE" in diagnostic.message


def test_declare_exhaustive_is_diagnosed():
    result = report(DECLARED + "SELECT /**DECLARE EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 END AS n FROM o")
    diagnostic, = result.diagnostics
    assert diagnostic.code == UNRECOGNIZED_MARKER
    assert "only be asserted" in diagnostic.message


def test_an_exhaustive_case_the_analysis_does_not_reach_is_diagnosed():
    result = report("SELECT max(/**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 'a' END) OVER () AS n FROM orders")
    diagnostic, = result.diagnostics
    assert diagnostic.code == UNANALYZED_ASSERTION
    assert "exhaustive" in diagnostic.message


def test_an_exhaustive_case_inside_an_aggregate_is_analyzed():
    # Evaluated on every input row of the group, like any other select item.
    sql = DECLARED_NOT_NULL + "SELECT max(/**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 'a' WHEN 'paid' THEN 'b' END) AS n FROM o"
    assert outcomes(sql) == [Outcome.PROVED]


def test_an_exhaustive_case_inside_a_function_is_analyzed():
    result = report("SELECT upper(/**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 'a' END) AS n FROM orders")
    assert result.diagnostics == ()
    assert len(result.assertions) == 1
