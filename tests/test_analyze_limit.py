"""Limits retain input facts but may remove rows needed by referenced coverage."""

import pytest

from sqlassert import Outcome, analyze


@pytest.mark.parametrize("tail", ["LIMIT 0", "LIMIT 1", "FETCH FIRST 1 ROWS ONLY"])
def test_at_most_one_row_is_unique_without_an_input_key(tail: str):
    report = analyze(f"CREATE TABLE t (a INTEGER); SELECT a FROM t {tail} /**ASSERT UNIQUE(a)**/")
    assert report.proved


@pytest.mark.parametrize("tail", ["LIMIT 2", "LIMIT 1 PERCENT", "ORDER BY a FETCH FIRST 1 ROWS WITH TIES"])
def test_a_limit_that_may_keep_duplicate_rows_does_not_make_a_key(tail: str):
    report = analyze(f"CREATE TABLE t (a INTEGER); SELECT a FROM t {tail} /**ASSERT UNIQUE(a)**/")
    assert not report.diagnostics
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_limit_preserves_keys_non_nullness_and_domains():
    report = analyze("""
        CREATE TABLE t (id INTEGER PRIMARY KEY, status VARCHAR);
        WITH limited AS (SELECT id, status FROM t WHERE status = 'paid' LIMIT 2)
        SELECT id, status FROM limited
        /**ASSERT NOT NULL UNIQUE(id)**/ /**ASSERT ENUM(status IN ('paid'))**/
    """)
    assert report.proved


def test_a_limit_does_not_make_a_nullable_column_non_null():
    report = analyze("CREATE TABLE t (a INTEGER); SELECT a FROM t LIMIT 1 /**ASSERT NOT NULL(a)**/")
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
