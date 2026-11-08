"""The `.sqltest` collector: parsing, includes, and expectation matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from sqlassert import analyze
from sqltest import Case, ExpectedDiagnostic, ExpectedOutcome, SqlTestError, mismatches, parse_annotation, parse_file

SCHEMA = "CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER);\n"


def cases(tmp_path: Path, text: str, **files: str) -> dict[str, Case]:
    for name, body in files.items():
        (tmp_path / name).write_text(body)
    path = tmp_path / "x.sqltest"
    path.write_text(text)
    return {case.name: case for case in parse_file(path, root=tmp_path)}


def problems(case: Case) -> list[str]:
    return mismatches(case, analyze(case.sql, dialect=case.dialect))


def test_an_annotation_lists_outcomes_and_diagnostics():
    expectation = parse_annotation(' PROVED, UNKNOWN, unknown-asserted-column "a" "b \\" c"', Path("x"), 1)
    assert expectation.outcomes == [ExpectedOutcome("PROVED"), ExpectedOutcome("UNKNOWN")]
    assert expectation.diagnostics == [ExpectedDiagnostic("unknown-asserted-column", ("a", 'b " c'))]


@pytest.mark.parametrize("text", ["proved", "PROVED x", "code bare", '"x"', "PROVED,", "a = b"])
def test_a_malformed_annotation_is_an_error(text: str):
    with pytest.raises(SqlTestError):
        parse_annotation(text, Path("x"), 1)


def test_an_outcome_checks_its_explanation(tmp_path: Path):
    found = cases(tmp_path, SCHEMA + (
        "-- case: right\n"
        "SELECT a FROM t /**ASSERT NOT NULL(a)**/\n"
        '--~ UNKNOWN "a"\n'
        "-- case: wrong\n"
        "SELECT a FROM t /**ASSERT NOT NULL(a)**/\n"
        '--~ UNKNOWN "not in the explanation"\n'
    ))
    assert problems(found["right"]) == []
    [problem] = problems(found["wrong"])
    assert problem.startswith("x.sqltest:6: UNKNOWN explanation lacks 'not in the explanation'")


def test_setup_is_shared_and_each_program_line_maps_to_its_file_line(tmp_path: Path):
    found = cases(tmp_path, SCHEMA + (
        "-- case: one\n"
        "SELECT id FROM t /**ASSERT UNIQUE(id)**/  --~ PROVED\n"
        "-- case: two\n"
        "SELECT a FROM t\n"
        "/**ASSERT UNIQUE(a)**/  --~ UNKNOWN\n"
    ))
    assert found["one"].sql.splitlines()[0] == SCHEMA.strip()
    assert [source.line for source in found["two"].sources] == [1, 4, 5, 6]
    assert "--~" not in found["two"].sql and "case:" not in found["two"].sql
    assert problems(found["one"]) == [] and problems(found["two"]) == []


def test_a_standalone_annotation_checks_the_nearest_sql_line_above(tmp_path: Path):
    found = cases(tmp_path, SCHEMA + (
        "-- case: c\n"
        "SELECT a FROM t /**ASSERT UNIQUE(a)**/\n"
        "\n"
        "-- a comment\n"
        "--~ UNKNOWN\n"
    ))
    assert problems(found["c"]) == []


def test_standalone_annotations_stack(tmp_path: Path):
    found = cases(tmp_path, SCHEMA + (
        "-- case: c\n"
        "SELECT a FROM t /**ASSERT UNIQUE(a)**/ /**ASSERT UNIQUE(nope)**/\n"
        "--~ UNKNOWN\n"
        '--~ unknown-asserted-column "nope"\n'
    ))
    assert problems(found["c"]) == []


def test_a_program_level_annotation_checks_diagnostics_without_a_line(tmp_path: Path):
    found = cases(tmp_path, "-- case: c\nSELECT FROM WHERE;\n--~! sql-parse-failed\n")
    assert problems(found["c"]) == []


def test_every_outcome_and_diagnostic_must_be_annotated(tmp_path: Path):
    found = cases(tmp_path, SCHEMA + (
        "-- case: missing\n"
        "SELECT a FROM t /**ASSERT UNIQUE(a)**/\n"
        "-- case: wrong\n"
        "SELECT a FROM t /**ASSERT UNIQUE(a)**/  --~ PROVED\n"
        "-- case: unexpected\n"
        "SELECT a FROM t /**ASSERT UNIQUE(nope)**/\n"
        "-- case: substring\n"
        "SELECT a FROM t /**ASSERT UNIQUE(nope)**/\n"
        '--~ unknown-asserted-column "elsewhere"\n'
    ))
    assert problems(found["missing"]) == ["x.sqltest:3: outcomes ['UNKNOWN'], expected none"]
    assert problems(found["wrong"]) == ["x.sqltest:5: outcomes ['UNKNOWN'], expected ['PROVED']"]
    [unexpected] = problems(found["unexpected"])
    assert unexpected.startswith("x.sqltest:7: unexpected unknown-asserted-column")
    assert problems(found["substring"])[0] == 'x.sqltest:9: expected unknown-asserted-column "elsewhere", not reported'


def test_an_annotation_on_the_wrong_line_fails(tmp_path: Path):
    found = cases(tmp_path, SCHEMA + (
        "-- case: c\n"
        "SELECT a  --~ UNKNOWN\n"
        "FROM t /**ASSERT UNIQUE(a)**/\n"
    ))
    assert len(problems(found["c"])) == 2


def test_an_include_is_relative_to_the_root_and_may_carry_annotations(tmp_path: Path):
    (tmp_path / "schemas").mkdir()
    found = cases(
        tmp_path,
        "-- include: schemas/s.sql\n-- case: c\nSELECT id FROM t /**ASSERT UNIQUE(id)**/  --~ PROVED\n",
        **{"schemas/s.sql": "-- include: schemas/u.sql\n" + SCHEMA,
           "schemas/u.sql": "CREATE VIEW mv AS SELECT * FROM missing;\n--~! undeclared-table\n"},
    )
    assert found["c"].sources[0].path == (tmp_path / "schemas/u.sql").resolve()
    assert problems(found["c"]) == []


def test_a_dialect_applies_to_the_file_or_one_case(tmp_path: Path):
    found = cases(tmp_path, "-- dialect: postgres\n-- case: a\nSELECT 1;\n-- case: b\n-- dialect: sqlite\nSELECT 1;\n")
    assert (found["a"].dialect, found["b"].dialect) == ("postgres", "sqlite")


@pytest.mark.parametrize(
    ("text", "files", "message"),
    [
        ("SELECT 1;\n", {}, "no -- case:"),
        ("-- case: a\n-- case: a\n", {}, "duplicate case name"),
        ("-- case: a-b\n", {}, "case names"),
        ("--~ PROVED\n-- case: a\n", {}, "needs a SQL line above"),
        ("-- include: missing.sql\n-- case: a\n", {}, "include not found"),
        ("-- include: x.sqltest\n-- case: a\n", {}, "only include .sql"),
        ("-- include: a.sql\n-- case: a\n", {"a.sql": "-- include: a.sql\n"}, "include cycle"),
        ("-- include: a.sql\n-- case: a\n", {"a.sql": "-- case: b\n"}, "only allowed in .sqltest"),
        ("-- case: a\nSELECT 1;  --~! sql-parse-failed\n", {}, "stands alone"),
        ("-- case: a\n--~! PROVED\n", {}, "Diagnostics, not outcomes"),
    ],
)
def test_a_malformed_file_names_its_line(tmp_path: Path, text: str, files: dict[str, str], message: str):
    with pytest.raises(SqlTestError, match=message):
        cases(tmp_path, text, **files)
