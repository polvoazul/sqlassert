"""Regressions for soundness and integration findings from the repository review."""

from importlib import import_module

import duckdb
import pytest

from sqlassert import Outcome, analyze, analyze_database, suggest
from sqlassert.suggest import SuggestType
from sqlassert.discovery.duckdb import DuckDBKnowledgeFetcher


@pytest.mark.parametrize("query", [
    "SELECT * REPLACE (0 AS a) FROM t",
    "SELECT * EXCLUDE (a) FROM t",
    "SELECT * FROM (SELECT b AS a, a AS a FROM t) AS s",
])
def test_a_marker_cannot_borrow_the_key_of_a_different_output(query):
    report = analyze("CREATE TABLE t(a INT PRIMARY KEY, b INT); " + query + " /**ASSERT UNIQUE(a)**/")
    assert not report.assertions
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["unresolved-asserted-column"]


@pytest.mark.parametrize(("dialect", "sql", "code"), [
    ("postgres", 'CREATE TABLE t(a INT PRIMARY KEY); SELECT a FROM "T" /**ASSERT UNIQUE(a)**/', "unsupported-construct"),
    ("postgres", 'CREATE TABLE t("A" INT, a INT PRIMARY KEY); SELECT * FROM t /**ASSERT UNIQUE("A")**/', "unattached-marker"),
    ("duckdb", 'CREATE TABLE "s.t"(a INT PRIMARY KEY); SELECT a FROM s.t /**ASSERT UNIQUE(a)**/', "unsupported-construct"),
])
def test_unsupported_identifiers_do_not_borrow_facts(dialect, sql, code):
    report = analyze(sql, dialect=dialect)
    assert not report.assertions
    assert [diagnostic.code for diagnostic in report.diagnostics] == [code]


def test_dollar_quoted_text_does_not_count_as_a_marker():
    report = analyze("SELECT $tag$/**ASSERT UNIQUE**/ /**typo**/$tag$ AS s", dialect="postgres")
    assert not report.diagnostics


def test_catalog_json_type_does_not_withdraw_an_offline_proof():
    sql = (
        "WITH c AS (SELECT payload FROM t /**DECLARE NOT NULL(payload)**/) "
        "SELECT CAST(payload AS INTEGER) AS result FROM c /**ASSERT NOT NULL(result)**/"
    )
    with duckdb.connect() as con:
        con.execute("CREATE TABLE t(payload JSON NOT NULL); INSERT INTO t VALUES (JSON 'null')")
        assert con.execute(sql).fetchall() == [(None,)]
        for report in (analyze(sql), analyze_database(sql, DuckDBKnowledgeFetcher(con))):
            assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_catalog_knowledge_attaches_to_both_spellings():
    sql = (
        "CREATE VIEW v AS SELECT id FROM t /**ASSERT UNIQUE(id)**/; "
        "SELECT id FROM main.t /**ASSERT UNIQUE(id)**/"
    )
    with duckdb.connect() as con:
        con.execute("CREATE TABLE t(id INT PRIMARY KEY)")
        report = analyze_database(sql, DuckDBKnowledgeFetcher(con))
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED, Outcome.PROVED]


def test_failed_suggestion_setup_releases_the_analysis_session(monkeypatch):
    module = import_module("sqlassert.suggest")

    def fail(*args, **kwargs):
        raise ValueError("suggestion setup failed")

    monkeypatch.setattr(module, "rewrites", fail)
    with pytest.raises(ValueError, match="suggestion setup failed") as failure:
        suggest("SELECT 1")
    assert failure.traceback
    assert not analyze("SELECT 1").diagnostics


def test_distinct_to_all_requires_preserved_types():
    ddl = "CREATE TABLE l(k DOUBLE NOT NULL); CREATE TABLE r(k BIGINT PRIMARY KEY); "
    for operator in ("EXCEPT", "INTERSECT"):
        assert not suggest(ddl + f"SELECT k FROM r {operator} SELECT k FROM l").rewrites


def test_approximate_comparison_does_not_make_integer_values_equal():
    sql = (
        "CREATE TABLE t(a BIGINT, b BIGINT); "
        "SELECT a,b FROM t WHERE a=9.007199254740992e15 AND b=9.007199254740992e15 AND a=b"
    )
    assert not suggest(sql).rewrites


@pytest.mark.parametrize("expression", ["sum(b)+1", "count(*)+2147483647"])
def test_a_single_row_aggregate_narrowed_inside_an_expression_is_caveated(expression):
    # b + 1 overflows an INT where sum(b) + 1 does not: a rewrite with caveat that says so.
    ddl = "CREATE TABLE t(a INT PRIMARY KEY,b INT); "
    rewrite, = suggest(ddl + f"SELECT a,{expression} AS s FROM t GROUP BY a").rewrites
    assert rewrite.suggest_type is SuggestType.REWRITE_WITH_CAVEAT
    assert "'s' runs its arithmetic in " in rewrite.message
    assert "may overflow where the aggregate's did not" in rewrite.message


def test_an_average_of_an_integer_is_not_narrowed_inside_an_expression():
    # b / 2 truncates in PostgreSQL where avg(b) / 2 does not.
    ddl = "CREATE TABLE t(a INT PRIMARY KEY,b INT); "
    assert not suggest(ddl + "SELECT a,avg(b)/2 AS s FROM t GROUP BY a").rewrites


def test_except_all_with_a_duplicate_can_return_rows():
    sql = "CREATE TABLE t(a INT NOT NULL); SELECT a FROM t EXCEPT ALL SELECT DISTINCT a FROM t"
    assert not suggest(sql).hazards
