"""Compile a vendored dbt project offline and analyze its manifest with sqlassert."""

from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

from sqlglot import ErrorLevel, exp, parse
from sqlglot.errors import SqlglotError
from yaml import safe_dump, safe_load

from sqlassert_dbt.expectations import Expectation, expectations
from sqlassert_dbt.manifest import Catalog, CatalogTable, Column, load_manifest
from sqlassert import ir
from sqlassert.properties import NonNullColumn, NonNullColumns, UniqueSet
from sqlassert.python_engine import Engine
from sqlassert.rules.join_cardinality import many_to_one_join, one_to_many_join
from sqlassert_dbt.project import _analysis


# Neither profile reaches a database server. DuckDB introspection queries an empty in-memory
# database, which some projects need to compile; other adapters compile with
# --no-introspect --no-populate-cache, so their placeholder connection is never opened.
OFFLINE_PROFILES = {
    "duckdb": {"type": "duckdb", "path": ":memory:"},
    "snowflake": {"type": "snowflake", "account": "offline", "user": "offline", "password": "offline",
                  "database": "offline", "warehouse": "offline", "schema": "offline"},
}


def run_dbt(project, output, env, step, *options):
    command = ["dbt", step, "--project-dir", str(project), "--profiles-dir", str(output),
               "--target", "audit", "--log-path", str(output / "logs"), *options]
    with (output / f"{step}.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, env=os.environ | env)
    if result.returncode:
        lines = (output / f"{step}.log").read_text().splitlines()
        marker = next((i for i, line in enumerate(lines) if line.endswith("Encountered an error:")), None)
        detail = " ".join(line.strip() for line in lines[marker + 1:marker + 3]) if marker is not None else ""
        raise RuntimeError(f"dbt {step} failed: {detail}".rstrip(": "))


def join_statistics(models, dialect, analysis):
    counts = Counter()
    skipped = {}
    cardinalities = []
    base_tables = {"tables": 0, "typed": 0, "keyed": 0, "not_null": 0, "columns": 0, "typed_columns": 0}
    model_ids = {node["unique_id"] for node in models}
    builder = analysis._builder(dialect, []) if dialect else None
    if builder:
        builder.build(relation for key, relation in analysis.manifest.relations.items() if key in model_ids)
    conversion = builder._conversion if builder else None
    if conversion:
        program = conversion.program
        reachable = set(program.nodes())
        facts = (*program.declarations, *conversion.knowledge)
        non_null = {fact.column for fact in facts if isinstance(fact, NonNullColumn)}
        non_null |= {column for fact in facts if isinstance(fact, NonNullColumns) for column in fact.columns}
        unique_sets = [fact.columns for fact in facts if isinstance(fact, UniqueSet) and fact.columns]
        # Base tables are the relations analysis reads without SQL: sources, seeds and tables.
        for table in {node for node in reachable if isinstance(node, ir.NamedRelation) and node.role is ir.RelationRole.TABLE}:
            columns = set(table.output_columns)
            typed = sum(column.comparison_type is not None for column in columns)
            base_tables["tables"] += 1
            base_tables["columns"] += len(columns)
            base_tables["typed_columns"] += typed
            base_tables["typed"] += bool(columns) and typed == len(columns)
            base_tables["keyed"] += any(key <= columns for key in unique_sets)
            base_tables["not_null"] += bool(columns & non_null)
        with Engine().session(program, conversion.knowledge):
            for model_id in sorted(model_ids):
                for branch, statement in enumerate(builder._statements.get(model_id, [])):
                    seen = set()
                    for site, join in program.attachment_sites:
                        if not isinstance(join, ir.Join) or site.trailing or join in seen or not statement.has_site(site):
                            continue
                        offset = site.offset - statement.first_offset
                        if statement.body.in_injected(offset):
                            continue
                        seen.add(join)
                        kind = "UNKNOWN"
                        if join.kind in {"semi", "anti"}:
                            kind = "NOT_APPLICABLE"
                        elif join in reachable:
                            left_once = many_to_one_join(join).value
                            right_once = one_to_many_join(join).value
                            kind = {
                                (True, True): "ONE_TO_ONE", (True, False): "MANY_TO_ONE",
                                (False, True): "ONE_TO_MANY", (False, False): "UNKNOWN",
                            }[bool(left_once), bool(right_once)]
                        cardinalities.append({
                            "model": model_id, "branch": branch, "compiled_line": statement.local(site.line),
                            "cardinality": kind,
                        })
    for node in models:
        sql = node.get("pre_injected_sql") or node.get("compiled_code")
        if not sql or not dialect:
            skipped[node["unique_id"]] = "Missing compiled SQL or supported dialect"
            continue
        try:
            statements = parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
        except (SqlglotError, ValueError) as error:
            skipped[node["unique_id"]] = str(error)
            continue
        for statement in filter(None, statements):
            for join in statement.find_all(exp.Join):
                parts = [join.method, join.side, join.kind]
                if join.kind == "OUTER" and join.side:
                    parts.pop()
                kind = " ".join(filter(None, parts)) or ("INNER" if join.args.get("on") or join.args.get("using") else "UNQUALIFIED")
                counts[kind] += 1
    total = counts.total()
    cardinality_counts = Counter(join["cardinality"] for join in cardinalities)
    cardinality_total = cardinality_counts.total()
    return base_tables, {
        "total": total,
        "by_type": {kind: {"count": count, "percent": round(100 * count / total, 2)}
                    for kind, count in sorted(counts.items())},
        "skipped_models": skipped,
        "cardinality_total": cardinality_total,
        "by_cardinality": {
            kind: {"count": count, "percent": round(100 * count / cardinality_total, 2)}
            for kind, count in sorted(cardinality_counts.items())
        },
        "cardinality_sites": cardinalities,
    }


def load_seed_facts(path, project, manifest):
    facts = []
    tables = {}
    for node_id, entry in safe_load(path.read_text())["seeds"].items():
        node = manifest["nodes"].get(node_id)
        if not node or node["resource_type"] != "seed":
            raise ValueError(f"{path}: {node_id} is not a manifest seed")
        csv_path = project / node["original_file_path"]
        if sha256(csv_path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"{path}: seed changed: {node_id}")
        if column_types := entry.get("column_types"):
            tables[node_id] = CatalogTable(
                node_id, node["database"], node["schema"], node["alias"],
                tuple(Column(name, kind) for name, kind in column_types.items()),
            )
        for column, properties in entry["columns"].items():
            for label, declared in (("unique", "unique"), ("not_null", "not_null")):
                if properties.get(label) is True:
                    name = f"seed_snapshot:{node_id}:{column}:{label}"
                    facts.append(Expectation(name, name, "constraint", node_id, str(path), None, declared, (column,)))
    return tuple(facts), tables


def compile_project(project, output, settings):
    """Run dbt deps and compile without a database; return manifest facts."""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    config = safe_load((project / "dbt_project.yml").read_text())
    profile = OFFLINE_PROFILES[settings.get("adapter", "duckdb")] | {"threads": 1}
    (output / "profiles.yml").write_text(safe_dump({config["profile"]: {
        "target": "audit", "outputs": {"audit": profile},
    }}))
    env = {name: str(value) for name, value in settings.get("env", {}).items()}
    if (project / "packages.yml").exists() or (project / "dependencies.yml").exists():
        run_dbt(project, output, env, "deps")
    offline = [] if profile["type"] == "duckdb" else ["--no-introspect", "--no-populate-cache"]
    run_dbt(project, output, env, "compile", "--target-path", str(output / "target"),
            *offline, *settings.get("compile_args", []))
    manifest = output / "target" / "manifest.json"
    metadata = json.loads(manifest.read_text())["metadata"]
    return {"manifest": str(manifest), "manifest_sha256": sha256(manifest.read_bytes()).hexdigest(),
            "dbt_version": metadata["dbt_version"], "adapter": metadata["adapter_type"]}


def analyze_project(project, manifest_path, settings, seed_facts_path=None):
    """Prove dbt tests and join cardinalities from a compiled manifest."""
    config = safe_load((project / "dbt_project.yml").read_text())
    manifest = json.loads(manifest_path.read_text())
    nodes = [node for node in manifest["nodes"].values() if node["package_name"] == config["name"]]
    models = [node for node in nodes if node["resource_type"] == "model"]
    test_ids = {node["unique_id"] for node in nodes if node["resource_type"] == "test"}
    catalog = settings.get("catalog")
    analysis = _analysis(project, manifest_path, settings.get("trust_upstream_tests", True),
                         Path(catalog).expanduser().resolve() if catalog else None)
    if seed_facts_path:
        facts, seed_tables = load_seed_facts(seed_facts_path, project, manifest)
        analysis.expectations += facts
        if seed_tables:
            existing = analysis.catalog or Catalog(None)
            analysis.catalog = Catalog(existing.generated_at, seed_tables | existing.tables)
    report = analysis.run(None)
    tests = [test for test in report.tests if test.unique_id in test_ids and test.kind == "test"]
    eligible = sum(
        test.kind == "test" and test.unique_id in test_ids and test.asserted is not None
        for test in expectations(load_manifest(manifest_path))
    )
    proved = sum(test.skippable for test in tests)
    base_tables, joins = join_statistics(models, report.dialect, analysis)
    return {
        "models": len(models), "joins": joins, "base_tables": base_tables,
        "tests": {"total": len(test_ids), "eligible": eligible, "proved": proved},
        "test_outcomes": [asdict(test) for test in tests],
        "problems": [asdict(problem) for problem in report.problems],
        "markers": [asdict(marker) for marker in report.markers],
    }
