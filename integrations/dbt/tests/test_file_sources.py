"""Sources that read files (dbt-duckdb `external_location`): dbt pastes the read,
`read_csv('…/people.csv', header = true)` or `'…/kinds.csv'`, where a
table name would go. The plugin names each such source after its file, so it
can be declared like a table and given the columns a catalog lists for it."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from dbt_runs import copy_source, dbt

from sqlassert_dbt import analyze_project
from sqlassert_dbt.manifest import load_manifest
from sqlassert.reporting import Outcome

SOURCES = """version: 2
sources:
  - name: files
    meta: {external_location: "DATA/{identifier}.csv"}
    tables:
      - name: kinds
        columns:
          - name: id
            data_tests: [unique, not_null]
      - name: people
        meta: {external_location: "read_csv('DATA/people.csv', header = true)"}
"""
MODEL = (
    "select people.id, kinds.name from {{ source('files', 'people') }} as people\n"
    "/**ASSERT MANY TO ONE**/ left join {{ source('files', 'kinds') }} as kinds on people.kind_id = kinds.id\n"
)
# An `external` model writes its rows to a file each run; readers read its query.
EXTERNAL = (
    "{{ config(materialized='external', location='KINDS_PARQUET') }}\n"
    "select id, name from {{ source('files', 'kinds') }}\n"
)
THROUGH_EXTERNAL = (
    "select people.id, kinds.name from {{ source('files', 'people') }} as people\n"
    "/**ASSERT MANY TO ONE**/ left join {{ ref('kinds_external') }} as kinds on people.kind_id = kinds.id\n"
)
MARKER = "models/people_kinds.sql:2 /**ASSERT MANY TO ONE**/"
EXTERNAL_MARKER = "models/people_external_kinds.sql:2 /**ASSERT MANY TO ONE**/"
COLUMNS = {
    "source.jaffle.files.kinds": [("id", "INTEGER"), ("name", "VARCHAR")],
    "source.jaffle.files.people": [("id", "INTEGER"), ("kind_id", "INTEGER")],
}


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("files")
    project = copy_source(directory)
    (project / "data").mkdir()
    (project / "data" / "kinds.csv").write_text("id,name\n1,a\n2,b\n")
    (project / "data" / "people.csv").write_text("id,kind_id\n10,1\n11,2\n")
    # dbt-duckdb reads relative paths from the working directory; these are absolute.
    (project / "models" / "files.yml").write_text(SOURCES.replace("DATA", str(project / "data")))
    (project / "models" / "people_kinds.sql").write_text(MODEL)
    (project / "models" / "kinds_external.sql").write_text(EXTERNAL.replace("KINDS_PARQUET", str(directory / "kinds.parquet")))
    (project / "models" / "people_external_kinds.sql").write_text(THROUGH_EXTERNAL)
    database = directory / "warehouse.duckdb"
    profiles = project / "profiles.yml"
    profiles.write_text(profiles.read_text().replace('":memory:"', f'"{database}"'))
    with duckdb.connect(str(database)) as connection:
        connection.execute("create schema raw")
        for table in ("customers (id integer, first_name varchar)",
                      "orders (id integer, user_id integer, status varchar, amount decimal(10, 2))",
                      "signups (id integer, code varchar)"):
            connection.execute(f"create table raw.{table}")
    for command in ("seed", "run", "docs generate"):
        assert dbt(project, *command.split()), f"dbt {command} failed"
    return project


def outcome(report, marker: str) -> Outcome:
    return next(found.outcome for found in report.markers if f"{found.location} {found.marker}" == marker)


def test_a_file_source_is_named_after_its_file(project):
    relations = load_manifest(project / "target" / "manifest.json").relations
    assert relations["source.jaffle.files.people"].relation_name == "__file_people"
    assert relations["source.jaffle.files.kinds"].relation_name == "__file_kinds"
    compiled = relations["model.jaffle.people_kinds"].compiled_code or ""
    assert "from __file_people as people" in compiled and "join __file_kinds as kinds" in compiled
    # One line each, as dbt wrote them: marker lines still match the model file.
    written = json.loads((project / "target" / "manifest.json").read_text())["nodes"]["model.jaffle.people_kinds"]
    assert compiled.count("\n") == written["compiled_code"].count("\n")


def test_without_columns_a_file_source_warns_with_its_read(project):
    report = analyze_project(project, trust_upstream_tests=True)
    assert outcome(report, MARKER) is Outcome.UNKNOWN
    unknown = [problem.message for problem in report.problems if problem.code == "dbt-unknown-columns"]
    assert any("read_csv(" in message and "people.csv" in message for message in unknown)


def test_catalog_columns_of_a_file_source_prove_its_join(project, tmp_path):
    catalog = json.loads((project / "target" / "catalog.json").read_text())
    # dbt docs generate lists no file source; give them entries as base facts would.
    for unique_id, columns in COLUMNS.items():
        catalog["sources"][unique_id] = {
            "metadata": {"type": "BASE TABLE", "schema": "main", "name": unique_id.rsplit(".", 1)[1], "database": "warehouse"},
            "columns": {name: {"type": kind, "index": index, "name": name} for index, (name, kind) in enumerate(columns, 1)},
            "stats": {}, "unique_id": unique_id,
        }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    report = analyze_project(project, trust_upstream_tests=True, catalog_path=path)
    assert outcome(report, MARKER) is Outcome.PROVED
    assert outcome(report, EXTERNAL_MARKER) is Outcome.PROVED
    assert "dbt-catalog-other-relation" not in [problem.code for problem in report.problems]
