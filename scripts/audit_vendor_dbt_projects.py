"""Compile pinned vendor dbt projects and save proof and join statistics."""

import argparse
from collections import Counter
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile

from sqlglot import ErrorLevel, exp, parse
from sqlglot.errors import SqlglotError
from yaml import safe_dump, safe_load

from report_vendor_sql_repository_facts import VENDOR, git
from sqlassert_dbt.expectations import Expectation, expectations
from sqlassert_dbt.manifest import load_manifest
from sqlassert import ir
from sqlassert.properties import NonNullColumn, NonNullColumns, UniqueSet
from sqlassert.python_engine import Engine
from sqlassert.rules.join_cardinality import many_to_one_join, one_to_many_join
from sqlassert_dbt.project import ProjectError, _analysis


def run_dbt(project, output, profiles, target, step, *options):
    command = ["dbt", step, "--project-dir", str(project), "--profiles-dir", str(profiles),
               "--target", target, "--log-path", str(output / "logs"), *options]
    with (output / f"{step}.log").open("w") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)


def join_statistics(models, dialect, analysis):
    counts = Counter()
    skipped = {}
    cardinalities = []
    has_base_table_properties = False
    model_ids = {node["unique_id"] for node in models}
    builder = analysis._builder(dialect, []) if dialect else None
    if builder:
        builder.build(relation for key, relation in analysis.manifest.relations.items() if key in model_ids)
    conversion = builder._conversion if builder else None
    if conversion:
        program = conversion.program
        reachable = set(program.nodes())
        facts = (*program.declarations, *conversion.knowledge)
        base_columns = {
            column for node in reachable
            if isinstance(node, ir.NamedRelation) and node.role is ir.RelationRole.TABLE
            for column in node.output_columns
        }
        has_base_table_properties = any(
            (isinstance(fact, NonNullColumn) and fact.column in base_columns)
            or (isinstance(fact, NonNullColumns) and bool(fact.columns & base_columns))
            or (isinstance(fact, UniqueSet) and bool(fact.columns) and fact.columns <= base_columns)
            for fact in facts
        )
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
    return has_base_table_properties, {
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
    for node_id, entry in safe_load(path.read_text())["seeds"].items():
        node = manifest["nodes"].get(node_id)
        if not node or node["resource_type"] != "seed":
            raise ValueError(f"{path}: {node_id} is not a manifest seed")
        csv_path = project / node["original_file_path"]
        if sha256(csv_path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError(f"{path}: seed changed: {node_id}")
        for column, properties in entry["columns"].items():
            for label, declared in (("unique", "unique"), ("not_null", "not_null")):
                if properties.get(label) is True:
                    name = f"seed_snapshot:{node_id}:{column}:{label}"
                    facts.append(Expectation(name, name, "constraint", node_id, str(path), None, declared, (column,)))
    return tuple(facts)


def audit_project(project, output, settings):
    config = safe_load((project / "dbt_project.yml").read_text())
    target = settings.get("target", "audit")
    profiles = output
    if settings.get("profiles_dir"):
        profiles = Path(settings["profiles_dir"]).expanduser().resolve()
    else:
        profile = {"type": "duckdb", "path": ":memory:", "threads": 1}
        (profiles / "profiles.yml").write_text(safe_dump({config["profile"]: {
            "target": target, "outputs": {target: profile},
        }}))
    result = {"target": target}
    if (project / "packages.yml").exists() or (project / "dependencies.yml").exists():
        run_dbt(project, output, profiles, target, "deps")
    compile_mode = [] if profiles == output else ["--no-introspect", "--no-populate-cache"]
    run_dbt(project, output, profiles, target, "compile", "--target-path", str(output / "target"),
            *compile_mode, *settings.get("compile_args", []))
    manifest_path = output / "target" / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    metadata = manifest["metadata"]
    nodes = [node for node in manifest["nodes"].values() if node["package_name"] == config["name"]]
    models = [node for node in nodes if node["resource_type"] == "model"]
    test_ids = {node["unique_id"] for node in nodes if node["resource_type"] == "test"}
    catalog = settings.get("catalog")
    analysis = _analysis(project, manifest_path, settings.get("trust_upstream_tests", True),
                         Path(catalog).expanduser().resolve() if catalog else None)
    seed_facts = settings.get("seed_facts")
    if seed_facts:
        path = (VENDOR / "vendor_config.yaml").resolve().parent / seed_facts
        analysis.expectations += load_seed_facts(path, project, manifest)
    report = analysis.run(None)
    tests = [test for test in report.tests if test.unique_id in test_ids and test.kind == "test"]
    eligible = sum(
        test.kind == "test" and test.unique_id in test_ids and test.asserted is not None
        for test in expectations(load_manifest(manifest_path))
    )
    proved = sum(test.skippable for test in tests)
    has_base_table_properties, joins = join_statistics(models, report.dialect, analysis)
    return result | {
        "status": "analyzed", "dbt_version": metadata["dbt_version"], "adapter": metadata["adapter_type"],
        "seed_facts": seed_facts, "models": len(models), "joins": joins, "has_base_table_properties": has_base_table_properties,
        "tests": {"total": len(test_ids), "eligible": eligible, "proved": proved,
                  "percent_of_total": round(100 * proved / len(test_ids), 2) if test_ids else None,
                  "percent_of_eligible": round(100 * proved / eligible, 2) if eligible else None},
        "test_outcomes": [asdict(test) for test in tests],
        "problems": [asdict(problem) for problem in report.problems],
        "markers": [asdict(marker) for marker in report.markers],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repositories", nargs="*")
    parser.add_argument("--output", type=Path, default=VENDOR.parent / "tmp" / "vendor-dbt-audit")
    args = parser.parse_args()
    repositories = safe_load((VENDOR / "vendor_config.yaml").read_text())["repositories"]
    if unknown := set(args.repositories) - repositories.keys():
        parser.error(f"Unknown repositories: {', '.join(sorted(unknown))}")
    args.output.mkdir(parents=True, exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="run-", dir=args.output.resolve()))
    results = []
    for name in args.repositories or repositories:
        repo = VENDOR / name
        settings = repositories[name]
        commit = git(repo, "rev-parse", "HEAD").decode().strip()
        projects = settings.get("audit", {})
        if not projects:
            results.append({"repository": name, "commit": commit, "status": "no audit target configured"})
            continue
        with tempfile.TemporaryDirectory() as checkout:
            subprocess.run(["tar", "-xf", "-", "-C", checkout], input=git(repo, "archive", commit, "--", settings.get("scope", ".")), check=True)
            for index, (path, profile) in enumerate(projects.items(), 1):
                output = run / name / str(index)
                output.mkdir(parents=True)
                result = {"repository": name, "commit": commit, "project": path,
                          "target": profile.get("target", "audit"), "artifacts": str(output.relative_to(run))}
                try:
                    result.update(audit_project(Path(checkout) / path, output, profile))
                except (subprocess.CalledProcessError, OSError, ValueError, ProjectError) as error:
                    result.update(status="failed", error=str(error))
                results.append(result)
                print(f"{name} / {path}: {result['status']}", flush=True)
    (run / "results.json").write_text(json.dumps(results, indent=2, default=lambda value: value.value) + "\n")
    print(run / "results.json")
    return int(any(result["status"] == "failed" for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
