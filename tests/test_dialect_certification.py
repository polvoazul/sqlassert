"""Every dialect fact has a provenance (`docs/dialect-provenance.json`), and the
in-process engines still pass their probes (`scripts/certify_dialects.py`)."""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import duckdb
import pytest

from sqlassert.dialects import MODELLED_DIALECTS

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "certify_dialects.py"

# The dialects whose engine runs locally (in process or in docker): each of
# their facts is certified on an engine, never only documented.
LOCAL_ENGINES = {"duckdb", "sqlite", "postgres", "mysql", "clickhouse", "trino"}
# Facts of a local engine that no local engine can run: MySQL Cluster's NDB.
DOCUMENTED_ONLY = {("mysql", "foreign_key_engines:ndb"), ("mysql", "foreign_key_engines:ndbcluster")}


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("certify_dialects", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


certify = _script()


@pytest.mark.parametrize("dialect", sorted(MODELLED_DIALECTS))
def test_every_fact_has_a_provenance(dialect: str):
    provenance = certify.load_provenance()[dialect]
    names = {fact.name for fact in certify.facts(dialect)}

    assert set(provenance) == names
    for name, entry in provenance.items():
        probed = bool(entry.keys() & {"engine", "undefined"})
        if probed:
            assert entry.keys() <= {"engine", "undefined"} and all(entry.values()), name
        else:
            assert entry.keys() == {"doc", "read"} and entry["doc"].startswith("https://"), name
        local = dialect in LOCAL_ENGINES and (dialect, name) not in DOCUMENTED_ONLY
        assert probed == local, name


@pytest.mark.parametrize(("dialect", "connect"), [("duckdb", lambda: duckdb.connect()), ("sqlite", lambda: sqlite3.connect(":memory:"))])
def test_in_process_engines_pass_every_probe(dialect, connect):
    results = certify.run(connect().cursor(), dialect, certify.facts(dialect))

    assert [(result.fact.name, result.detail) for result in results if result.outcome not in ("PASS", "VACUOUS")] == []


def test_a_probe_finds_a_wrong_fact():
    # SQLite's `1 / 0` is NULL, so `/` is not total there.
    strong = certify.replace(certify.semantics("sqlite"), checked_division=True)
    fact, = (fact for fact in certify.facts("sqlite", strong) if fact.name == "total(/)")

    result, = certify.run(sqlite3.connect(":memory:").cursor(), "sqlite", [fact])

    assert result.outcome == "FAIL"


def test_a_vacuous_fact_is_undefined_on_that_version_not_certified():
    fact = certify.Fact("total(//)", "checked_division=False", ())
    provenance = {"trino": {"total(//)": {"engine": ["trino 484"]}}}

    certify.record(provenance, "trino", "trino 484", [certify.Result(fact, "VACUOUS")])
    assert provenance == {"trino": {"total(//)": {"undefined": ["trino 484"]}}}
    certify.record(provenance, "trino", "trino 490", [certify.Result(fact, "PASS")])
    assert provenance == {"trino": {"total(//)": {"engine": ["trino 490"], "undefined": ["trino 484"]}}}


def test_recording_only_removes_a_failing_engine_and_never_adds_a_fact():
    fact = certify.Fact("total(+)", "checked_arithmetic=False", ())
    provenance = {"duckdb": {"total(+)": {"engine": ["duckdb 1.0", "duckdb 1.5"]}, "gone": {"engine": ["duckdb 1.0"]}}}

    changes = certify.record(provenance, "duckdb", "duckdb 1.5", [certify.Result(fact, "FAIL")])

    assert provenance == {"duckdb": {"total(+)": {"engine": ["duckdb 1.0"]}}}
    assert changes == ["dropped gone: no longer a fact", "total(+): no longer certified on duckdb 1.5"]
    certify.record(provenance, "duckdb", "duckdb 1.0", [certify.Result(fact, "FAIL")])
    assert provenance == {"duckdb": {}}
    certify.record(provenance, "duckdb", "duckdb 1.6", [certify.Result(fact, "ERROR")])
    assert provenance == {"duckdb": {}}


def test_recording_a_pass_replaces_a_doc_with_the_engine():
    fact = certify.Fact("strict(+)", "", ())
    provenance = {"oracle": {"strict(+)": {"doc": "https://docs.oracle.com/x", "read": "26"}}}

    certify.record(provenance, "oracle", "oracle 23.5", [certify.Result(fact, "PASS")])

    assert provenance == {"oracle": {"strict(+)": {"engine": ["oracle 23.5"]}}}


def test_a_function_is_probed_as_written_not_as_sqlglot_rewrites_it():
    # SQLGlot writes DuckDB's concat(a, b) as a || b, which is NULL for a NULL operand.
    fact, = (fact for fact in certify.facts("duckdb") if fact.name == "total(concat)")

    assert all("concat(" in check.query.lower() for check in fact.checks)


def test_an_error_passes_only_when_the_probe_declares_its_message():
    cursor = sqlite3.connect(":memory:").cursor()
    boom = "SELECT abs(-9223372036854775807 - 1)"  # SQLite: "integer overflow"
    syntax = "SELEKT 1"

    declared = certify.Check(boom, "error or true", accepted_error=r"(?i)overflow")
    assert certify._check(cursor, declared)[0] == "PASS"
    assert certify._check(cursor, certify.Check(boom, "error or true", accepted_error=r"(?i)out of range"))[0] == "FAIL"
    assert certify._check(cursor, certify.Check(boom, "error or true"))[0] == "FAIL"
    # A syntax error is no overflow, whatever else the probe accepts.
    outcome, message = certify._check(cursor, certify.Check(syntax, "error or true", accepted_error=r"(?i)overflow"))
    assert outcome == "FAIL" and "syntax error" in message


def test_a_returned_value_passes_only_when_exact():
    cursor = sqlite3.connect(":memory:").cursor()
    assert certify._check(cursor, certify.Check("SELECT 1", "error or true"))[0] == "PASS"
    assert certify._check(cursor, certify.Check("SELECT 0", "error or true", accepted_error=".*"))[0] == "FAIL"
    assert certify._check(cursor, certify.Check("SELECT NULL", "error or true", accepted_error=".*"))[0] == "FAIL"
