"""The plugin over `tests/dbt_project` as `dbt compile` writes it (`compiled`
in conftest.py); a test that needs another manifest edits a copy of it."""

from __future__ import annotations

import ast
import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from sqlassert_dbt import DbtReport, ProjectError, Status, analyze_project, suggest_models
from sqlassert_dbt.expectations import expectations
from sqlassert_dbt.jinja import Unrenderable, align, render
from sqlassert_dbt.manifest import load_manifest
from sqlassert.ir.model import LiteralValue
from sqlassert.reporting import Outcome
from dbt.cli.main import dbtRunner
from dbt_runs import copy_source, dbt

from sqlassert_dbt import __main__ as dbt_command
from sqlassert_dbt.__main__ import main, proved_tests


@pytest.fixture(scope="module")
def report(compiled: Path) -> DbtReport:
    return analyze_project(compiled, trust_upstream_tests=True)


def statuses(report: DbtReport) -> dict[str, tuple[Status, str]]:
    return {test.name: (test.status, test.reason) for test in report.tests}


def markers(report: DbtReport) -> dict[str, Outcome]:
    return {f"{marker.location} {marker.marker}": marker.outcome for marker in report.markers}


@pytest.fixture
def copy_project(compiled: Path, tmp_path: Path) -> Callable[[], Path]:
    def copy() -> Path:
        shutil.copytree(compiled, tmp_path / "project")
        return tmp_path / "project"
    return copy


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


def test_upstream_tests_are_facts_only_when_trusted(compiled):
    report = analyze_project(compiled)
    found = statuses(report)
    assert found["unique_stg_customers_customer_id"][0] is Status.NOT_PROVED
    assert found["not_null_stg_customers_customer_id"][0] is Status.NOT_PROVED
    assert [problem.code for problem in report.problems if problem.code == "dbt-untrusted-upstream-tests"] == [
        "dbt-untrusted-upstream-tests"
    ]


def test_a_test_is_never_its_own_declaration(report):
    status, reason = statuses(report)["unique_stg_customers_first_name"]
    assert status is Status.NOT_PROVED
    assert "not proved from the SQL" in reason


def test_a_warn_severity_test_is_no_fact_downstream(report):
    # raw.orders' accepted_values test only warns, so stg_orders' own is not proved.
    assert statuses(report)["accepted_values_stg_orders_status__placed__shipped__returned"][0] is Status.NOT_PROVED


def test_an_error_severity_test_is_a_fact_downstream(copy_project):
    project = copy_project()
    name = "source_accepted_values_raw_orders_status__placed__shipped__returned"
    edit_manifest(project, lambda m: node_named(m, name)["config"].update(severity="ERROR"))
    found = next(e for e in expectations(load_manifest(project / "target" / "manifest.json")) if e.name == name)
    assert found.is_fact
    # raw.orders' column types are unknown, so its values are not known to be the literals as written.
    assert statuses(analyze_project(project, trust_upstream_tests=True))["accepted_values_stg_orders_status__placed__shipped__returned"][0] is Status.NOT_PROVED


def test_a_where_config_makes_a_test_no_fact(copy_project):
    project = copy_project()
    edit_manifest(project, lambda m: node_named(m, "source_unique_raw_customers_id")["config"].update(where="id > 0"))
    assert statuses(analyze_project(project, trust_upstream_tests=True))["unique_stg_customers_customer_id"][0] is Status.NOT_PROVED


def test_tables_and_relationships_are_left_to_the_warehouse(report):
    found = statuses(report)
    assert found["source_unique_raw_customers_id"] == (Status.NOT_PROVED, "a source is a table: only its own data shows this")
    assert found["not_null_raw_payments_id"][1] == "a seed is a table: only its own data shows this"
    # Referential Coverage is not carried through a view's projection.
    assert found["relationships_stg_orders_customer_id__customer_id__ref_stg_customers_"][0] is Status.NOT_PROVED


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


def test_incremental_predicates_leave_an_incremental_key_unproved(copy_project):
    # A predicate can keep an earlier row out of the merge's match, so its key is inserted twice.
    project = copy_project()
    edit_manifest(project, lambda m: node_named(m, "orders_incremental")["config"].update(
        incremental_predicates=["DBT_INTERNAL_DEST.order_id > 100"],
    ))
    found = statuses(analyze_project(project, trust_upstream_tests=True))
    status, reason = found["unique_orders_incremental_order_id"]
    assert status is Status.NOT_PROVED and "incremental_predicates" in reason
    assert found["not_null_orders_incremental_order_id"][0] is Status.PROVED


def test_both_incremental_branches_are_analyzed(report):
    marker = next(m for m in report.markers if m.model == "orders_incremental")
    assert (str(marker.location), marker.outcome) == ("models/orders_incremental.sql:8", Outcome.PROVED)
    # The ambiguous column is in the is_incremental() branch only, which dbt did not compile.
    assert any(
        problem.code == "ambiguous-column" and str(problem.location) == "models/orders_incremental.sql:6"
        for problem in report.problems
    )


def test_an_unrenderable_branch_leaves_its_model_unproved(copy_project):
    project = copy_project()

    def edit(manifest):
        node = node_named(manifest, "orders_incremental")
        node["raw_code"] = node["raw_code"].replace("{{ this }}", "{{ this }} where {{ var('x') }}")

    edit_manifest(project, edit)
    report = analyze_project(project, trust_upstream_tests=True)
    assert statuses(report)["unique_orders_incremental_order_id"][0] is Status.NOT_PROVED
    marker = next(m for m in report.markers if m.model == "orders_incremental")
    assert marker.outcome is Outcome.UNKNOWN and "is_incremental() branch was not analyzed" in marker.explanation
    assert any(problem.code == "dbt-unanalyzed-branch" for problem in report.problems)


# Markers in model files


def test_a_model_with_markers_and_no_compiled_code_fails_the_check(copy_project):
    project = copy_project()

    def edit(manifest):
        for name in ("active_customers", "stg_orders"):  # Markers; none.
            node_named(manifest, name).pop("compiled_code")

    edit_manifest(project, edit)
    report = analyze_project(project, trust_upstream_tests=True)
    unanalyzed = {problem.model: problem for problem in report.problems if problem.code == "dbt-unanalyzed-model"}
    assert unanalyzed["active_customers"].severity == "error" and "dbt compile" in unanalyzed["active_customers"].message
    assert unanalyzed["stg_orders"].severity == "warning"
    assert report.failed


def test_markers_map_back_to_the_model_file(report):
    assert markers(report) == {
        "models/active_customers.sql:4 /**ASSERT UNIQUE(customer_id)**/": Outcome.PROVED,
        "models/customer_orders.sql:6 /**ASSERT UNIQUE(customer_id)**/": Outcome.PROVED,
        "models/customers.sql:11 /**ASSERT MANY TO ONE**/": Outcome.UNKNOWN,
        "models/customers.sql:12 /**ASSERT NOT NULL(first_name)**/": Outcome.UNKNOWN,
        "models/orders_incremental.sql:8 /**ASSERT UNIQUE(order_id)**/": Outcome.PROVED,
    }


def test_markers_on_one_line_are_reported_each_as_written(tmp_path):
    project = compile_with(tmp_path, {
        "models/two_markers.sql": "select 1 as a, cast(null as int) as b\n/**ASSERT NOT NULL(a)**/ /**ASSERT NOT NULL(b)**/\n",
    })
    found = [
        (str(marker.location), marker.marker, marker.outcome)
        for marker in analyze_project(project, trust_upstream_tests=True).markers if marker.model == "two_markers"
    ]
    assert found == [
        ("models/two_markers.sql:2", "/**ASSERT NOT NULL(a)**/", Outcome.PROVED),
        ("models/two_markers.sql:2", "/**ASSERT NOT NULL(b)**/", Outcome.UNKNOWN),
    ]


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
    assert sorted(p.model for p in unknown if p.model) == ["raw.customers", "raw.orders", "raw.signups"]
    assert all(p.severity == "warning" for p in unknown)
    assert not any(p.code == "undeclared-table" for p in report.problems)
    assert not any(p.severity == "error" for p in report.problems)


def test_an_unsupported_adapter_is_not_guessed(copy_project):
    project = copy_project()
    edit_manifest(project, lambda m: m["metadata"].update(adapter_type="sqlserver"))
    report = analyze_project(project)
    assert [p.code for p in report.problems] == ["dbt-unsupported-dialect"]
    assert report.failed and not report.markers
    assert all(test.status is Status.NOT_PROVED for test in report.tests)
    assert analyze_project(project, dialect="duckdb").markers


def test_only_the_latest_manifest_schema_is_read(copy_project):
    project = copy_project()

    def edit(manifest):
        manifest["metadata"]["dbt_schema_version"] = "https://schemas.getdbt.com/dbt/manifest/v11.json"

    edit_manifest(project, edit)
    with pytest.raises(ProjectError, match="reads only the latest"):
        analyze_project(project)


def test_a_missing_manifest_is_a_project_error(tmp_path):
    with pytest.raises(ProjectError, match="dbt compile"):
        analyze_project(tmp_path)


# Command line


def test_command_exits_1_on_an_unproved_marker(compiled, capsys):
    assert main(["check", str(compiled)]) == 1
    out = capsys.readouterr().out
    assert "models/customers.sql:12: UNKNOWN /**ASSERT NOT NULL(first_name)**/" in out
    assert "Proved statically" in out and "schema.yml additions" in out


def test_command_exits_0_when_every_marker_is_proved(copy_project, capsys):
    project = copy_project()

    def edit(manifest):
        node = node_named(manifest, "customers")
        for text in ("/**ASSERT MANY TO ONE**/ ", "\n/**ASSERT NOT NULL(first_name)**/"):
            node["compiled_code"] = node["compiled_code"].replace(text, "")
            node["raw_code"] = node["raw_code"].replace(text, "")

    edit_manifest(project, edit)
    assert main(["check", str(project), "--trust-upstream-tests"]) == 0


def test_require_proved_fails_on_an_unproved_test(copy_project, capsys):
    project = copy_project()

    def edit(manifest):
        node = node_named(manifest, "customers")
        for text in ("/**ASSERT MANY TO ONE**/ ", "\n/**ASSERT NOT NULL(first_name)**/"):
            node["compiled_code"] = node["compiled_code"].replace(text, "")
            node["raw_code"] = node["raw_code"].replace(text, "")

    edit_manifest(project, edit)
    assert main(["check", str(project), "--trust-upstream-tests"]) == 0
    assert main(["check", str(project), "--trust-upstream-tests", "--require-proved"]) == 1


def test_command_exits_2_without_a_manifest(tmp_path, capsys):
    assert main(["check", str(tmp_path)]) == 2
    assert "manifest" in capsys.readouterr().err


def fake_dbt(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(arguments: list[str]) -> int:
        calls.append(arguments)
        return 0

    monkeypatch.setattr(dbt_command, "run_dbt", run)
    return calls


def test_build_compiles_checks_then_builds_without_the_proved_tests(copy_project, monkeypatch, capsys):
    project = copy_project()
    calls = fake_dbt(monkeypatch)

    code = main(["build", str(project), "--", "--target", "dev"])

    assert code == 1  # dbt passed; a marker is UNKNOWN
    assert "models/customers.sql:12: UNKNOWN /**ASSERT NOT NULL(first_name)**/" in capsys.readouterr().out
    compile_, build = calls
    # The project holds its profiles.yml, which dbt would not look for there.
    assert compile_ == ["compile", "--target", "dev", "--project-dir", str(project), "--profiles-dir", str(project.resolve())]
    excluded = build[build.index("--exclude") + 1:]
    assert build[:4] == ["build", "--target", "dev", "--project-dir"]
    # Upstream tests run in the same invocation, so they are facts.
    assert excluded == proved_tests(analyze_project(project, trust_upstream_tests=True))
    assert "unique_stg_customers_customer_id" in excluded


def test_a_selection_makes_upstream_tests_no_facts(copy_project, monkeypatch, capsys):
    project = copy_project()
    calls = fake_dbt(monkeypatch)

    main(["test", str(project), "--", "--select", "customers"])

    test = calls[1]
    assert test[0] == "test"
    excluded = test[test.index("--exclude") + 1:] if "--exclude" in test else []
    assert excluded == proved_tests(analyze_project(project))
    assert "unique_stg_customers_customer_id" not in excluded


def test_a_failing_dbt_run_is_the_exit_status(copy_project, monkeypatch, capsys):
    project = copy_project()
    monkeypatch.setattr(dbt_command, "run_dbt", lambda arguments: 0 if arguments[0] == "compile" else 7)

    assert main(["build", str(project)]) == 7


def test_dbt_takes_the_proved_tests_as_excluded(tmp_path, monkeypatch, capsys):
    """A real dbt build: the project has no raw data, so models fail, but every
    test dbt selected appears in run_results, and no excluded one does."""
    project = copy_source(tmp_path)
    log = str(project / "logs")
    excluded: list[str] = []

    def run(arguments: list[str]) -> int:
        if "--exclude" in arguments:
            excluded.extend(arguments[arguments.index("--exclude") + 1:])
        invoked = dbtRunner().invoke(["--quiet", "--no-send-anonymous-usage-stats", "--log-path", log, *arguments])
        return 0 if invoked.success else 1

    monkeypatch.setattr(dbt_command, "run_dbt", run)

    main(["build", str(project), "--", "--profiles-dir", str(project)])

    results = json.loads((project / "target" / "run_results.json").read_text())["results"]
    ran = {result["unique_id"].split(".")[2] for result in results if result["unique_id"].startswith("test.")}
    assert "unique_stg_customers_customer_id" in excluded
    assert ran and not ran & set(excluded)


# Expectations


def manifest_with_test(copy_project: Callable[[], Path], name: str, kwargs: dict, config: dict | None = None, namespace: str | None = None):
    project = copy_project()

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
def test_only_a_test_that_fails_on_any_failing_row_is_a_fact(copy_project, config, fact):
    assert manifest_with_test(copy_project, "unique", {"column_name": "customer_id"}, config).is_fact is fact


@pytest.mark.parametrize(("config", "provable"), [({}, True), ({"warn_if": "< 1"}, False), ({"fail_calc": "sum(n)"}, False)])
def test_a_test_is_provable_only_when_zero_failing_rows_pass_it(copy_project, config, provable):
    expectation = manifest_with_test(copy_project, "unique", {"column_name": "customer_id"}, config)
    assert (expectation.unprovable is None) is provable


def test_unique_combination_is_proved_by_a_non_null_unique_set_and_declares_a_unique_set(copy_project):
    expectation = manifest_with_test(
        copy_project, "unique_combination_of_columns", {"combination_of_columns": ["customer_id", "first_name"]},
        namespace="dbt_utils",
    )
    assert (expectation.asserted, expectation.declared, expectation.columns) == ("key", "unique", ("customer_id", "first_name"))


@pytest.mark.parametrize(("quote", "values"), [
    (True, (LiteralValue("string", "1"), LiteralValue("string", "a"))),
    (False, None),
])
def test_accepted_values_are_strings_unless_unquoted(copy_project, quote, values):
    expectation = manifest_with_test(copy_project, "accepted_values", {"column_name": "first_name", "values": [1, "a"], "quote": quote})
    assert (expectation.values or None) == values


def compile_with(tmp_path: Path, files: dict[str, str]) -> Path:
    """`tests/dbt_project` with `files` added, compiled by dbt."""
    project = copy_source(tmp_path)
    for name, text in files.items():
        (project / name).write_text(text)
    assert dbt(project, "compile"), "dbt compile failed"
    return project


def test_accepted_values_declare_a_domain_only_of_a_column_they_compare_by_identity(tmp_path):
    # DuckDB casts '01' to the integer column, so 1 passes `n in ('01')`: n's values are not '01'.
    project = compile_with(tmp_path, {
        "models/codes.sql": (
            "{{ config(materialized='incremental', on_schema_change='fail') }}\n"
            "select id as n, cast(amount as varchar) as code from {{ ref('raw_payments') }}\n"
        ),
        "models/codes_used.sql": (
            "select n, code\nfrom {{ ref('codes') }}\n/**ASSERT ENUM(n IN ('01'))**/\n/**ASSERT ENUM(code IN ('a'))**/\n"
        ),
        "models/codes.yml": """version: 2
models:
  - name: codes
    config:
      contract: {enforced: true}
    columns:
      - name: n
        data_type: integer
        tests:
          - accepted_values:
              arguments: {values: ['01']}
      - name: code
        data_type: varchar
        tests:
          - accepted_values:
              arguments: {values: ['a']}
""",
    })
    assert {
        marker.marker: marker.outcome for marker in analyze_project(project, trust_upstream_tests=True).markers
        if marker.model == "codes_used"
    } == {"/**ASSERT ENUM(n IN ('01'))**/": Outcome.UNKNOWN, "/**ASSERT ENUM(code IN ('a'))**/": Outcome.PROVED}


def test_unquoted_accepted_values_are_sql_literals(copy_project):
    expectation = manifest_with_test(copy_project, "accepted_values", {"column_name": "first_name", "values": [1, True], "quote": False})
    assert expectation.values == (LiteralValue("numeric", "1"), LiteralValue("boolean", "TRUE"))


def test_a_test_of_an_expression_states_no_property(copy_project):
    expectation = manifest_with_test(copy_project, "unique", {"column_name": "lower(first_name)"})
    assert expectation.asserted is None and "expression" in (expectation.unprovable or "")


@pytest.mark.parametrize(("adapter", "materialized", "enforced", "fact"), [
    ("duckdb", "table", True, True),
    ("duckdb", "view", True, False),
    ("duckdb", "table", False, False),
    ("snowflake", "table", True, False),
])
def test_a_primary_key_constraint_is_a_fact_only_where_it_is_enforced(copy_project, adapter, materialized, enforced, fact):
    project = copy_project()

    def edit(manifest):
        manifest["metadata"]["adapter_type"] = adapter
        node = node_named(manifest, "stg_customers")
        node["config"]["materialized"] = materialized
        node["contract"]["enforced"] = enforced
        node["constraints"] = [{"type": "primary_key", "columns": ["customer_id"], "name": None, "expression": None,
                                "warn_unenforced": True, "warn_unsupported": True, "to": None, "to_columns": []}]

    edit_manifest(project, edit)
    found = [e for e in expectations(load_manifest(project / "target" / "manifest.json")) if e.kind == "constraint" and e.node.endswith(".stg_customers")]
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


def test_align_maps_text_a_macro_wrote_to_the_macro_tag():
    raw = "with u as (\n    select id from {{ scoped() }}\n),\nv as (select 1)\nselect * from u"
    compiled = "with u as (\n    select id from \n(select id\n from p)\n),\nv as (select 1)\nselect * from u"
    sources = align(raw, compiled, incremental=False)
    assert sources is not None and len(sources) == len(compiled)
    tag = raw.index("{{")
    # Every character of the macro's two lines comes from its tag; the rest is copied.
    assert {sources[i] for i in range(compiled.index("\n(select"), compiled.index("\n),"))} == {tag}
    assert sources[compiled.index("v as")] == raw.index("v as")
    assert sources[-1] == len(raw) - 1


def test_align_takes_a_loop_or_a_set_as_one_tag():
    raw = "{% set n = 2 %}\nselect\n{%- for c in cols %} {{ c }},{% endfor %} 1 from t"
    compiled = "\nselect a, b, 1 from t"
    sources = align(raw, compiled, incremental=False)
    assert sources is not None
    assert sources[compiled.index("a,")] == raw.index("{%- for")
    assert sources[compiled.index("1 from")] == raw.index("1 from")


def test_align_follows_the_incremental_branch_dbt_took():
    raw = "select {{ m() }} from t\n{% if is_incremental() %}where x > 1{% else %}-- full{% endif %}"
    assert align(raw, "select 7 from t\nwhere x > 1", incremental=True) is not None
    assert align(raw, "select 7 from t\nwhere x > 1", incremental=False) is None


def test_align_refuses_compiled_code_the_raw_text_does_not_appear_in():
    assert align("select {{ m() }} from t", "select 1 from u", incremental=False) is None


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


# Packaging


def test_the_plugin_reads_the_manifest_without_importing_dbt():
    # dbt compiles the project; checking it needs only the manifest, so dbt is a test dependency.
    # Only `--connect` imports dbt, inside the function that opens its connection.
    for path in (Path(__file__).parents[1] / "sqlassert_dbt").rglob("*.py"):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(name.split(".")[0] in {"dbt", "dbt_common"} for name in names), path


def test_an_incremental_batch_reads_its_own_previous_rows_without_its_tests(tmp_path):
    # The incremental branch reads {{ this }}: the rows before this run, which
    # its own tests (checked after the run) say nothing about. Were they facts
    # of what the batch reads, unique(order_id) would prove itself.
    project = compile_with(tmp_path, {
        "models/reloaded.sql": (
            "{{ config(materialized='incremental', unique_key='order_id', incremental_strategy='merge') }}\n"
            "{% if is_incremental() %}\n"
            "select order_id from {{ this }}\n"
            "{% else %}\n"
            "select order_id from {{ ref('stg_orders') }}\n"
            "{% endif %}\n"
        ),
        "models/reloaded.yml": (
            "version: 2\nmodels:\n  - name: reloaded\n    columns:\n      - name: order_id\n        tests: [unique]\n"
        ),
    })
    report = analyze_project(project, trust_upstream_tests=True)
    status, reason = statuses(report)["unique_reloaded_order_id"]
    assert status is Status.NOT_PROVED and reason == "its Property is not proved from the SQL"
    # Its previous run is unknown by nature, never a table with unknown columns to declare.
    assert not any("reloaded" in problem.message for problem in report.problems if problem.code == "dbt-unknown-columns")


def test_a_model_that_does_not_parse_is_left_out_alone(tmp_path):
    project = compile_with(tmp_path, {
        "models/broken.sql": "select order_id from {{ ref('stg_orders') }} where (\n",
        "models/natural.sql": "select * from {{ ref('stg_orders') }} natural join {{ ref('stg_customers') }}\n",
        "models/reads_broken.sql": "select order_id from {{ ref('broken') }}\n",
    })
    report = analyze_project(project, trust_upstream_tests=True)
    by_model = {(problem.model, problem.code) for problem in report.problems}
    assert ("broken", "sql-parse-failed") in by_model
    assert ("natural", "unsupported-natural-join") in by_model
    # Everything else is analyzed as before; what reads it sees a table of unknown columns.
    assert statuses(report)["unique_stg_customers_customer_id"][0] is Status.PROVED
    assert ("broken", "dbt-unknown-columns") in by_model
    suggested = suggest_models(project, ["models"], trust_upstream_tests=True)
    assert any(item.model == "customer_orders" for item in suggested.suggestions)
