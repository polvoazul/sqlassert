"""`--connect` and `analyze_project(fetcher=...)`: the tables models read come
from the warehouse, keys and NOT NULLs included, through dbt's own
connection (`connect.py`) or a fetcher the caller passes."""

from __future__ import annotations

import subprocess
from pathlib import Path

import duckdb
import pytest
from dbt_runs import copy_source

from sqlassert.discovery.duckdb import DuckDBKnowledgeFetcher
from sqlassert_dbt import Status, analyze_project
from sqlassert_dbt.__main__ import main
from sqlassert_dbt.connect import ConnectError, dbt_fetcher

# The raw tables with the keys, NOT NULLs and foreign key the warehouse states.
KEYED_TABLES = (
    "create table raw.customers (id integer primary key, first_name varchar not null)",
    "create table raw.orders (id integer primary key, user_id integer not null references raw.customers (id), "
    "status varchar, amount decimal(10, 2))",
    "create table raw.signups (id integer, code varchar)",
)


@pytest.fixture(scope="module")
def keyed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("keyed")
    project = copy_source(directory)
    database = directory / "warehouse.duckdb"
    profiles = project / "profiles.yml"
    profiles.write_text(profiles.read_text().replace('":memory:"', f'"{database}"'))
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        for statement in KEYED_TABLES:
            connection.execute(statement)
    # In a separate process: dbt-duckdb keeps the file open in the process that ran it,
    # and `suggest --connect` runs dbt compile as another process, as the CLI does.
    for command in ("seed", "run", "compile"):
        ran = subprocess.run(
            ["dbt", "--quiet", "--no-send-anonymous-usage-stats", command, "--project-dir", str(project), "--profiles-dir", str(project)],
            capture_output=True, text=True, check=False,
        )
        assert ran.returncode == 0, f"dbt {command} failed: {ran.stdout}{ran.stderr}"
    return project


def statuses(report) -> dict[str, Status]:
    return {test.name: test.status for test in report.tests}


def test_the_warehouse_keys_prove_the_tests_a_snapshot_cannot(keyed: Path):
    without = statuses(analyze_project(keyed, read_catalog=False))
    with dbt_fetcher(keyed, []) as fetcher:
        connected = statuses(analyze_project(keyed, fetcher=fetcher))

    for name in ("unique_stg_customers_customer_id", "not_null_stg_customers_customer_id", "unique_stg_orders_order_id"):
        assert without[name] is not Status.PROVED
        assert connected[name] is Status.PROVED
    # Only proofs are added.
    assert all(connected[name] is Status.PROVED for name, status in without.items() if status is Status.PROVED)


def test_a_fetcher_from_python_reads_the_same_warehouse(keyed: Path):
    database = keyed.parent / "warehouse.duckdb"
    with duckdb.connect(str(database)) as connection:
        report = analyze_project(keyed, fetcher=DuckDBKnowledgeFetcher(connection))

    assert statuses(report)["unique_stg_customers_customer_id"] is Status.PROVED
    assert "dbt-unknown-columns" not in [problem.code for problem in report.problems]


def test_an_incremental_models_previous_run_is_not_reported_missing(keyed: Path):
    with dbt_fetcher(keyed, []) as fetcher:
        report = analyze_project(keyed, fetcher=fetcher)

    assert "catalog-relation-not-found" not in [problem.code for problem in report.problems]


def test_suggest_connects_through_dbt(keyed: Path, capfd: pytest.CaptureFixture[str]):
    assert main(["suggest", "--connect", str(keyed / "models" / "stg_orders.sql")]) == 0

    assert "/**ASSERT NOT NULL(customer_id)**/" in capfd.readouterr().out


def test_an_unknown_target_is_a_usage_error(keyed: Path):
    with pytest.raises(ConnectError, match="--connect"):
        with dbt_fetcher(keyed, ["--target", "nowhere"]):
            pass
