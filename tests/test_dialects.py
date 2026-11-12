"""Dialect semantics that need more than an outcome: the `trust_unenforced_keys`
opt-in, parsing, and the defaults for a dialect not listed. Outcomes per
dialect are in `tests/dialect_*.sqltest`."""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from sqlassert import Outcome, analyze, suggest
from sqlassert.dialects import DialectSemantics, semantics
from sqlassert.sql_parse import is_ambiguous_pipes, sqlassert_dialect

BIGQUERY = """
CREATE TABLE t (id INT64 NOT NULL, PRIMARY KEY (id) NOT ENFORCED);
CREATE TABLE o (id INT64, t_id INT64, FOREIGN KEY (t_id) REFERENCES t (id) NOT ENFORCED);
"""


def test_an_unenforced_key_is_ignored_and_reported_when_not_trusted():
    report = analyze(
        BIGQUERY + "SELECT t_id FROM o /**ASSERT FOREIGN KEY(t_id) REFERENCES t(id)**/", dialect="bigquery", trust_unenforced_keys=False,
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert [warning.code for warning in report.warnings] == ["unenforced-constraint", "unenforced-constraint"]


def test_unenforced_keys_are_trusted_as_declarations_by_default():
    report = analyze(BIGQUERY + "SELECT t_id FROM o /**ASSERT FOREIGN KEY(t_id) REFERENCES t(id)**/", dialect="bigquery")

    assertion, = report.assertions
    assert assertion.outcome is Outcome.PROVED and assertion.evidence is not None
    assert "Declared ReferentialCoverage" in assertion.evidence.explain()
    assert "Declared UniqueSet" in assertion.evidence.explain()
    assert report.warnings == ()


def test_suggest_trusts_unenforced_keys_only_when_asked():
    sql = BIGQUERY + "SELECT o.id FROM o JOIN t ON t.id = o.t_id"

    def markers(trust: bool) -> list[str]:
        return [item.marker for item in suggest(sql, dialect="bigquery", trust_unenforced_keys=trust).suggestions]

    assert "/**ASSERT MANY TO ONE**/" not in markers(False)
    assert "/**ASSERT MANY TO ONE**/" in markers(True)


def test_mysql_pipes_parse_as_a_marked_or():
    select = sqlglot.parse_one("SELECT a || b, a OR b FROM t", read=sqlassert_dialect("mysql"))

    pipes, disjunction = select.expressions
    assert isinstance(pipes, exp.Or) and is_ambiguous_pipes(pipes)
    assert isinstance(disjunction, exp.Or) and not is_ambiguous_pipes(disjunction)
    assert pipes == disjunction  # The same tree; only the marking differs.


def test_pipes_are_concatenation_elsewhere():
    select = sqlglot.parse_one("SELECT a || b FROM t", read=sqlassert_dialect("postgres"))

    assert isinstance(select.expressions[0], exp.DPipe)


def test_a_dialect_not_listed_gets_cautious_semantics_and_enforced_keys():
    assert semantics("tsql") == DialectSemantics()
    assert semantics("spark2") == DialectSemantics()  # Older versions are not modelled (ADR 0006).
    assert not DialectSemantics().checked_arithmetic and not DialectSemantics().text_is_binary
    assert DialectSemantics().pipes is None and DialectSemantics().unique_keys == "enforced"
