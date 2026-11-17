"""Compile a vendored dbt project offline and analyze its manifest with sqlassert."""

from collections import Counter
from dataclasses import asdict, replace
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
from sqlassert.ir.convert import _unsupported_source
from sqlassert.properties import NonNullColumn, NonNullColumns, UniqueSet
from sqlassert.python_engine import Engine
from sqlassert.rules.join_cardinality import many_to_one_join, one_to_many_join, unproved_join
from sqlassert_dbt.program import _canonical
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


def join_statistics(models, dialect, analysis, ignored=frozenset(), *, intermediate_tables=frozenset()):
    counts = Counter()
    skipped = {}
    cardinalities = []
    base_tables = {"tables": 0, "typed": 0, "keyed": 0, "not_null": 0, "columns": 0, "typed_columns": 0, "untyped": []}
    opaque_models = {"names": []}
    model_ids = {node["unique_id"] for node in models}
    builder = analysis._builder(dialect, []) if dialect else None
    if builder:
        builder.build(relation for key, relation in analysis.manifest.relations.items() if key in model_ids)
    conversion = builder._conversion if builder else None
    if conversion:
        assert builder is not None  # A conversion only comes from a builder.
        program = conversion.program
        reachable = set(program.nodes())
        facts = (*program.declarations, *conversion.knowledge)
        non_null = {fact.column for fact in facts if isinstance(fact, NonNullColumn)}
        non_null |= {column for fact in facts if isinstance(fact, NonNullColumns) for column in fact.columns}
        unique_sets = [fact.columns for fact in facts if isinstance(fact, UniqueSet) and fact.columns]
        # Base tables are data entering the project. Opaque relations are dbt models
        # read as tables or configured intermediates materialized outside dbt.
        by_name = {
            _canonical(relation.relation_name, dialect): relation
            for relation in analysis.manifest.relations.values() if relation.relation_name
        }
        tables = {node for node in reachable if isinstance(node, ir.NamedRelation) and node.role is ir.RelationRole.TABLE}
        for table in {table for table in tables if table.name.split(".")[-1].strip('"').lower() not in ignored}:
            relation = by_name.get(table.name.lower())
            if table.name.lower() in intermediate_tables:
                reason = "intermediate"
            elif relation is not None and relation.resource_type == "model":
                reason = ("python" if relation.language != "sql" else "left out" if relation.unique_id in builder._left_out
                          else relation.materialized or "table")
            else:
                reason = None
            if reason is not None:
                opaque_models[reason] = opaque_models.get(reason, 0) + 1
                opaque_models["names"].append(relation.name if relation is not None else table.name)
                continue
            columns = set(table.output_columns)
            typed = sum(column.typing is not None for column in columns)
            base_tables["tables"] += 1
            base_tables["columns"] += len(columns)
            base_tables["typed_columns"] += typed
            base_tables["typed"] += bool(columns) and typed == len(columns)
            if not columns or typed < len(columns):
                base_tables["untyped"].append(table.name)
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
                        site_entry = {"model": model_id, "branch": branch, "compiled_line": statement.local(site.line)}
                        unproved = unproved_join(join)
                        if join.kind in {"semi", "anti"}:
                            site_entry["cardinality"] = "NOT_APPLICABLE"
                        elif unproved is not None:
                            site_entry |= {"cardinality": "UNKNOWN", "reason": unproved.reason}
                            if unproved.detail is not None:
                                site_entry["detail"] = unproved.detail
                            if unproved.construct is not None:
                                site_entry["construct"] = unproved.construct
                        else:
                            site_entry["cardinality"] = {
                                (True, True): "ONE_TO_ONE", (True, False): "MANY_TO_ONE", (False, True): "ONE_TO_MANY",
                            }[bool(many_to_one_join(join).value), bool(one_to_many_join(join).value)]
                        cardinalities.append(site_entry)
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
                source = join.this
                while isinstance(source, exp.Lateral):
                    source = source.this
                if isinstance(source, exp.Unnest):
                    name = "UNNEST"
                elif isinstance(source, exp.Table) and isinstance(source.this, exp.Anonymous) and source.this.name.upper() == "JSON_EACH":
                    name = "JSON_EACH"
                else:
                    name = ""
                if name:
                    counts[name + ("*" if _unsupported_source(join.this) is not None else "")] += 1
                    continue
                parts = [join.method, join.side, join.kind]
                if join.kind == "OUTER" and join.side:
                    parts.pop()
                kind = " ".join(filter(None, parts)) or ("INNER" if join.args.get("on") or join.args.get("using") else "CROSS")
                counts[kind] += 1
    total = counts.total()
    cardinality_counts = Counter(join["cardinality"] for join in cardinalities)
    cardinality_total = cardinality_counts.total()
    return base_tables, opaque_models, {
        "total": total,
        "by_type": {kind: {"count": count, "percent": round(100 * count / total, 2)}
                    for kind, count in sorted(counts.items())},
        "skipped_models": skipped,
        "cardinality_total": cardinality_total,
        "by_cardinality": {
            kind: {"count": count, "percent": round(100 * count / cardinality_total, 2)}
            for kind, count in sorted(cardinality_counts.items())
        },
        "by_unknown_reason": {
            reason: {"count": count, "percent": round(100 * count / cardinality_total, 2)}
            for reason, count in sorted(Counter(
                join["reason"] + (f" ({': '.join(join[key] for key in ('detail', 'construct') if key in join)})"
                                  if "detail" in join or "construct" in join else "")
                for join in cardinalities if "reason" in join
            ).items())
        },
        "cardinality_sites": cardinalities,
    }


def load_base_facts(path, project, manifest, commit):
    """Facts about the seeds and sources a project reads, recorded once per repository commit.

    Each table entry gives `column_types` (every column, in order), `columns`
    (per-column `unique` and `not_null` flags) and `unique_sets` (composite keys). A seed entry also
    pins the profiled CSV by `sha256`. See pvt/public_repos_audit/README.md.
    """
    document = safe_load(path.read_text())
    if document["commit"] != commit:
        raise ValueError(f"{path}: recorded at {document['commit'][:12]}, checkout is at {commit[:12]}; redo their determination")
    facts = []
    tables = {}
    for node_id, entry in document["tables"].items():
        node = manifest["sources"].get(node_id) or manifest["nodes"].get(node_id)
        if not node or node["resource_type"] not in {"seed", "source"}:
            raise ValueError(f"{path}: {node_id} is not a manifest seed or source")
        if node["resource_type"] == "seed":
            if sha256((project / node["original_file_path"]).read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError(f"{path}: seed changed: {node_id}")
        if column_types := entry.get("column_types"):
            name = node["identifier"] if node["resource_type"] == "source" else node["alias"]
            tables[node_id] = CatalogTable(
                node_id, node["database"], node["schema"], name,
                tuple(Column(column, kind) for column, kind in column_types.items()),
            )
        for column, properties in entry.get("columns", {}).items():
            for label in ("unique", "not_null"):
                if properties.get(label) is True:
                    name = f"base_fact:{node_id}:{column}:{label}"
                    facts.append(Expectation(name, name, "constraint", node_id, str(path), None, label, (column,)))
        for columns in entry.get("unique_sets", []):
            name = f"base_fact:{node_id}:{','.join(columns)}:unique"
            facts.append(Expectation(name, name, "constraint", node_id, str(path), None, "unique", tuple(columns)))
    return tuple(facts), tables


def compile_project(project, output, settings):
    """Run dbt deps and compile without a database; return manifest facts."""
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    config = safe_load((project / "dbt_project.yml").read_text())
    profile = OFFLINE_PROFILES[settings.get("adapter", "duckdb")] | {"threads": 1}
    if settings.get("database") and profile["type"] == "duckdb":
        # An empty local file named as the real warehouse's database, so relation
        # names match a catalog.json generated there; nothing else is in it.
        profile["path"] = str(output / f"{settings['database']}.duckdb")
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


def analyze_project(project, manifest_path, settings, base_facts=None, commit=None, catalog=None, *, extra_base_facts=None):
    """Prove dbt tests and join cardinalities from a compiled manifest."""
    config = safe_load((project / "dbt_project.yml").read_text())
    manifest = json.loads(manifest_path.read_text())
    nodes = [node for node in manifest["nodes"].values() if node["package_name"] == config["name"]]
    models = [node for node in nodes if node["resource_type"] == "model"]
    test_ids = {node["unique_id"] for node in nodes if node["resource_type"] == "test"}
    analysis = _analysis(project, manifest_path, settings.get("trust_upstream_tests", True),
                         catalog)
    if settings.get("trust_warn_tests"):
        # Assume warn-severity tests pass too, as if they stopped the build:
        # proofs that use them hold only if they do.
        tests = {uid: replace(test, severity="error") if test.severity == "warn" else test
                 for uid, test in analysis.manifest.tests.items()}
        analysis.expectations = tuple(expectations(replace(analysis.manifest, tests=tests)))
    for path in (base_facts, extra_base_facts):
        if path is None:
            continue
        facts, tables = load_base_facts(path, project, manifest, commit)
        analysis.expectations += facts
        if tables:
            existing = analysis.catalog or Catalog(None)
            analysis.catalog = Catalog(existing.generated_at, tables | existing.tables)
    report = analysis.run(None)
    tests = [test for test in report.tests if test.unique_id in test_ids and test.kind == "test"]
    eligible = sum(
        test.kind == "test" and test.unique_id in test_ids and test.asserted is not None
        for test in expectations(load_manifest(manifest_path))
    )
    proved = sum(test.skippable for test in tests)
    ignored = frozenset(name.lower() for name in settings.get("ignore_tables", []))
    intermediate = frozenset(name.lower() for name in settings.get("intermediate_tables", []))
    base_tables, opaque_models, joins = join_statistics(models, report.dialect, analysis, ignored, intermediate_tables=intermediate)
    return {
        "models": len(models), "joins": joins, "base_tables": base_tables, "opaque_models": opaque_models,
        "tests": {"total": len(test_ids), "eligible": eligible, "proved": proved},
        "test_outcomes": [asdict(test) for test in tests],
        "problems": [asdict(problem) for problem in report.problems],
        "markers": [asdict(marker) for marker in report.markers],
    }
