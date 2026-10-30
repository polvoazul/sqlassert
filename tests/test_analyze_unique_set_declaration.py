"""Unique Set Declarations: `/**DECLARE UNIQUE(...)**/` written on a Select
Expression states a trusted Unique Set. It is an assumption, not a proof: it
contributes Knowledge for downstream proofs and never produces an assertion
result of its own.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import UNATTACHED_MARKER, UNKNOWN_ASSERTED_COLUMN

# `t` has no key of its own, so any proof below comes from the declaration.
SCHEMA = "CREATE TABLE t (id INTEGER, v INTEGER);"

ATTACHMENT_SITES = [
    ("root select", f"{SCHEMA} SELECT id, v FROM t /**DECLARE UNIQUE(id)**/ /**ASSERT UNIQUE(id, v)**/"),
    ("view", f"{SCHEMA} CREATE VIEW w AS SELECT id, v FROM t /**DECLARE UNIQUE(id)**/; SELECT id FROM w /**ASSERT UNIQUE(id)**/"),
    ("cte", f"{SCHEMA} WITH c AS (SELECT id, v FROM t /**DECLARE UNIQUE(id)**/) SELECT id FROM c /**ASSERT UNIQUE(id)**/"),
    ("from subquery", f"{SCHEMA} SELECT id FROM (SELECT id, v FROM t /**DECLARE UNIQUE(id)**/) AS s /**ASSERT UNIQUE(id)**/"),
]


@pytest.mark.parametrize(("label", "sql"), ATTACHMENT_SITES)
def test_a_declaration_proves_a_downstream_assertion_at_every_attachment_site(label: str, sql: str):
    report = analyze(sql)

    assert not report.diagnostics, label
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED], label


def test_a_declaration_produces_no_assertion_result():
    report = analyze(f"{SCHEMA} SELECT id FROM t /**DECLARE UNIQUE(id)**/")

    assert report.assertions == ()
    assert report.diagnostics == ()
    assert report.proved


def test_a_declaration_proves_a_unique_join():
    report = analyze(
        f"""
        {SCHEMA}
        CREATE TABLE s (tid INTEGER);
        WITH c AS (SELECT id FROM t /**DECLARE UNIQUE(id)**/)
        SELECT * FROM s /**ASSERT UNIQUE**/ JOIN c ON s.tid = c.id
        """
    )

    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]


def test_without_the_declaration_the_same_assertion_is_unknown():
    report = analyze(f"{SCHEMA} WITH c AS (SELECT id, v FROM t) SELECT id FROM c /**ASSERT UNIQUE(id)**/")

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]


def test_the_explanation_identifies_the_declaration_as_trusted():
    report = analyze(f"{SCHEMA} WITH c AS (SELECT id, v FROM t /**DECLARE UNIQUE(id)**/) SELECT id FROM c /**ASSERT UNIQUE(id)**/")

    evidence = report.assertions[0].evidence
    assert evidence is not None
    assert "Declared UniqueSet" in evidence.explain()


def test_a_declaration_naming_an_unknown_column_is_reported():
    report = analyze(f"{SCHEMA} SELECT id FROM t /**DECLARE UNIQUE(missing)**/")

    assert [d.code for d in report.diagnostics] == [UNKNOWN_ASSERTED_COLUMN]
    assert "declaration" in report.diagnostics[0].message
    assert not report.proved


def test_a_declaration_with_no_columns_is_reported():
    report = analyze(f"{SCHEMA} SELECT id FROM t /**DECLARE UNIQUE()**/")

    assert [d.code for d in report.diagnostics] == [UNATTACHED_MARKER]
