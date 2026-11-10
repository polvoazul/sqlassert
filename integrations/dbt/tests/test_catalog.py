"""`--catalog`: the columns and types `dbt docs generate` found complete the
tables the manifest leaves unknown, unless the project shows them stale.
`built` (conftest.py) is the project built into a real warehouse."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from sqlassert_dbt import DbtReport, ProjectError, Status, analyze_project, suggest_models
from sqlassert_dbt.__main__ import main
from sqlassert.reporting import Outcome


def codes(report) -> list[str]:
    return [problem.code for problem in report.problems]


def statuses(report: DbtReport) -> dict[str, Status]:
    return {test.name: test.status for test in report.tests}


def outcome(report: DbtReport, marker: str) -> Outcome:
    return next(found.outcome for found in report.markers if f"{found.location} {found.marker}" == marker)


@pytest.fixture
def copy_built(built: Path, tmp_path: Path) -> Callable[[], Path]:
    def copy() -> Path:
        shutil.copytree(built, tmp_path / "project")
        return tmp_path / "project"
    return copy


def edit_catalog(project: Path, edit) -> None:
    path = project / "target" / "catalog.json"
    catalog = json.loads(path.read_text())
    edit(catalog)
    path.write_text(json.dumps(catalog))


MARKER = "models/customers.sql:11 /**ASSERT MANY TO ONE**/"


def test_the_catalog_completes_the_columns_of_sources(built):
    without = analyze_project(built, trust_upstream_tests=True)
    report = analyze_project(built, trust_upstream_tests=True, catalog_path=built / "target" / "catalog.json")
    # SELECT * of stg_customers over raw.customers now has known columns, so
    # the join's key is no longer ambiguous.
    assert outcome(without, MARKER) is Outcome.UNKNOWN
    assert outcome(report, MARKER) is Outcome.PROVED
    assert statuses(report)["unique_customers_customer_id"] is Status.PROVED
    assert "dbt-unknown-columns" in codes(without) and "dbt-unknown-columns" not in codes(report)


def test_the_catalog_types_a_column_so_a_test_declares_its_domain(built):
    report = analyze_project(built, trust_upstream_tests=True, catalog_path=built / "target" / "catalog.json")
    # stg_orders' accepted_values on status is a fact once status is known to be text.
    assert {(item.model, item.test) for item in report.untested} >= {
        ("orders_incremental", "accepted_values: {values: ['placed', 'returned', 'shipped']}"),
    }


def test_suggest_reads_the_catalog(built):
    report = suggest_models(
        built, ["models/customers.sql"], trust_upstream_tests=True, catalog_path=built / "target" / "catalog.json",
    )
    assert "dbt-unknown-columns" not in codes(report)


def test_a_catalog_of_another_target_is_not_used(copy_built):
    project = copy_built()

    def edit(catalog):
        for section in ("nodes", "sources"):
            for entry in catalog[section].values():
                entry["metadata"]["database"] = "prod"

    edit_catalog(project, edit)
    report = analyze_project(project, trust_upstream_tests=True, catalog_path=project / "target" / "catalog.json")
    assert outcome(report, MARKER) is Outcome.UNKNOWN
    other = next(problem for problem in report.problems if problem.code == "dbt-catalog-other-relation")
    assert "another target" in other.message and "prod." in other.message
    assert "dbt-unknown-columns" in codes(report)


def test_a_seed_whose_csv_changed_since_is_not_read_from_the_catalog(copy_built):
    project = copy_built()
    seed = project / "seeds" / "raw_payments.csv"
    seed.write_text(seed.read_text().replace("id,order_id,amount", "id,order_id,amount,currency").replace("1,1,10", "1,1,10,EUR"))
    report = analyze_project(project, trust_upstream_tests=True, catalog_path=project / "target" / "catalog.json")
    stale = next(problem for problem in report.problems if problem.code == "dbt-catalog-stale")
    assert "seed raw_payments" in stale.message and "currency" in stale.message


def test_an_enforced_contract_wins_over_the_catalog(copy_built):
    project = copy_built()

    def edit(catalog):
        entry = catalog["nodes"]["model.jaffle.customer_keys"]
        entry["columns"]["extra"] = {"type": "INTEGER", "index": 3, "name": "extra", "comment": None}

    edit_catalog(project, edit)
    report = analyze_project(project, trust_upstream_tests=True, catalog_path=project / "target" / "catalog.json")
    stale = next(problem for problem in report.problems if problem.code == "dbt-catalog-stale")
    assert "model customer_keys" in stale.message and "the contract is used" in stale.message


def test_an_unreadable_catalog_is_a_project_error(built, tmp_path):
    (tmp_path / "catalog.json").write_text('{"metadata": {"dbt_schema_version": "https://schemas.getdbt.com/dbt/catalog/v0.json"}}')
    with pytest.raises(ProjectError, match="catalog schema"):
        analyze_project(built, catalog_path=tmp_path / "catalog.json")
    with pytest.raises(ProjectError, match="cannot read"):
        analyze_project(built, catalog_path=tmp_path / "missing.json")


def test_check_takes_a_catalog(built, capsys):
    main(["check", str(built), "--trust-upstream-tests", "--catalog", str(built / "target" / "catalog.json")])
    assert "models/customers.sql:11: PROVED /**ASSERT MANY TO ONE**/" in capsys.readouterr().out
    assert main(["check", str(built), "--catalog", str(built / "missing.json")]) == 2
