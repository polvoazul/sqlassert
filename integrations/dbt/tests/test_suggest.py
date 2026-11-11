"""`sqlassert-dbt suggest`: what `sqlassert suggest` finds in model files,
with what each model reads declared, at the model file's lines."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from dbt.cli.main import dbtRunner
from dbt_runs import copy_source, dbt

from sqlassert_dbt import ProjectError, SuggestReport, analyze_project, suggest_models
from sqlassert_dbt.report import Location, Problem, WarningLimit
from sqlassert_dbt import __main__ as dbt_command
from sqlassert_dbt.__main__ import main
from sqlassert.reporting import Outcome


def rows(report: SuggestReport, model: str) -> list[tuple[str, str, str]]:
    return [(str(item.location), item.label, item.text) for item in report.suggestions if item.model == model]


def test_suggestions_use_the_facts_of_what_the_model_reads(compiled):
    report = suggest_models(compiled, ["models/stg_orders.sql"], trust_upstream_tests=True)
    # raw.orders has unknown columns; its passing unique and not_null tests are
    # facts. order_id's own unique test leaves only NOT NULL to suggest.
    assert rows(report, "stg_orders") == [
        ("models/stg_orders.sql:2", "Root Select", "/**ASSERT NOT NULL(order_id)**/"),
        ("models/stg_orders.sql:2", "Root Select", "/**ASSERT NOT NULL(status)**/"),
    ]
    assert report.models == ("stg_orders",)


def test_untrusted_upstream_tests_suggest_nothing_from_them(compiled):
    report = suggest_models(compiled, ["models/stg_orders.sql"])
    assert rows(report, "stg_orders") == []
    assert "dbt-untrusted-upstream-tests" in {problem.code for problem in report.problems}


def test_a_view_upstream_is_read_through_its_query(compiled):
    report = suggest_models(compiled, ["models/customer_orders.sql"], trust_upstream_tests=True)
    # Its own UNIQUE(customer_id) marker is not suggested again.
    assert rows(report, "customer_orders") == [
        ("models/customer_orders.sql:5", "Root Select", "/**ASSERT NOT NULL(n_orders)**/"),
    ]


def test_an_inlined_ephemeral_model_is_not_suggested_on_in_its_reader(compiled):
    report = suggest_models(compiled, ["models/customers.sql"], trust_upstream_tests=True)
    labels = {label for _, label, _ in rows(report, "customers")}
    assert labels and not any("customer_orders" in label for label in labels)
    assert ("models/customers.sql:7", "CTE orders", "/**ASSERT UNIQUE(customer_id)**/") in rows(report, "customers")


def test_a_marker_the_models_own_tests_state_is_not_suggested(compiled):
    report = suggest_models(
        compiled, ["models/customer_signups.sql", "models/stg_customers.sql", "models/customers.sql"], trust_upstream_tests=True,
    )
    # unique and not_null tests on every key column of both.
    assert rows(report, "customer_signups") == rows(report, "stg_customers") == []
    # customers tests unique(customer_id) and not_null(n_orders) only.
    assert ("models/customers.sql:11", "Root Select", "/**ASSERT NOT NULL(customer_id)**/") in rows(report, "customers")
    # A CTE's marker is about the CTE, not the model, so its tests do not state it.
    assert ("models/customers.sql:7", "CTE orders", "/**ASSERT NOT NULL(n_orders)**/") in rows(report, "customers")


def test_a_line_is_found_from_the_lines_around_it_when_the_raw_code_does_not_render(compiled):
    # var() is outside the rendered subset, and the suggestion's own line holds it.
    report = suggest_models(compiled, ["models/active_customers.sql"], trust_upstream_tests=True)
    assert rows(report, "active_customers") == [
        ("models/active_customers.sql:3", "Root Select", "/**ASSERT NOT NULL UNIQUE(customer_id)**/"),
    ]


def test_a_rewrite_or_marker_needs_every_incremental_branch(compiled, tmp_path):
    project = tmp_path / "project"
    shutil.copytree(compiled, project)
    path = project / "target" / "manifest.json"
    manifest = json.loads(path.read_text())
    node = next(node for node in manifest["nodes"].values() if node["name"] == "orders_incremental")
    complete = suggest_models(compiled, ["models/orders_incremental.sql"], trust_upstream_tests=True)
    # The Root Select ends on line 4 in one branch and 6 in the other: the
    # marker goes after both, past the {% endif %} on line 7.
    assert rows(complete, "orders_incremental") == [
        ("models/orders_incremental.sql:7", "Root Select", "/**ASSERT NOT NULL(status)**/"),
    ]

    node["raw_code"] = node["raw_code"].replace("{{ this }}", "{{ this }} where {{ var('x') }}")
    path.write_text(json.dumps(manifest))
    report = suggest_models(project, ["models/orders_incremental.sql"], trust_upstream_tests=True)
    assert rows(report, "orders_incremental") == []
    assert "dbt-unanalyzed-branch" in {problem.code for problem in report.problems}


def test_a_file_that_is_no_model_is_a_project_error(compiled):
    with pytest.raises(ProjectError, match="models/schema.yml is not a model"):
        suggest_models(compiled, ["models/schema.yml"])


def fake_dbt(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(arguments: list[str], *, to_stderr: bool = False) -> int:
        assert to_stderr  # stdout is the report's.
        calls.append(arguments)
        # As dbt would: the compiled manifest lands in --target-path.
        project = Path(arguments[arguments.index("--project-dir") + 1])
        if "--target-path" in arguments:
            target = Path(arguments[arguments.index("--target-path") + 1])
            target = target if target.is_absolute() else project / target
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy(project / "target" / "manifest.json", target / "manifest.json")
        return 0

    monkeypatch.setattr(dbt_command, "run_dbt", run)
    return calls


def test_command_compiles_what_the_models_read_and_lists_by_model(compiled, monkeypatch, capsys):
    calls = fake_dbt(monkeypatch)

    code = main([
        "suggest", str(compiled / "models/stg_orders.sql"), str(compiled / "models/customer_orders.sql"),
        "--trust-upstream-tests", "--", "--target", "dev",
    ])

    assert code == 0
    (call,) = calls
    assert call[:call.index("--target-path")] == [
        "compile", "--target", "dev", "--select", "+path:models/stg_orders.sql", "+path:models/customer_orders.sql",
        "--project-dir", str(compiled.resolve()), "--profiles-dir", str(compiled.resolve()),
    ]
    # dbt's target/ and logs/ go to a scratch directory, not the project.
    target, logs = call[call.index("--target-path") + 1], call[call.index("--log-path") + 1]
    assert not Path(target).is_relative_to(compiled.resolve()) and not Path(logs).is_relative_to(compiled.resolve())
    assert not Path(target).exists()  # Removed afterwards.
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["stg_orders:", "  models/stg_orders.sql:2  mid  Root Select  Not Null  /**ASSERT NOT NULL(order_id)**/"]
    assert "customer_orders:" in out
    assert out[-1].startswith("0 improvements, 3 markers, 0 smells in 2 models")


def test_command_explains_and_keeps_the_callers_selection(compiled, monkeypatch, capsys):
    calls = fake_dbt(monkeypatch)

    main(["suggest", str(compiled / "models/stg_orders.sql"), "--explain", "--trust-upstream-tests", "--", "-s", "stg_orders"])

    assert calls[0][:3] == ["compile", "-s", "stg_orders"] and "+path:models/stg_orders.sql" not in calls[0]
    assert "PROVED" in capsys.readouterr().out


def test_command_takes_the_project_directory_for_every_model(compiled, monkeypatch, capsys):
    calls = fake_dbt(monkeypatch)
    monkeypatch.chdir(compiled.parent)

    assert main(["suggest", compiled.name, "--trust-upstream-tests"]) == 0

    # The whole project compiles; no selection.
    assert "--select" not in calls[0]
    assert "in 9 models" in capsys.readouterr().out


def test_command_takes_a_directory_of_models(compiled, monkeypatch, capsys):
    calls = fake_dbt(monkeypatch)

    main(["suggest", str(compiled / "models")])

    assert calls[0][calls[0].index("--select") + 1] == "+path:models"
    assert "in 9 models" in capsys.readouterr().out


@pytest.mark.parametrize("given", [["--profiles-dir", "elsewhere"], ["--profiles-dir=elsewhere"]])
def test_a_profiles_dir_of_the_callers_is_kept(compiled, monkeypatch, given):
    calls = fake_dbt(monkeypatch)
    main(["suggest", str(compiled / "models/stg_orders.sql"), "--", *given])
    assert calls[0].count(str(compiled.resolve())) == 1  # --project-dir only


def test_a_target_path_of_the_callers_is_kept(compiled, monkeypatch, capsys):
    calls = fake_dbt(monkeypatch)
    main(["suggest", str(compiled / "models/stg_orders.sql"), "--trust-upstream-tests", "--", "--target-path", "kept"])
    assert calls[0].count("--target-path") == 1 and "stg_orders:" in capsys.readouterr().out
    assert (compiled / "kept" / "manifest.json").is_file()
    shutil.rmtree(compiled / "kept")


def test_dbt_can_compile_in_place(compiled, monkeypatch, capsys):
    calls = fake_dbt(monkeypatch)
    main(["suggest", str(compiled / "models/stg_orders.sql"), "--trust-upstream-tests", "--dbt-compile-inplace"])
    assert "--target-path" not in calls[0] and "--log-path" not in calls[0]
    assert "stg_orders:" in capsys.readouterr().out


def test_dbt_profiles_dir_is_kept(compiled, monkeypatch):
    calls = fake_dbt(monkeypatch)
    monkeypatch.setenv("DBT_PROFILES_DIR", "elsewhere")
    main(["suggest", str(compiled / "models/stg_orders.sql")])
    assert "--profiles-dir" not in calls[0]


def test_command_needs_a_project_around_the_files(tmp_path, monkeypatch, capsys):
    fake_dbt(monkeypatch)
    (tmp_path / "model.sql").write_text("select 1")
    assert main(["suggest", str(tmp_path / "model.sql")]) == 2
    assert "no dbt_project.yml at or above" in capsys.readouterr().err


def test_command_runs_dbt_selecting_by_path_from_another_directory(tmp_path, monkeypatch, capsys):
    project = copy_source(tmp_path)

    def run(arguments: list[str], *, to_stderr: bool = False) -> int:
        invoked = dbtRunner().invoke(["--quiet", "--no-send-anonymous-usage-stats", *arguments])
        return 0 if invoked.success else 1

    monkeypatch.setattr(dbt_command, "run_dbt", run)
    monkeypatch.chdir(tmp_path)

    code = main(["suggest", "project/models/customer_orders.sql", "--trust-upstream-tests", "--", "--profiles-dir", str(project)])

    assert code == 0
    assert "models/customer_orders.sql:5  mid  Root Select  Not Null  /**ASSERT NOT NULL(n_orders)**/" in capsys.readouterr().out
    # Real dbt wrote its target/ and logs/ elsewhere.
    assert not (project / "target").exists() and not (project / "logs").exists()


def test_every_suggested_marker_pasted_at_its_line_is_proved(tmp_path):
    project = copy_source(tmp_path)
    assert dbt(project, "compile")
    files = sorted(path.relative_to(project).as_posix() for path in (project / "models").glob("*.sql"))
    report = suggest_models(project, files, trust_upstream_tests=True)
    pasted = [item for item in report.suggestions if item.level == "mid" and item.location.path.startswith("models/")]
    for path in {item.location.path for item in pasted}:
        lines = (project / path).read_text().split("\n")
        for item in pasted:
            if item.location.path == path:
                lines[(item.location.line or 0) - 1] += " " + item.text
        (project / path).write_text("\n".join(lines))
    assert dbt(project, "compile")

    checked = analyze_project(project, trust_upstream_tests=True)
    outcomes = {(str(marker.location), marker.marker): marker.outcome for marker in checked.markers}
    assert len(pasted) >= 8
    assert all(outcomes[str(item.location), item.text] is Outcome.PROVED for item in pasted)


def test_each_model_is_reported_as_soon_as_it_is_analyzed(compiled):
    parts: list[SuggestReport] = []
    report = suggest_models(
        compiled, ["models/stg_orders.sql", "models/customer_orders.sql"], trust_upstream_tests=True, on_model=parts.append,
    )
    # One part per model, in order, then the warnings about what they read.
    assert [part.models for part in parts] == [("stg_orders",), ("customer_orders",), ()]
    assert "dbt-unknown-columns" in {problem.code for problem in parts[-1].problems}
    assert report.models == ("stg_orders", "customer_orders")
    assert report.suggestions == parts[0].suggestions + parts[1].suggestions


def test_warnings_past_the_limit_of_their_code_are_suppressed_and_counted():
    limit = WarningLimit(2)
    warning = Problem("ambiguous-column", "column x", "warning")
    other = Problem("undeclared-table", "table t", "warning")
    error = Problem("dbt-unanalyzed-model", "model m", "error")
    printed = [line for problem in (warning, warning, warning, other, warning, error, error, error) for line in limit.lines(problem)]
    assert printed == [
        "warning ambiguous-column: column x",
        "warning ambiguous-column: column x",
        "warning: suppressing further 'ambiguous-column' warnings (--warning-limit 0 prints every one)",
        "warning undeclared-table: table t",
        *["error dbt-unanalyzed-model: model m"] * 3,
    ]
    assert limit.suppressed == 2
    assert WarningLimit(0).lines(warning) == ["warning ambiguous-column: column x"]


def test_suggest_takes_a_warning_limit(compiled, monkeypatch, capsys):
    fake_dbt(monkeypatch)
    main(["suggest", str(compiled / "models"), "--warning-limit", "1"])
    out = capsys.readouterr().out
    assert out.count("warning dbt-unknown-columns") == 1
    assert "suppressing further 'dbt-unknown-columns' warnings" in out
    assert out.rstrip().endswith("suppressed)")


def test_core_advice_for_unknown_columns_points_at_the_catalog(compiled):
    report = analyze_project(compiled, trust_upstream_tests=True)
    ambiguous = [problem for problem in report.problems if problem.code == "ambiguous-column"]
    assert ambiguous and all("analyze_database" not in problem.message for problem in report.problems)
    assert "dbt docs generate" in ambiguous[0].message


def test_a_compiled_location_goes_through_a_short_link(tmp_path):
    compiled = tmp_path / "target" / "compiled"
    (compiled / "jaffle" / "models").mkdir(parents=True)
    (compiled / "jaffle" / "models" / "m.sql").write_text("select 1")
    shortcut = dbt_command._Shortcut(compiled)
    assert shortcut.location(Location("models/m.sql", 3)) == Location("models/m.sql", 3)
    assert shortcut.link is None  # made only when a location needs it
    short = shortcut.location(Location(str(compiled / "jaffle" / "models" / "m.sql"), 3))
    try:
        assert shortcut.link is not None and short.path == str(shortcut.link / "jaffle" / "models" / "m.sql")
        assert len(shortcut.link.name) == len("sqlassert-") + 6
        assert Path(short.path).read_text() == "select 1"
    finally:
        shortcut.link.unlink()
