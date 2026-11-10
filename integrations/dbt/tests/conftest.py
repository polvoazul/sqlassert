"""`compiled`: `tests/dbt_project` compiled by a real `dbt compile`, once per
session. `built`: the same project built into a DuckDB file whose raw tables
exist, with the `target/catalog.json` a real `dbt docs generate` writes. Every
test reads what dbt writes, never a hand-written manifest or catalog."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
from dbt_runs import copy_source, dbt


@pytest.fixture(scope="session")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = copy_source(tmp_path_factory.mktemp("compiled"))
    assert dbt(project, "compile"), "dbt compile failed on tests/dbt_project"
    return project


# The raw tables the project's sources name, as the warehouse holds them.
RAW_TABLES = (
    "create table raw.customers (id integer, first_name varchar)",
    "create table raw.orders (id integer, user_id integer, status varchar, amount decimal(10, 2))",
    "create table raw.signups (id integer, code varchar)",
)


@pytest.fixture(scope="session")
def built(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("built")
    project = copy_source(directory)
    database = directory / "warehouse.duckdb"
    profiles = project / "profiles.yml"
    profiles.write_text(profiles.read_text().replace('":memory:"', f'"{database}"'))
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        for statement in RAW_TABLES:
            connection.execute(statement)
    for command in ("seed", "run", "docs generate"):
        assert dbt(project, *command.split()), f"dbt {command} failed on tests/dbt_project"
    return project
