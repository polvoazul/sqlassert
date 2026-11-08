"""The dbt adapter over `tests/dbt_project`: a hand-written manifest (schema v12)
with the raw model files it was compiled from."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sqlassert.__main__ import main
from sqlassert.dbt import DbtReport, ProjectError, Status, analyze_project
from sqlassert.dbt.expectations import expectations
from sqlassert.dbt.jinja import Unrenderable, render
from sqlassert.dbt.manifest import load_manifest
from sqlassert.ir.model import LiteralValue
from sqlassert.reporting import Outcome

PROJECT = Path(__file__).parent / "dbt_project"


@pytest.fixture(scope="module")
def report() -> DbtReport:
    return analyze_project(PROJECT)


def statuses(report: DbtReport) -> dict[str, tuple[Status, str]]:
    return {test.name: (test.status, test.reason) for test in report.tests}


def markers(report: DbtReport) -> dict[str, Outcome]:
    return {f"{marker.location} {marker.marker}": marker.outcome for marker in report.markers}


def copy_project(tmp_path: Path) -> Path:
    shutil.copytree(PROJECT, tmp_path / "project")
    return tmp_path / "project"


def edit_manifest(project: Path, edit) -> None:
    path = project / "target" / "manifest.json"
    manifest = json.loads(path.read_text())
    edit(manifest)
    path.write_text(json.dumps(manifest))


def node_named(manifest: dict, name: str) -> dict:
    return next(node for node in manifest["nodes"].values() if node["name"] == name)


# Tests as assertions, and the passing tests of upstream relations as declarations


def test_a_test_is_proved_from_upstream_tests_that_are_facts(report):
    found = statuses(report)
    # stg_customers renames raw.customers.id, whose unique and not_null tests passed.
    assert found["unique_stg_customers_customer_id"][0] is Status.PROVED
    assert found["not_null_stg_customers_customer_id"][0] is Status.PROVED
    assert found["unique_stg_orders_order_id"][0] is Status.PROVED
    assert found["not_null_customers_n_orders"][0] is Status.PROVED


def test_a_test_is_never_its_own_declaration(report):
    status, reason = statuses(report)["unique_stg_customers_first_name"]
    assert status is Status.NOT_PROVED
    assert "not proved from the SQL" in reason


def test_a_warn_severity_test_is_no_fact_downstream(report):
    # raw.orders' accepted_values test only warns, so stg_orders' own is not proved.
    assert statuses(report)["accepted_values_stg_orders_status"][0] is Status.NOT_PROVED


def test_an_error_severity_test_is_a_fact_downstream(tmp_path):
    project = copy_project(tmp_path)
    edit_manifest(project, lambda m: node_named(m, "source_accepted_values_raw_orders_status")["config"].update(severity="ERROR"))
    assert statuses(analyze_project(project))["accepted_values_stg_orders_status"][0] is Status.PROVED


def test_a_where_config_makes_a_test_no_fact(tmp_path):
    project = copy_project(tmp_path)
    edit_manifest(project, lambda m: node_named(m, "source_unique_raw_customers_id")["config"].update(where="id > 0"))
    assert statuses(analyze_project(project))["unique_stg_customers_customer_id"][0] is Status.NOT_PROVED


def test_tables_and_relationships_are_left_to_the_warehouse(report):
    found = statuses(report)
    assert found["source_unique_raw_customers_id"] == (Status.NOT_PROVED, "a source is a table: only its own data shows this")
    assert found["not_null_raw_payments_id"][1] == "a seed is a table: only its own data shows this"
    # Referential Coverage is not carried through a view's projection.
    assert found["relationships_stg_orders_customer_id"][0] is Status.NOT_PROVED


def test_proved_tests_are_skippable_and_constraints_are_not(report):
    proved = [test for test in report.tests if test.status is Status.PROVED]
    assert proved and all(test.skippable for test in proved)


# Incremental models


def test_an_incremental_key_holds_under_merge_on_that_unique_key(report):
    found = statuses(report)
    assert found["unique_orders_incremental_order_id"][0] is Status.PROVED
    assert found["not_null_orders_incremental_order_id"][0] is Status.PROVED
    status, reason = found["unique_orders_appended_order_id"]
    assert status is Status.NOT_PROVED and "keeps rows of earlier runs" in reason


def test_both_incremental_branches_are_analyzed(report):
    marker = next(m for m in report.markers if m.model == "orders_incremental")
    assert (str(marker.location), marker.outcome) == ("models/orders_incremental.sql:8", Outcome.PROVED)
    # The ambiguous column is in the is_incremental() branch only, which dbt did not compile.
    assert any(
        problem.code == "ambiguous-column" and str(problem.location) == "models/orders_incremental.sql:6"
        for problem in report.problems
    )


def test_an_unrenderable_branch_leaves_its_model_unproved(tmp_path):
    project = copy_project(tmp_path)

    def edit(manifest):
        node = node_named(manifest, "orders_incremental")
        node["raw_code"] = node["raw_code"].replace("{{ this }}", "{{ this }} where {{ var('x') }}")

    edit_manifest(project, edit)
    report = analyze_project(project)
    assert statuses(report)["unique_orders_incremental_order_id"][0] is Status.NOT_PROVED
    marker = next(m for m in report.markers if m.model == "orders_incremental")
    assert marker.outcome is Outcome.UNKNOWN and "is_incremental() branch was not analyzed" in marker.explanation
    assert any(problem.code == "dbt-unanalyzed-branch" for problem in report.problems)


# Markers in model files


def test_markers_map_back_to_the_model_file(report):
    assert markers(report) == {
        "models/active_customers.sql:4 /**ASSERT UNIQUE(customer_id)**/": Outcome.PROVED,
        "models/customer_orders.sql:6 /**ASSERT UNIQUE(customer_id)**/": Outcome.PROVED,
        "models/customers.sql:11 /**ASSERT MANY TO ONE**/": Outcome.UNKNOWN,
        "models/customers.sql:12 /**ASSERT NOT NULL(first_name)**/": Outcome.UNKNOWN,
        "models/orders_incremental.sql:8 /**ASSERT UNIQUE(order_id)**/": Outcome.PROVED,
    }


def test_an_ephemeral_models_markers_are_reported_once(report):
    # customer_orders is inlined into customers, whose analysis does not report its marker again.
    assert sum(marker.model == "customer_orders" for marker in report.markers) == 1
    assert all(marker.model != "customers" or "customer_orders" not in str(marker.location) for marker in report.markers)


def test_a_marker_is_found_by_its_text_when_the_raw_code_does_not_render(report):
    # active_customers uses var(), which only dbt renders.
    marker = next(m for m in report.markers if m.model == "active_customers")
    assert marker.location.line == 4


# Proved but untested


def test_untested_properties_are_offered_as_schema_additions(report):
    offered = {(item.model, item.column, item.test) for item in report.untested}
    assert ("customers", "customer_id", "not_null") in offered
    assert ("stg_orders", "status", "not_null") in offered
    assert ("active_customers", "customer_id", "unique") in offered
    # Already tested.
    assert ("customers", "n_orders", "not_null") not in offered
    assert ("stg_customers", "customer_id", "unique") not in offered
    # An incremental table keeps rows of earlier runs: no key is offered.
    assert not any(item.model == "orders_incremental" and item.test == "unique" for item in report.untested)


# Problems


def test_relations_with_unknown_columns_are_warned_about_once(report):
    unknown = [p for p in report.problems if p.code == "dbt-unknown-columns"]
    assert sorted(p.model for p in unknown if p.model) == ["raw.customers", "raw.orders"]
    assert all(p.severity == "warning" for p in unknown)
    assert not any(p.code == "undeclared-table" for p in report.problems)
    assert not any(p.severity == "error" for p in report.problems)


def test_an_unsupported_adapter_is_not_guessed(tmp_path):
    project = copy_project(tmp_path)
    edit_manifest(project, lambda m: m["metadata"].update(adapter_type="snowflake"))
    report = analyze_project(project)
    assert [p.code for p in report.problems] == ["dbt-unsupported-dialect"]
    assert report.failed and not report.markers
    assert all(test.status is Status.NOT_PROVED for test in report.tests)
    assert analyze_project(project, dialect="duckdb").markers


def test_a_missing_manifest_is_a_project_error(tmp_path):
    with pytest.raises(ProjectError, match="dbt compile"):
        analyze_project(tmp_path)


# Command line


def test_command_exits_1_on_an_unproved_marker(capsys):
    assert main(["dbt", str(PROJECT)]) == 1
    out = capsys.readouterr().out
    assert "models/customers.sql:12: UNKNOWN /**ASSERT NOT NULL(first_name)**/" in out
    assert "Proved statically" in out and "schema.yml additions" in out


def test_command_exits_0_when_every_marker_is_proved(tmp_path, capsys):
    project = copy_project(tmp_path)

    def edit(manifest):
        node = node_named(manifest, "customers")
        for text in ("/**ASSERT MANY TO ONE**/ ", "\n/**ASSERT NOT NULL(first_name)**/"):
            node["compiled_code"] = node["compiled_code"].replace(text, "")
            node["raw_code"] = node["raw_code"].replace(text, "")

    edit_manifest(project, edit)
    assert main(["dbt", str(project)]) == 0


def test_command_exits_2_without_a_manifest(tmp_path, capsys):
    assert main(["dbt", str(tmp_path)]) == 2
    assert "manifest" in capsys.readouterr().err


# Expectations


def manifest_with_test(tmp_path: Path, name: str, kwargs: dict, config: dict | None = None, namespace: str | None = None):
    project = copy_project(tmp_path)

    def edit(manifest):
        node = node_named(manifest, "unique_stg_customers_customer_id")
        node["test_metadata"] = {"name": name, "kwargs": {"model": "{{ get_where_subquery(ref('stg_customers')) }}", **kwargs}, "namespace": namespace}
        node["config"].update(config or {})

    edit_manifest(project, edit)
    manifest = load_manifest(project / "target" / "manifest.json")
    return next(e for e in expectations(manifest) if e.name == "unique_stg_customers_customer_id")


@pytest.mark.parametrize(("config", "fact"), [
    ({}, True),
    ({"severity": "warn"}, False),
    ({"where": "x > 1"}, False),
    ({"error_if": "> 10"}, False),
    ({"error_if": ">= 1"}, True),
    ({"fail_calc": "sum(n)"}, False),
])
def test_only_a_test_that_fails_on_any_failing_row_is_a_fact(tmp_path, config, fact):
    assert manifest_with_test(tmp_path, "unique", {"column_name": "customer_id"}, config).is_fact is fact


@pytest.mark.parametrize(("config", "provable"), [({}, True), ({"warn_if": "< 1"}, False), ({"fail_calc": "sum(n)"}, False)])
def test_a_test_is_provable_only_when_zero_failing_rows_pass_it(tmp_path, config, provable):
    expectation = manifest_with_test(tmp_path, "unique", {"column_name": "customer_id"}, config)
    assert (expectation.unprovable is None) is provable


def test_unique_combination_is_proved_by_a_non_null_unique_set_and_declares_a_unique_set(tmp_path):
    expectation = manifest_with_test(
        tmp_path, "unique_combination_of_columns", {"combination_of_columns": ["customer_id", "first_name"]},
        namespace="dbt_utils",
    )
    assert (expectation.asserted, expectation.declared, expectation.columns) == ("key", "unique", ("customer_id", "first_name"))


@pytest.mark.parametrize(("quote", "values"), [
    (True, (LiteralValue("string", "1"), LiteralValue("string", "a"))),
    (False, None),
])
def test_accepted_values_are_strings_unless_unquoted(tmp_path, quote, values):
    expectation = manifest_with_test(tmp_path, "accepted_values", {"column_name": "first_name", "values": [1, "a"], "quote": quote})
    assert (expectation.values or None) == values


def test_unquoted_accepted_values_are_sql_literals(tmp_path):
    expectation = manifest_with_test(tmp_path, "accepted_values", {"column_name": "first_name", "values": [1, True], "quote": False})
    assert expectation.values == (LiteralValue("numeric", "1"), LiteralValue("boolean", "TRUE"))


def test_a_test_of_an_expression_states_no_property(tmp_path):
    expectation = manifest_with_test(tmp_path, "unique", {"column_name": "lower(first_name)"})
    assert expectation.asserted is None and "expression" in (expectation.unprovable or "")


@pytest.mark.parametrize(("adapter", "materialized", "enforced", "fact"), [
    ("duckdb", "table", True, True),
    ("duckdb", "view", True, False),
    ("duckdb", "table", False, False),
    ("snowflake", "table", True, False),
])
def test_a_primary_key_constraint_is_a_fact_only_where_it_is_enforced(tmp_path, adapter, materialized, enforced, fact):
    project = copy_project(tmp_path)

    def edit(manifest):
        manifest["metadata"]["adapter_type"] = adapter
        node = node_named(manifest, "stg_customers")
        node["config"]["materialized"] = materialized
        node["contract"]["enforced"] = enforced
        node["constraints"] = [{"type": "primary_key", "columns": ["customer_id"], "name": None, "expression": None,
                                "warn_unenforced": True, "warn_unsupported": True, "to": None, "to_columns": []}]

    edit_manifest(project, edit)
    found = [e for e in expectations(load_manifest(project / "target" / "manifest.json")) if e.kind == "constraint"]
    assert [(e.asserted, e.is_fact) for e in found] == [("key", fact)]


# Rendering raw code


def resolve(function, arguments, options):
    return {"this": "me"}.get(function) or f"<{function}:{','.join(map(str, arguments))}>"


def test_render_takes_one_incremental_branch_with_whitespace_control():
    raw = "{{ config(materialized='incremental') }}\nselect 1 from {{ ref('a') }}\n{%- if is_incremental() %} where x > (select max(x) from {{ this }}){% else %} -- full{% endif %}\n"
    assert render(raw, incremental=False, resolve=resolve).text == "\nselect 1 from <ref:a> -- full"
    assert render(raw, incremental=True, resolve=resolve).text == "\nselect 1 from <ref:a> where x > (select max(x) from me)"


def test_render_maps_every_copied_character_to_its_raw_offset():
    raw = "{# c #}select\n  {{ source('s', 't') }} x"
    rendered = render(raw, incremental=False, resolve=resolve)
    assert rendered.text == "select\n  <source:s,t> x"
    assert rendered.raw_offset(0) == raw.index("select")
    assert rendered.raw_offset(rendered.text.index("<")) is None
    assert rendered.raw_offset(len(rendered.text) - 1) == len(raw) - 1


@pytest.mark.parametrize("raw", [
    "select {{ var('x') }}",
    "{% for x in y %}{% endfor %}",
    "{% if target.name == 'dev' %}x{% endif %}",
    "select {{ ref('a') | upper }}",
    "{% if is_incremental() %}x",
])
def test_render_refuses_anything_outside_its_subset(raw):
    with pytest.raises(Unrenderable):
        render(raw, incremental=False, resolve=resolve)
