"""Read the parts of a dbt `manifest.json` (and, opted into, `catalog.json`) that analysis needs.

Only the latest manifest schema is read (`SCHEMA`, written by dbt 1.8 through
1.12); any other is a `ManifestError`, rather than a different shape read as
if it were this one. Only plain values leave this module: no dbt import, no
Jinja. A field the manifest lacks reads as absent, never as a guess; a
manifest that is not a JSON object of nodes is a `ManifestError`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# The manifest schema this module reads: the latest dbt writes.
SCHEMA = "https://schemas.getdbt.com/dbt/manifest/v12.json"
# The catalog schema `dbt docs generate` writes.
CATALOG_SCHEMA = "https://schemas.getdbt.com/dbt/catalog/v1.json"


class ManifestError(Exception):
    """The manifest (or catalog) is missing, unreadable, or not shaped like one dbt writes."""


@dataclass(frozen=True)
class Constraint:
    """One model contract constraint, column-level (`columns` is that one
    column) or model-level. `to` is a `ref(...)`/`source(...)` call as written,
    `expression` the raw SQL of an older-style foreign key."""

    type: str
    columns: tuple[str, ...] = ()
    expression: str | None = None
    to: str | None = None
    to_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str | None = None


@dataclass(frozen=True)
class InjectedCte:
    """An ephemeral model dbt inlined into a node's compiled code: `sql` is
    the exact text it inserted (` __dbt__cte__name as (\\n...\\n)`)."""

    id: str
    sql: str


@dataclass(frozen=True)
class Relation:
    """A model, seed, snapshot, or source: anything a ref() or source() names."""

    unique_id: str
    resource_type: str
    name: str
    package_name: str
    relation_name: str | None
    original_file_path: str
    patch_path: str | None = None
    materialized: str | None = None
    language: str = "sql"
    raw_code: str = ""
    compiled_code: str | None = None
    compiled_path: str | None = None
    depends_on: tuple[str, ...] = ()
    columns: tuple[Column, ...] = ()
    constraints: tuple[Constraint, ...] = ()
    contract_enforced: bool = False
    incremental_strategy: str | None = None
    unique_key: tuple[str, ...] = ()
    # Whether `incremental_predicates` narrow which existing rows the merge matches.
    incremental_predicates: bool = False
    column_types: Mapping[str, str] = field(default_factory=dict)
    extra_ctes: tuple[InjectedCte, ...] = ()
    source_name: str | None = None
    version: str | None = None

    @property
    def label(self) -> str:
        return f"{self.source_name}.{self.name}" if self.resource_type == "source" else self.name


@dataclass(frozen=True)
class Test:
    """A data test. `test_name` and `kwargs` come from `test_metadata` and are
    empty for a singular test."""

    unique_id: str
    name: str
    original_file_path: str
    test_name: str | None = None
    namespace: str | None = None
    kwargs: Mapping[str, Any] = field(default_factory=dict)
    column_name: str | None = None
    attached_node: str | None = None
    depends_on: tuple[str, ...] = ()
    severity: str = "error"
    where: str | None = None
    fail_calc: str = "count(*)"
    warn_if: str = "!= 0"
    error_if: str = "!= 0"


@dataclass(frozen=True)
class Manifest:
    adapter_type: str | None
    project_name: str | None
    schema_version: str | None
    relations: Mapping[str, Relation]
    tests: Mapping[str, Test]


def load_manifest(path: Path) -> Manifest:
    try:
        document = json.loads(path.read_text())
    except OSError as error:
        raise ManifestError(f"cannot read {path}: {error.strerror or error}") from error
    except ValueError as error:
        raise ManifestError(f"{path} is not JSON: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("nodes"), dict):
        raise ManifestError(f"{path} is not a dbt manifest: it has no nodes")
    metadata = _mapping(document.get("metadata"))
    schema = _text(metadata.get("dbt_schema_version"))
    if schema != SCHEMA:
        raise ManifestError(
            f"{path} has manifest schema {schema or 'unknown'}; sqlassert-dbt reads only the latest, {SCHEMA}: "
            "compile with a current dbt"
        )
    relations: dict[str, Relation] = {}
    tests: dict[str, Test] = {}
    for unique_id, node in document["nodes"].items():
        if not isinstance(node, dict):
            continue
        match node.get("resource_type"):
            case "model" | "seed" | "snapshot":
                relations[unique_id] = _relation(unique_id, node)
            case "test":
                tests[unique_id] = _test(unique_id, node)
            case _:
                pass  # Analyses, operations and unit tests name no relation and state no Property.
    for unique_id, node in _mapping(document.get("sources")).items():
        if isinstance(node, dict):
            relations[unique_id] = _relation(unique_id, node)
    return Manifest(
        adapter_type=_text(metadata.get("adapter_type")),
        project_name=_text(metadata.get("project_name")),
        schema_version=schema,
        relations=relations,
        tests=tests,
    )


def _relation(unique_id: str, node: dict) -> Relation:
    config = _mapping(node.get("config"))
    columns = tuple(
        Column(name=str(column.get("name") or name), data_type=_text(column.get("data_type")))
        for name, column in _mapping(node.get("columns")).items()
        if isinstance(column, dict)
    )
    column_constraints = tuple(
        _constraint(constraint, (str(column.get("name") or name),))
        for name, column in _mapping(node.get("columns")).items()
        if isinstance(column, dict)
        for constraint in column.get("constraints") or ()
        if isinstance(constraint, dict)
    )
    model_constraints = tuple(
        _constraint(constraint, ()) for constraint in node.get("constraints") or () if isinstance(constraint, dict)
    )
    contract = _mapping(node.get("contract")) or _mapping(config.get("contract"))
    version = node.get("version")
    return Relation(
        unique_id=unique_id,
        resource_type=str(node.get("resource_type")),
        name=str(node.get("name")),
        package_name=str(node.get("package_name")),
        relation_name=_text(node.get("relation_name")),
        original_file_path=str(node.get("original_file_path") or ""),
        # `package://models/schema.yml`: the file a model's columns and tests are written in.
        patch_path=(_text(node.get("patch_path")) or "").partition("://")[2] or None,
        materialized=_text(config.get("materialized")),
        language=str(node.get("language") or "sql"),
        raw_code=str(node.get("raw_code") or node.get("raw_sql") or ""),
        compiled_code=_text(node.get("compiled_code")),
        compiled_path=_text(node.get("compiled_path")),
        depends_on=tuple(_mapping(node.get("depends_on")).get("nodes") or ()),
        columns=columns,
        constraints=column_constraints + model_constraints,
        contract_enforced=bool(contract.get("enforced")),
        incremental_strategy=_text(config.get("incremental_strategy")),
        unique_key=_names(config.get("unique_key")),
        incremental_predicates=bool(config.get("incremental_predicates")),
        column_types={str(key): str(value) for key, value in _mapping(config.get("column_types")).items()},
        extra_ctes=tuple(
            InjectedCte(id=str(cte.get("id")), sql=str(cte.get("sql")))
            for cte in node.get("extra_ctes") or ()
            if isinstance(cte, dict) and cte.get("sql") is not None
        ),
        source_name=_text(node.get("source_name")),
        version=None if version is None else str(version),
    )


def _constraint(constraint: dict, columns: tuple[str, ...]) -> Constraint:
    return Constraint(
        type=str(constraint.get("type")),
        columns=columns or _names(constraint.get("columns")),
        expression=_text(constraint.get("expression")),
        to=_text(constraint.get("to")),
        to_columns=_names(constraint.get("to_columns")),
    )


def _test(unique_id: str, node: dict) -> Test:
    config = _mapping(node.get("config"))
    metadata = _mapping(node.get("test_metadata"))
    return Test(
        unique_id=unique_id,
        name=str(node.get("name")),
        original_file_path=str(node.get("original_file_path") or ""),
        test_name=_text(metadata.get("name")),
        namespace=_text(metadata.get("namespace")),
        kwargs=_mapping(metadata.get("kwargs")),
        column_name=_text(node.get("column_name")),
        attached_node=_text(node.get("attached_node")),
        depends_on=tuple(_mapping(node.get("depends_on")).get("nodes") or ()),
        severity=str(config.get("severity") or "error").lower(),
        where=_text(config.get("where")),
        fail_calc=str(config.get("fail_calc") or "count(*)"),
        warn_if=str(config.get("warn_if") or "!= 0"),
        error_if=str(config.get("error_if") or "!= 0"),
    )


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _names(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


@dataclass(frozen=True)
class CatalogTable:
    """One relation as `dbt docs generate` found it in the warehouse: every
    column, in order, with its type as the warehouse writes it. No keys and
    no nullability: the catalog has neither."""

    unique_id: str
    database: str | None
    schema: str
    name: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Catalog:
    generated_at: str | None
    tables: Mapping[str, CatalogTable] = field(default_factory=dict)


def load_catalog(path: Path) -> Catalog:
    """The relations of a dbt `catalog.json`, by unique_id."""
    try:
        document = json.loads(path.read_text())
    except OSError as error:
        raise ManifestError(f"cannot read {path}: {error.strerror or error}") from error
    except ValueError as error:
        raise ManifestError(f"{path} is not JSON: {error}") from error
    if not isinstance(document, dict):
        raise ManifestError(f"{path} is not a dbt catalog")
    metadata = _mapping(document.get("metadata"))
    schema = _text(metadata.get("dbt_schema_version"))
    if schema != CATALOG_SCHEMA:
        raise ManifestError(f"{path} has catalog schema {schema or 'unknown'}; sqlassert-dbt reads only {CATALOG_SCHEMA}")
    tables: dict[str, CatalogTable] = {}
    for section in ("nodes", "sources"):
        for unique_id, entry in _mapping(document.get(section)).items():
            if not isinstance(entry, dict):
                continue
            where = _mapping(entry.get("metadata"))
            columns = sorted(
                (column for column in _mapping(entry.get("columns")).values() if isinstance(column, dict)),
                key=lambda column: column.get("index") if isinstance(column.get("index"), int) else 0,
            )
            if not columns or _text(where.get("schema")) is None or _text(where.get("name")) is None:
                continue
            tables[unique_id] = CatalogTable(
                unique_id, _text(where.get("database")), str(where["schema"]), str(where["name"]),
                tuple(Column(str(column.get("name")), _text(column.get("type"))) for column in columns),
            )
    return Catalog(_text(metadata.get("generated_at")), tables)
