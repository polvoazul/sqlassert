"""Exhaustive simple CASE: `/**ASSERT EXHAUSTIVE**/ CASE x WHEN ... END`.

The marker sits immediately before a simple `CASE` and asks for a proof that
every member of the operand's Enumerated Domain has an explicit `WHEN` arm.
`ELSE` never substitutes for an arm, and NULL is outside every domain. A CASE
whose `ELSE` is unreachable -- exhaustive over a non-null operand -- has the
domain of its `THEN` arms alone.
"""

from __future__ import annotations

import pytest

from sqlassert import analyze
from sqlassert.diagnostics import UNANALYZED_ASSERTION, UNATTACHED_MARKER, UNRECOGNIZED_MARKER

SCHEMA = """
CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR, kind VARCHAR NOT NULL, qty INTEGER);
"""

# A source of statuses the author vouches for.
DECLARED = "WITH o AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('new', 'paid'))**/) "

# The same source, with every status present.


def report(sql: str):
    return analyze(f"{SCHEMA} {sql}")


def test_an_unknown_report_names_every_member_without_an_arm():
    result = report(
        "WITH o AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('new', 'paid', 'void'))**/) "
        "SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 ELSE 0 END AS n FROM o"
    )
    assertion, = result.assertions
    assert assertion.evidence is not None
    assert "status may be 'paid', 'void', which no WHEN arm lists" in assertion.evidence.explain()


def test_the_assertion_reports_the_case_and_its_line():
    result = report(DECLARED + "SELECT id,\n /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END AS n FROM o")
    assertion, = result.assertions
    assert assertion.origin.line == 4
    assert assertion.origin.detail.startswith("CASE status")


# Output domains -----------------------------------------------------------


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


def test_an_exhaustive_case_inside_a_function_is_analyzed():
    result = report("SELECT upper(/**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 'a' END) AS n FROM orders")
    assert result.diagnostics == ()
    assert len(result.assertions) == 1
