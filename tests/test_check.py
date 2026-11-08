"""`sqlassert check`: exit codes, configuration, output formats, and marker spans."""

from __future__ import annotations

import io
import json

import pytest

from sqlassert import analyze
from sqlassert.__main__ import main
from sqlassert.check import CODES, EXIT_FINDINGS, EXIT_OK, EXIT_USAGE, UNPROVED_CODES
from sqlassert.provenance import Span

PROVED = "CREATE TABLE users (id INTEGER PRIMARY KEY);\nSELECT id FROM users /**ASSERT UNIQUE(id)**/\n"
UNKNOWN = (
    "CREATE TABLE users (id INTEGER, name VARCHAR);\n"
    "CREATE TABLE sessions (user_id INTEGER);\n"
    "SELECT * FROM sessions\n"
    "  /**ASSERT MANY TO ONE**/ JOIN users ON sessions.user_id = users.id\n"
)
# A table with no CREATE: a Connection Warning, and nothing asserted.
WARNED = "SELECT id FROM events\n"
# A mistyped marker: an error Diagnostic, and nothing asserted.
MISTYPED = "CREATE TABLE users (id INTEGER PRIMARY KEY);\nSELECT id FROM users /**ASSERT UNIQE(id)**/\n"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An empty project directory, made the working directory."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(["check", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# Exit codes ------------------------------------------------------------------


def test_a_program_whose_assertions_all_prove_passes(project, capsys):
    (project / "ok.sql").write_text(PROVED)

    code, out, _ = run(capsys, "ok.sql")

    assert code == EXIT_OK
    assert out == "1 file checked: 1 assertion (1 PROVED, 0 UNKNOWN), 0 errors, 0 warnings\n"


def test_an_unknown_assertion_fails_at_its_marker(project, capsys):
    (project / "q.sql").write_text(UNKNOWN)

    code, out, _ = run(capsys, "q.sql")

    assert code == EXIT_FINDINGS
    assert out.splitlines()[0] == "q.sql:4:3: error unproved-many-to-one-join: /**ASSERT MANY TO ONE**/ is not proved (UNKNOWN)"


def test_warnings_never_fail_the_check(project, capsys):
    (project / "w.sql").write_text(WARNED)

    code, out, _ = run(capsys, "w.sql")

    assert code == EXIT_OK
    assert out.startswith("w.sql: warning undeclared-table: ")


def test_a_diagnostic_fails_the_check_without_any_assertion(project, capsys):
    (project / "m.sql").write_text(MISTYPED)

    code, out, _ = run(capsys, "m.sql")

    assert code == EXIT_FINDINGS
    assert out.startswith("m.sql:2:22: error unrecognized-marker: ")


def test_ignored_codes_neither_print_nor_fail(project, capsys):
    (project / "q.sql").write_text(UNKNOWN)
    (project / "m.sql").write_text(MISTYPED)

    code, out, _ = run(capsys, "q.sql", "m.sql", "--ignore", "unproved-many-to-one-join,unrecognized-marker")

    assert code == EXIT_OK
    assert out.splitlines() == ["2 files checked: 1 assertion (0 PROVED, 1 UNKNOWN), 0 errors, 0 warnings"]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["missing.sql"], "missing.sql: no such file or directory"),
        (["*.sqlx"], "*.sqlx matches no SQL files"),
        (["ok.sql", "--ignore", "unknown-asertion"], "unknown code(s) to ignore: unknown-asertion"),
        (["ok.sql", "--dialect", "nosuchdialect"], "nosuchdialect"),
        ([], "no SQL to check"),
    ],
)
def test_usage_errors_exit_apart_from_findings(project, capsys, argv, message):
    (project / "ok.sql").write_text(PROVED)

    code, out, err = run(capsys, *argv)

    assert code == EXIT_USAGE
    assert out == ""
    assert message in err


def test_every_diagnostic_code_and_unproved_kind_can_be_ignored():
    assert {"unproved-unique", "unproved-not-null", "unproved-exhaustive", "unproved-many-to-one-join"} <= UNPROVED_CODES
    assert UNPROVED_CODES | {"undeclared-table", "sql-parse-failed", "unrecognized-marker"} <= CODES


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("CREATE TABLE t (a INTEGER);\nSELECT a FROM t /**ASSERT UNIQUE(a)**/\n", "unproved-unique"),
        ("CREATE TABLE t (a INTEGER);\nSELECT a FROM t /**ASSERT NOT NULL(a)**/\n", "unproved-not-null"),
        ("CREATE TABLE t (a INTEGER);\nSELECT a FROM t /**ASSERT PRIMARY KEY(a)**/\n", "unproved-not-null-unique"),
        (UNKNOWN, "unproved-many-to-one-join"),
    ],
)
def test_an_unproved_assertion_is_coded_by_its_marker(project, capsys, sql, code):
    (project / "q.sql").write_text(sql)

    exit_, out, _ = run(capsys, "q.sql")

    assert exit_ == EXIT_FINDINGS
    assert f"error {code}: " in out.splitlines()[0]


def test_ignoring_one_unproved_kind_keeps_the_others(project, capsys):
    (project / "q.sql").write_text(
        "CREATE TABLE t (a INTEGER);\nSELECT a FROM t /**ASSERT UNIQUE(a)**/ /**ASSERT NOT NULL(a)**/\n"
    )

    exit_, out, _ = run(capsys, "q.sql", "--ignore", "unproved-unique")

    assert exit_ == EXIT_FINDINGS
    assert "unproved-not-null" in out and "unproved-unique" not in out


# Paths -----------------------------------------------------------------------


def test_directories_and_globs_expand_to_sql_files_once_each(project, capsys):
    (project / "models" / "nested").mkdir(parents=True)
    (project / "models" / "a.sql").write_text(PROVED)
    (project / "models" / "nested" / "b.sql").write_text(PROVED)
    (project / "models" / "notes.txt").write_text("not SQL")

    code, out, _ = run(capsys, "models", "models/**/*.sql", "--format", "json")

    assert code == EXIT_OK
    assert [a["path"] for a in json.loads(out)["assertions"]] == ["models/a.sql", "models/nested/b.sql"]


def test_a_dash_reads_the_program_from_stdin(project, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(UNKNOWN))

    code, out, _ = run(capsys, "-")

    assert code == EXIT_FINDINGS
    assert out.startswith("<stdin>:4:3: error unproved-many-to-one-join")


# Configuration -----------------------------------------------------------------


def test_pyproject_configures_paths_dialect_and_ignored_codes_from_a_subdirectory(project, capsys, monkeypatch):
    (project / "pyproject.toml").write_text(
        '[tool.sqlassert]\ndialect = "postgres"\npaths = ["models"]\nignore = ["unproved-many-to-one-join"]\n'
    )
    (project / "models").mkdir()
    (project / "models" / "q.sql").write_text(UNKNOWN)
    (project / "sub").mkdir()
    monkeypatch.chdir(project / "sub")

    code, out, _ = run(capsys, "--format", "json")

    assert code == EXIT_OK
    document = json.loads(out)
    assert document["findings"] == []
    assert document["summary"]["unknown"] == 1


def test_paths_and_only_ignore_replace_the_configured_values(project, capsys):
    (project / "pyproject.toml").write_text('[tool.sqlassert]\npaths = ["ok.sql"]\nignore = ["unproved-many-to-one-join"]\n')
    (project / "ok.sql").write_text(PROVED)
    (project / "q.sql").write_text(UNKNOWN)

    code, out, _ = run(capsys, "q.sql", "--only-ignore", "undeclared-table")

    assert code == EXIT_FINDINGS
    assert out.startswith("q.sql:4:3: error unproved-many-to-one-join")


def test_ignore_adds_to_the_configured_codes(project, capsys):
    (project / "pyproject.toml").write_text('[tool.sqlassert]\nignore = ["unproved-many-to-one-join"]\n')
    (project / "q.sql").write_text(UNKNOWN)
    (project / "m.sql").write_text(MISTYPED)

    code, out, _ = run(capsys, "q.sql", "m.sql", "--ignore", "unrecognized-marker")

    assert code == EXIT_OK
    assert "unproved-many-to-one-join" not in out and "unrecognized-marker" not in out


def test_only_ignore_empty_ignores_nothing(project, capsys):
    (project / "pyproject.toml").write_text('[tool.sqlassert]\nignore = ["unproved-many-to-one-join"]\n')
    (project / "q.sql").write_text(UNKNOWN)

    code, _, _ = run(capsys, "q.sql", "--only-ignore", "")

    assert code == EXIT_FINDINGS


def test_ignore_and_only_ignore_are_exclusive(project, capsys):
    (project / "q.sql").write_text(UNKNOWN)

    with pytest.raises(SystemExit) as exit_:
        main(["check", "q.sql", "--ignore", "unproved-many-to-one-join", "--only-ignore", "unproved-many-to-one-join"])

    assert exit_.value.code == EXIT_USAGE


def test_the_nearest_pyproject_with_a_sqlassert_table_wins(project, capsys, monkeypatch):
    (project / "pyproject.toml").write_text('[tool.sqlassert]\nignore = ["unproved-many-to-one-join"]\n')
    (project / "inner").mkdir()
    (project / "inner" / "pyproject.toml").write_text('[project]\nname = "inner"\n')
    (project / "inner" / "q.sql").write_text(UNKNOWN)
    monkeypatch.chdir(project / "inner")

    code, _, _ = run(capsys, "q.sql")

    assert code == EXIT_OK


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ('dialects = "duckdb"', "unknown key 'dialects'"),
        ('paths = "models"', "paths must be a list of strings"),
        ("dialect = 3", "dialect must be a string"),
        ('ignore = ["no-such-code"]', "unknown code(s) to ignore: no-such-code"),
    ],
)
def test_a_malformed_configuration_is_a_usage_error(project, capsys, table, message):
    (project / "pyproject.toml").write_text(f"[tool.sqlassert]\n{table}\n")
    (project / "ok.sql").write_text(PROVED)

    code, _, err = run(capsys, "ok.sql")

    assert code == EXIT_USAGE
    assert message in err


# Output formats ----------------------------------------------------------------


def test_explain_prints_each_unknown_assertions_evidence(project, capsys):
    (project / "q.sql").write_text(UNKNOWN)

    _, out, _ = run(capsys, "q.sql", "--explain")

    lines = out.splitlines()
    assert lines[1].startswith("    #1 UNKNOWN: many_to_one_join")
    assert any("No known unique set on this relation" in line for line in lines)


def test_json_lists_every_assertion_and_finding_with_its_span(project, capsys):
    (project / "q.sql").write_text(UNKNOWN)
    (project / "w.sql").write_text(WARNED)

    _, out, _ = run(capsys, "q.sql", "w.sql", "--format", "json", "--explain")

    document = json.loads(out)
    marker = {"line": 4, "column": 3, "end_line": 4, "end_column": 27}
    assert document["assertions"] == [{"path": "q.sql", "id": "assert_1", "outcome": "unknown", "location": marker}]
    unknown, warning = document["findings"]
    assert unknown["location"] == marker
    assert unknown["severity"] == "error"
    assert unknown["evidence"].startswith("#1 UNKNOWN: many_to_one_join")
    assert (warning["path"], warning["severity"], warning["code"], warning["location"]) == (
        "w.sql", "warning", "undeclared-table", None
    )
    assert document["summary"] == {"files": 2, "assertions": 1, "proved": 0, "unknown": 1, "errors": 1, "warnings": 1}


def test_sarif_is_a_structurally_valid_2_1_0_log(project, capsys):
    (project / "q.sql").write_text(UNKNOWN)
    (project / "m.sql").write_text(MISTYPED)
    (project / "w.sql").write_text(WARNED)

    code, out, _ = run(capsys, "q.sql", "m.sql", "w.sql", "--format", "sarif")

    assert code == EXIT_FINDINGS
    log = json.loads(out)
    assert log["version"] == "2.1.0"
    assert log["$schema"].endswith("sarif-2.1.0.json")
    run_, = log["runs"]
    driver = run_["tool"]["driver"]
    assert driver["name"] == "sqlassert"
    rules = [rule["id"] for rule in driver["rules"]]
    assert len(set(rules)) == len(rules)
    assert all(rule["shortDescription"]["text"] for rule in driver["rules"])
    for result in run_["results"]:
        assert result["level"] in {"error", "warning", "note", "none"}
        assert result["message"]["text"]
        assert rules[result["ruleIndex"]] == result["ruleId"]
        location, = result["locations"]
        physical = location["physicalLocation"]
        assert physical["artifactLocation"]["uri"] in {"q.sql", "m.sql", "w.sql"}
        region = physical.get("region", {"startLine": 1})
        assert region["startLine"] >= 1
        assert all(region[key] >= 1 for key in ("startColumn", "endLine", "endColumn") if key in region)
    by_rule = {result["ruleId"]: result for result in run_["results"]}
    assert by_rule["unproved-many-to-one-join"]["locations"][0]["physicalLocation"]["region"] == {
        "startLine": 4, "startColumn": 3, "endLine": 4, "endColumn": 27,
    }
    assert by_rule["undeclared-table"]["level"] == "warning"
    assert "region" not in by_rule["undeclared-table"]["locations"][0]["physicalLocation"]


def test_github_annotations_land_on_the_marker(project, capsys):
    (project / "q.sql").write_text(UNKNOWN)
    (project / "w.sql").write_text(WARNED)

    code, out, _ = run(capsys, "q.sql", "w.sql", "--format", "github", "--explain")

    assert code == EXIT_FINDINGS
    unknown, warning = out.splitlines()
    assert unknown.startswith(
        "::error file=q.sql,line=4,endLine=4,col=3,endColumn=27,title=sqlassert unproved-many-to-one-join::"
        "/**ASSERT MANY TO ONE**/ is not proved (UNKNOWN)%0A#1 UNKNOWN: many_to_one_join"
    )
    assert warning.startswith("::warning file=w.sql,title=sqlassert undeclared-table::table events has no CREATE TABLE")


def test_github_annotations_drop_columns_for_a_marker_across_lines(project, capsys):
    (project / "q.sql").write_text(
        "CREATE TABLE t (a INTEGER, b INTEGER);\nSELECT a, b FROM t /**ASSERT UNIQUE(a,\n  b)**/\n"
    )

    _, out, _ = run(capsys, "q.sql", "--format", "github")

    assert out.startswith("::error file=q.sql,line=2,endLine=3,title=sqlassert unproved-unique::")


# Spans -------------------------------------------------------------------------


def test_every_kind_of_marker_reports_its_span():
    sql = (
        "CREATE TABLE u (id INTEGER PRIMARY KEY, s VARCHAR);\n"
        "SELECT u.id, /**ASSERT EXHAUSTIVE**/ CASE u.s WHEN 'a' THEN 1 END AS n\n"
        "FROM u /**ASSERT UNIQUE**/ JOIN u AS v ON v.id = u.id /**ASSERT NOT NULL(\n"
        "  id)**/\n"
    )

    origins = [assertion.origin for assertion in analyze(sql).assertions]

    assert sorted((origin.line, origin.span) for origin in origins) == [
        (2, Span(2, 14, 2, 37)), (3, Span(3, 8, 3, 27)), (3, Span(3, 55, 4, 9)),
    ]


def test_a_misplaced_marker_reports_its_span():
    sql = "CREATE TABLE u (id INTEGER);\nSELECT * FROM u /**ASSERT UNIQUE**/ , u AS v\n"

    diagnostic, = analyze(sql).diagnostics

    assert diagnostic.code == "unattached-marker"
    assert diagnostic.origin is not None
    assert diagnostic.origin.span == Span(2, 17, 2, 36)
