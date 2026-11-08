"""Model contract constraints against dbt itself: the manifest shape `dbt
compile` writes for them, the adapter table they are read through, and that
the warehouse (DuckDB) really enforces what the plugin takes as a fact."""

from __future__ import annotations

from pathlib import Path

import pytest
from dbt_runs import copy_source, dbt
from dbt.adapters.base.impl import ConstraintSupport
from dbt.adapters.duckdb.impl import DuckDBAdapter
from dbt_common.contracts.constraints import ConstraintType

from sqlassert_dbt import Status, analyze_project
from sqlassert_dbt.expectations import _ENFORCED_CONSTRAINTS


def statuses(project: Path) -> dict[str, Status]:
    return {test.name: test.status for test in analyze_project(project).tests}


def test_an_enforced_contract_proves_the_tests_downstream(compiled):
    found = statuses(compiled)
    # raw.signups has no tests: only customer_keys' constraints state its keys.
    for test in ("unique_customer_signups_customer_id", "not_null_customer_signups_customer_id",
                 "unique_customer_signups_signup_code", "not_null_customer_signups_signup_code"):
        assert found[test] is Status.PROVED, test


def test_without_an_enforced_contract_the_constraints_are_no_facts(tmp_path):
    project = copy_source(tmp_path)
    schema = project / "models" / "schema.yml"
    schema.write_text(schema.read_text().replace("contract: {enforced: true}", "contract: {enforced: false}"))
    assert dbt(project, "compile")
    found = statuses(project)
    assert found["unique_customer_signups_customer_id"] is Status.NOT_PROVED
    assert found["unique_customer_signups_signup_code"] is Status.NOT_PROVED


def test_the_duckdb_constraint_table_is_dbt_duckdbs():
    enforced = {
        kind.value for kind, support in DuckDBAdapter.CONSTRAINT_SUPPORT.items()
        if support is ConstraintSupport.ENFORCED and kind is not ConstraintType.check  # a check states no Property
    }
    assert _ENFORCED_CONSTRAINTS["duckdb"] == enforced


@pytest.mark.parametrize(("rows", "builds"), [("(1, 'a'), (2, 'b')", True), ("(1, 'a'), (1, 'b')", False)])
def test_duckdb_enforces_a_contracted_primary_key(tmp_path, rows, builds):
    project = copy_source(tmp_path)
    (project / "models" / "customer_keys.sql").write_text(
        "{{ config(materialized='table') }}\n"
        f"select * from (values {rows}) as t(customer_id, signup_code)\n"
    )
    assert dbt(project, "run", "--select", "customer_keys") is builds
