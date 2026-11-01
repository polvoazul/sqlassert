"""Exercise oracle ownership and reporting through real pytest sessions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def oracle_pytester(pytester, monkeypatch):
    tests = Path(__file__).parent
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tests), str(tests.parent), os.environ.get("PYTHONPATH", "")]))
    pytester.makeconftest((tests / "conftest.py").read_text())
    return pytester


@pytest.mark.parametrize("workers", [0, 2])
def test_oracle_records_setup_call_and_teardown_per_test_and_combines_worker_reports(oracle_pytester, workers):
    oracle_pytester.makepyfile("""
        import pytest
        from sqlassert import analyze

        SQL = "CREATE TABLE t (id INTEGER PRIMARY KEY); SELECT id FROM t /**ASSERT UNIQUE(id)**/"
        analyze("SELECT 2 AS id /**ASSERT UNIQUE(id)**/")

        @pytest.fixture
        def analyzed_fixture():
            analyze(SQL)
            yield
            analyze(SQL)

        @pytest.mark.parametrize("value", [1, 2])
        def test_same_program(value, analyzed_fixture):
            analyze(SQL)
            analyze(SQL)

        def test_unsupported_database():
            analyze("CREATE TABLE t (id INTEGER); SELECT id FROM t WHERE no_such_function(id) /**DECLARE UNIQUE(id)**/ /**ASSERT UNIQUE(id)**/")
    """)

    result = oracle_pytester.runpytest_subprocess("--soundness-oracle", "-n", str(workers), "-q")

    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines([
        "*3 programs, 2 PROVED assertions judged, 0 counterexamples, 0 known findings, 1 skipped;*",
        "*skipped *::test_unsupported_database: program cannot run on DuckDB:*",
    ])


def test_oracle_failure_belongs_to_selected_test_and_runs_after_monkeypatch_cleanup(oracle_pytester):
    with (oracle_pytester.path / "conftest.py").open("a") as file:
        file.write('''
from dataclasses import replace
import sqlassert
import sqlassert.main
from sqlassert import Outcome

original_analyze = sqlassert.main.analyze

def unsound_analyze(sql, *, dialect="duckdb"):
    report = original_analyze(sql, dialect=dialect)
    return replace(report, assertions=tuple(replace(a, outcome=Outcome.PROVED) for a in report.assertions))

sqlassert.main.analyze = unsound_analyze
sqlassert.analyze = unsound_analyze
''')
    oracle_pytester.makepyfile("""
        import soundness_oracle
        from sqlassert import analyze

        def test_target(monkeypatch):
            monkeypatch.setattr(soundness_oracle, "judge", lambda *args, **kwargs: soundness_oracle.Judgement())
            analyze("CREATE TABLE t (id INTEGER); SELECT id FROM t /**ASSERT UNIQUE(id)**/")

        def test_other():
            raise AssertionError("must not run")
    """)

    disabled = oracle_pytester.runpytest_subprocess("-k", "test_target", "-n", "0", "-q")
    disabled.assert_outcomes(passed=1, deselected=1)

    enabled = oracle_pytester.runpytest_subprocess("--soundness-oracle", "-k", "test_target", "-n", "2", "-q")
    enabled.assert_outcomes(passed=1, errors=1)
    enabled.stdout.fnmatch_lines([
        "*ERROR at teardown of test_target*",
        "*PROVED assertions with counterexamples:*",
        "*1 programs, 1 PROVED assertions judged, 1 counterexamples, 0 known findings, 0 skipped;*",
    ])
