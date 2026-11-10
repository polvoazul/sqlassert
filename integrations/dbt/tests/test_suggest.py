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
    assert calls == [[
        "compile", "--target", "dev", "--select", "+path:models/stg_orders.sql", "+path:models/customer_orders.sql",
        "--project-dir", str(compiled.resolve()), "--profiles-dir", str(compiled.resolve()),
    ]]
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
    log = str(project / "logs")

    def run(arguments: list[str], *, to_stderr: bool = False) -> int:
        invoked = dbtRunner().invoke(["--quiet", "--no-send-anonymous-usage-stats", "--log-path", log, *arguments])
        return 0 if invoked.success else 1

    monkeypatch.setattr(dbt_command, "run_dbt", run)
    monkeypatch.chdir(tmp_path)

    code = main(["suggest", "project/models/customer_orders.sql", "--trust-upstream-tests", "--", "--profiles-dir", str(project)])

    assert code == 0
    assert "models/customer_orders.sql:5  mid  Root Select  Not Null  /**ASSERT NOT NULL(n_orders)**/" in capsys.readouterr().out


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
