"""Extract facts from vendored SQL repositories, cache each by its inputs, and render a report.

Each step stores one JSON fact per repository (or per dbt project) under
tmp/vendor-facts/. A fact records the key it was computed from; the step reruns
only when the key changes, its time to live passes, or --force is given.
"""

import argparse
from base64 import urlsafe_b64encode
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from importlib import metadata
import inspect
import json
import re
from pathlib import Path
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time

from markdown_it import MarkdownIt
from yaml import CSafeLoader, load

import vendor_dbt_analysis as dbt
import vendor_repository_inventory as inventory
from vendor_repository_inventory import SIZE_LIMITS, VENDOR, git, head


ROOT = VENDOR.parent
CONFIG = ROOT / "pvt" / "public_repos_audit" / "vendor_config.yaml"  # private workspace checkout
FACTS = ROOT / "tmp" / "vendor-facts"
REPORT = ROOT / "tmp" / "vendor-report.html"
STEPS = ("inventory", "stars", "compile", "base_facts", "analyze")
STAR_TTL = timedelta(hours=24)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def file_digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()[:16]


def code_digest(*objects):
    return digest([inspect.getsource(item) if callable(item) else item for item in objects])


def tree_digest(root):
    """Hash a source tree as it is on disk, uncommitted edits included."""
    hasher = sha256()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            hasher.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return hasher.hexdigest()[:16]


def version(distribution):
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return None


def project_slug(path):
    return "root" if path == "." else path.replace("/", "--")


class Step:
    def __init__(self, name, path, key, run, ttl=None, output=None):
        self.name, self.path, self.key, self.run, self.ttl = name, path, key, run, ttl
        self.output = output

    def stale_reason(self, saved, force):
        if force:
            return "forced"
        if saved is None:
            return "missing"
        if self.output is not None and saved["status"] == "ok" and not self.output.is_file():
            return "missing output"
        if changed := sorted(k for k in self.key.keys() | saved["key"].keys() if self.key.get(k) != saved["key"].get(k)):
            return "changed " + ", ".join(changed)
        if self.ttl and datetime.now(UTC) - datetime.fromisoformat(saved["created"]) > self.ttl:
            return "expired"
        return None

    def execute(self, force, dry_run):
        saved = read_fact(self.path)
        reason = self.stale_reason(saved, force)
        if reason is None or dry_run:
            return saved, reason
        record = {"step": self.name, "key": self.key, "created": datetime.now(UTC).isoformat(timespec="seconds")}
        started = time.monotonic()
        try:
            record |= {"status": "ok", "data": self.run()}
        except Exception as error:  # one broken repo must not stop the batch; the failure is cached as a fact
            record |= {"status": "failed", "error": f"{type(error).__name__}: {error}"}
        record["seconds"] = round(time.monotonic() - started, 2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, indent=2, default=lambda value: value.value) + "\n")
        temporary.replace(self.path)
        return record, reason


def read_fact(path):
    return json.loads(path.read_text()) if path.exists() else None


def fact_paths(name, settings):
    repo_facts = {step: FACTS / step / f"{name}.json" for step in ("inventory", "stars")}
    projects = {
        path: {step: FACTS / step / name / f"{project_slug(path)}.json" for step in ("compile", "base_facts", "analyze")}
        for path in settings.get("audit", {})
    }
    return repo_facts, projects


def generate_base_facts(program, context, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output.parent, delete=False) as temporary:
        path = Path(temporary.name)
        try:
            subprocess.run([sys.executable, str(program)], input=json.dumps(context).encode(),
                           stdout=temporary, check=True)
            path.replace(output)
        finally:
            path.unlink(missing_ok=True)
    return {"yaml": str(output)}


def repository_steps(name, settings, sources):
    """Yield the steps for one repository; later steps read facts written by earlier ones."""
    repo = VENDOR / name
    commit = head(repo)
    repo_facts, projects = fact_paths(name, settings)
    yield Step("inventory", repo_facts["inventory"], {
        "commit": commit, "settings": {k: settings.get(k) for k in ("scope", "dbt_projects", "workload")},
        "code": file_digest(inventory.__file__),
    }, lambda: inventory.inventory(repo, commit, settings))
    url = settings.get("url") if settings["workload"] != "Private project" else None
    yield Step("stars", repo_facts["stars"], {"url": url},
               lambda: {"stars": inventory.github_stars(url)}, ttl=STAR_TTL)
    for path, paths in projects.items():
        project_settings = settings["audit"][path] or {}
        project = repo / path
        adapter = project_settings.get("adapter", "duckdb")
        output = paths["compile"].with_suffix("")
        yield Step("compile", paths["compile"], {
            "commit": commit,
            "worktree": digest(git(repo, "diff", "HEAD").decode()),
            "settings": {k: project_settings.get(k) for k in ("adapter", "database", "env", "compile_args")},
            "dbt": version("dbt-core"), "adapter": version(f"dbt-{adapter}"),
            "code": code_digest(dbt.compile_project, dbt.run_dbt, dbt.OFFLINE_PROFILES),
        }, lambda project=project, output=output, s=project_settings: dbt.compile_project(project, output, s))
        compiled = read_fact(paths["compile"])
        if not compiled or compiled["status"] != "ok":
            continue
        manifest = Path(compiled["data"]["manifest"])
        base_config = project_settings.get("base_facts")
        if base_config is not None:
            base_facts = CONFIG.parent / base_config
        else:
            base_facts = CONFIG.parent / name / "base_facts.yaml"
            if not base_facts.exists():
                base_facts = None
        extra_base_facts = None
        if extra_config := project_settings.get("extra_base_facts"):
            program = CONFIG.parent / extra_config["program"]
            inputs = {item: str(manifest if item == "manifest" else project / item) for item in extra_config["inputs"]}
            trust = {"trust_upstream_tests": project_settings.get("trust_upstream_tests", True),
                     "trust_warn_tests": project_settings.get("trust_warn_tests", False)}
            context = {"commit": commit, "inputs": inputs, **trust}
            extra_base_facts = paths["base_facts"].with_suffix(".yaml")
            generation = Step("base_facts", paths["base_facts"], {
                "commit": commit, "program": file_digest(program), "settings": trust,
                "inputs": {item: file_digest(path) for item, path in inputs.items()},
                "code": code_digest(generate_base_facts),
            }, lambda: generate_base_facts(program, context, extra_base_facts), output=extra_base_facts)
            yield generation
            generated = read_fact(generation.path)
            if generation.stale_reason(generated, False) or generated["status"] != "ok":
                continue
        # A catalog.json from `dbt docs generate` on the real warehouse, named
        # explicitly per target (relative to the audit config) so the config says what was used.
        catalog = CONFIG.parent / project_settings["catalog"] if project_settings.get("catalog") else None
        yield Step("analyze", paths["analyze"], {
            "manifest": compiled["data"]["manifest_sha256"],
            "settings": {k: project_settings.get(k) for k in (
                "trust_upstream_tests", "trust_warn_tests", "catalog", "ignore_tables", "intermediate_tables",
            )},
            "base_facts": file_digest(base_facts) if base_facts else None,
            "extra_base_facts": file_digest(extra_base_facts) if extra_base_facts else None,
            "catalog": file_digest(catalog) if catalog else None,
            "sqlassert": sources["sqlassert"], "sqlassert_dbt": sources["sqlassert_dbt"],
            "sqlglot": version("sqlglot"),
            "code": code_digest(dbt.analyze_project, dbt.join_statistics, dbt.load_base_facts),
        }, lambda project=project, manifest=manifest, s=project_settings, facts=base_facts,
                  catalog=catalog, extra=extra_base_facts:
            dbt.analyze_project(project, manifest, s, facts, commit, catalog, extra_base_facts=extra))


# Report

TABLE = (
    "| Repository snapshot | Workload | Stars | SQL files | SQL lines | Size | dbt models | YAML data tests "
    "| Supported types | SQL data tests | What it contains |\n"
    "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |"
)
AUDIT_HEADERS = ("Audit", "Tests proved", "Join forms", "Join cardinality", "Base tables typed", "Base table facts",
                 "Opaque relations", "Analyzed with")


def number(value):
    return "—" if value is None else f"{value:,}"


def inventory_cells(name, settings, facts):
    label, url, workload = settings["label"], settings.get("url"), settings["workload"]
    found = facts["inventory"]
    commit = found["key"]["commit"] if found else None
    snapshot = f"{label} (`{commit[:12]}`)" if commit else label
    if url and workload != "Private project" and commit:
        tree_path = "-/tree" if url.startswith("https://gitlab.com/") else "tree"
        snapshot = f"[{label}]({url}/{tree_path}/{commit})"
    stars = facts["stars"]["data"]["stars"] if facts["stars"] and facts["stars"]["status"] == "ok" else None
    data = found["data"] if found and found["status"] == "ok" else {}
    counts = data.get("dbt") or {}
    columns = [counts.get(key) for key in ("models", "yaml_tests", "supported_tests", "sql_tests")]
    return [snapshot, workload, number(stars), number(data.get("sql_files")), number(data.get("sql_lines")),
            data.get("size", "—"), *map(number, columns), settings["description"]]


def breakdown(results, key):
    counts = Counter()
    for result in results:
        counts.update({kind: entry["count"] for kind, entry in result["joins"].get(key, {}).items()})
    total = counts.total()
    return ", ".join(f"{kind.replace('*', r'\*')} {count:,} ({100 * count / total:.2f}%)"
                     for kind, count in sorted(counts.items())) or "—"


# Who can act on a join with no proved cardinality: 1 nobody (no proof exists),
# 2 the project (a fact is missing), 3 sqlassert (it loses or does not model something).
# Keys are `reason (detail)` as `vendor_dbt_analysis` writes them; one with a construct after the
# detail, `reason (detail: construct)`, has its detail's mark. A reason not listed is sqlassert's.
UNKNOWN_MARKS = {
    "unconstrained": "¹", "no-unique-set (partial-key)": "¹",
    "untyped-key (table)": "²", "untyped-key (type-mismatch)": "²", "no-unique-set (undeclared-key)": "²",
    "no-unique-set (cascade)": "³",
}


def unknown_mark(reason):
    return UNKNOWN_MARKS.get(reason) or UNKNOWN_MARKS.get(reason.split(": ", 1)[0] + ")", "³")


def unknown_reasons(results):
    """Unknown-cardinality reasons as (mark, reason, count), the ones nobody can act on last,
    encoded so the Markdown table carries them to `render_html`, which links UNKNOWN to them."""
    counts = Counter()
    for result in results:
        counts.update({reason: entry["count"] for reason, entry in result["joins"].get("by_unknown_reason", {}).items()})
    marked = [(unknown_mark(reason), reason, count) for reason, count in counts.items()]
    marked.sort(key=lambda item: (item[0] == "¹", -item[2], item[1]))
    return f"⟦{urlsafe_b64encode(json.dumps(marked).encode()).decode()}⟧" if marked else ""


def audit_cells(projects):
    if not projects:
        return ["Unconfigured", *["—"] * (len(AUDIT_HEADERS) - 1)]
    analyzed = [facts["analyze"] for facts in projects.values()
                if facts["analyze"] and facts["analyze"]["status"] == "ok"]
    failed = sum(any(fact and fact["status"] == "failed" for fact in facts.values()) for facts in projects.values())
    status = f"{len(analyzed)}/{len(projects)} analyzed" + (f", {failed} failed" if failed else "")
    if not analyzed:
        return [status, *["—"] * (len(AUDIT_HEADERS) - 1)]
    data = [fact["data"] for fact in analyzed]
    proved = sum(result["tests"]["proved"] for result in data)
    total = sum(result["tests"]["total"] for result in data)
    tables = Counter()
    for result in data:
        tables.update({key: value for key, value in result["base_tables"].items() if key != "untyped"})
    sqlassert = sorted({fact["key"]["sqlassert"][:8] for fact in analyzed})
    dates = sorted(fact["created"][:10] for fact in analyzed)
    # Proofs that assume warn-severity tests pass are conditional: say so.
    assumed = " (warn tests trusted)" if any(fact["key"]["settings"].get("trust_warn_tests") for fact in analyzed) else ""
    return [f"{status}{assumed}", f"{proved:,}/{total:,}", breakdown(data, "by_type"), breakdown(data, "by_cardinality") + unknown_reasons(data),
            *base_table_cells(tables), opaque_cell(data), f"`{', '.join(sqlassert)}` {dates[-1]}"]


def opaque_cell(data):
    """Intermediate relations the analysis reads as tables, grouped by reason."""
    reasons = Counter()
    for result in data:
        reasons.update({key: value for key, value in result.get("opaque_models", {}).items() if key != "names"})
    if not reasons:
        return "none"
    return f"{reasons.total():,}: " + ", ".join(f"{reason} {count:,}" for reason, count in sorted(reasons.items()))


def percent(part, whole):
    return f"{100 * part / whole:.0f}%" if whole else "—"


def base_table_cells(tables):
    """Typing and declared facts of the sources, seeds and tables the analyzed models read."""
    count = tables["tables"]
    if not count:
        return ["no base tables", "—"]
    typed = (f'{percent(tables["typed"], count)} ({tables["typed"]:,}/{count:,} tables, '
             f'{tables["typed_columns"]:,}/{tables["columns"]:,} columns)')
    facts = f'keyed {percent(tables["keyed"], count)}, not null {percent(tables["not_null"], count)}'
    return [typed, facts]


def collect(repositories):
    rows = {}
    for name, settings in repositories.items():
        repo_facts, projects = fact_paths(name, settings)
        rows[name] = (
            {step: read_fact(path) for step, path in repo_facts.items()},
            {project: {step: read_fact(path) for step, path in paths.items()} for project, paths in projects.items()},
        )
    return rows


def markdown_table(repositories, rows, audit):
    header, alignment = TABLE.splitlines()
    if audit:
        header += " " + " | ".join(AUDIT_HEADERS) + " |"
        alignment += " --- |" * len(AUDIT_HEADERS)
    lines = [header, alignment]
    for name, settings in repositories.items():
        facts, projects = rows[name]
        cells = inventory_cells(name, settings, facts) + (audit_cells(projects) if audit else [])
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def failures(rows):
    found = []
    for name, (facts, projects) in rows.items():
        for step, fact in facts.items():
            if fact and fact["status"] == "failed":
                found.append((name, "", step, fact["error"]))
        for project, project_facts in projects.items():
            for step, fact in project_facts.items():
                if fact and fact["status"] == "failed":
                    found.append((name, project, step, fact["error"]))
    return found


def render_html(repositories, rows):
    markdown = MarkdownIt("commonmark", {"html": False}).enable("table")
    public = [name for name, settings in repositories.items() if settings["workload"] != "Private project"]
    analyzed = [facts["analyze"]["data"] for name in public for facts in rows[name][1].values()
                if facts["analyze"] and facts["analyze"]["status"] == "ok"]
    proved = sum(result["tests"]["proved"] for result in analyzed)
    total = sum(result["tests"]["total"] for result in analyzed)
    headline = (f"Across {len(analyzed)} analyzed public dbt projects, sqlassert proves {proved:,} of {total:,} data tests"
                f" ({100 * proved / total:.1f}%)." if total else "No analyzed dbt projects with data tests yet.")
    failure_items = "".join(
        f"<li><code>{name}{' / ' + project if project else ''}</code> {step}: {escape(error)}</li>"
        for name, project, step, error in failures(rows)
    )
    failure_section = f'<section><h2>Failures</h2><ul class="failures">{failure_items}</ul></section>' if failure_items else ""
    ordered = sorted(repositories, key=lambda name: not any(
        (fact := project["analyze"]) and fact["status"] == "ok" for project in rows[name][1].values()
    ))
    ordered_repositories = {name: repositories[name] for name in ordered}
    table = re.sub(
        r"UNKNOWN ([\d,]+ \([\d.]+%\))(.*?)⟦([\w=-]+)⟧",
        r'<a href="#" class="why-unknown" data-reasons="\3">UNKNOWN \1</a>\2',
        markdown.render(markdown_table(ordered_repositories, rows, audit=True)),
    )
    generated = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SQL repository audit</title>
  <link rel="stylesheet" href="https://cdn.datatables.net/2.3.5/css/dataTables.dataTables.min.css">
  <style>
    body {{ margin: clamp(1rem, 3vw, 3rem); font: 15px/1.5 system-ui, sans-serif; color: #172333; }}
    h1 {{ margin: 0 0 .5rem; font-size: 1.75rem; }}
    .headline {{ font-size: 1.1rem; margin: 0 0 1rem; }}
    /* The table scrolls inside its own box so the header and repository column can stay pinned. */
    .scroll {{ overflow: auto; max-height: calc(100vh - 4rem); border: 1px solid #dde4ec; }}
    table {{ width: max-content; min-width: 100%; border-collapse: separate; border-spacing: 0; }}
    th, td {{ padding: .65rem .8rem; border-bottom: 1px solid #dde4ec; vertical-align: top; text-align: left; }}
    thead th {{ position: sticky; top: 0; z-index: 2; background: #eaf0f7; white-space: nowrap; }}
    th:first-child, td:first-child {{ position: sticky; left: 0; z-index: 1; background: #fff;
      min-width: 12rem; max-width: 18rem; box-shadow: inset -1px 0 #c9d3de; }}
    thead th:first-child {{ z-index: 3; background: #eaf0f7; }}
    td:nth-child(n+2):nth-child(-n+10), td:nth-child(n+12):nth-child(-n+13), td:nth-child(n+16) {{
      white-space: nowrap; font-variant-numeric: tabular-nums; }}
    td:nth-child(11) {{ min-width: 28rem; }}
    td:nth-child(14), td:nth-child(15) {{ min-width: 18rem; }}
    a {{ color: #1455a0; }}
    .failures {{ font-size: .9rem; }}
    .column-guide {{ max-width: 76ch; margin-top: 1.5rem; }}
    .column-guide dt {{ font-weight: 650; }}
    .column-guide dd {{ margin: .2rem 0 .8rem; }}
    footer {{ margin-top: 2rem; color: #5a6878; font-size: .85rem; }}
    dialog {{ border: 1px solid #c9d3de; border-radius: 8px; padding: 1rem 1.25rem; max-width: min(90vw, 70rem); }}
    dialog table {{ width: 100%; }}
    dialog td:nth-child(n+3) {{ text-align: right; font-variant-numeric: tabular-nums; }}
  </style>
</head>
<body>
  <h1>SQL repository audit</h1>
  <p class="headline">{headline}</p>
  <div class="report scroll">{table}</div>
  {failure_section}
  <section class="column-guide" aria-labelledby="column-guide-heading">
    <h2 id="column-guide-heading">Column guide</h2>
    <dl>
      <dt>Workload</dt>
      <dd>Published benchmark is a corpus released for evaluation. Real-world project covers applied analytics.
        Used in research marks a real-world project with a published research reference in its description.
        Private project covers internal workloads. Demo covers examples and showcases.</dd>
      <dt>SQL lines</dt>
      <dd>Lines in tracked SQL files, excluding blank lines and comment-only lines.</dd>
      <dt>Size</dt>
      <dd>SQL line limits are XS under 200, S under 1,000, M under 10,000, L under 100,000,
        and XL under 1,000,000. XXL starts at 1,000,000.</dd>
      <dt>YAML data tests</dt>
      <dd>Generic dbt data test declarations under <code>tests</code> or <code>data_tests</code> in project YAML.</dd>
      <dt>Supported types</dt>
      <dd>How many of those YAML declarations use a test type sqlassert recognizes:
        <code>unique</code>, <code>not_null</code>, <code>accepted_values</code>,
        <code>relationships</code>, and <code>dbt_utils.unique_combination_of_columns</code>.</dd>
      <dt>SQL data tests</dt>
      <dd>Standalone dbt test query files. Each query returns rows that fail the check.</dd>
      <dt>Audit</dt>
      <dd>dbt projects analyzed out of those configured for this repository at the displayed commit.
        "warn tests trusted" means the analysis assumes warn-severity tests pass, so proofs based on them are conditional.</dd>
      <dt>Tests proved</dt>
      <dd>Data tests sqlassert proves from the compiled manifest, so dbt can skip them, out of all project tests.
        By default, upstream tests are facts only when their failure stops the build (severity error).</dd>
      <dt>Join forms and cardinality</dt>
      <dd>Counts pooled over analyzed projects, with separate totals for SQL forms and sqlassert proof sites.</dd>
      <dd>An asterisk marks a join source that sqlassert treats as opaque.</dd>
      <dt>Why unknown (click UNKNOWN in Join cardinality)</dt>
      <dd>Marks say who can act: <sup>1</sup> nobody, no proof exists (listed last); <sup>2</sup> the project, by supplying a
        missing fact (a type, a key); <sup>3</sup> sqlassert, which loses or does not model something.
        After a colon, the construct sqlassert does not support that is behind the reason.</dd>
      <dd>Why a join has no proved cardinality, the first that applies:
        <code>unsupported</code> (a table function, UNNEST or LATERAL input, or a join inside a SELECT sqlassert reads as opaque),
        <code>unconstrained</code> (no ON equality between the inputs or to a constant: a cross or range join; no key can make it unique),
        <code>unresolved-key</code> (an equality operand that does not resolve or is not modelled),
        <code>untyped-key</code> (a column = column equality whose types do not compare by identity; in parentheses, where the type is lost:
        <code>values</code>, <code>computed</code>, <code>opaque-relation</code>, <code>set-operation</code>, <code>table</code> with no declared type,
        or <code>type-mismatch</code>),
        <code>expression-key</code> (only expressions are compared, not columns),
        <code>no-unique-set</code> (the equated columns are a unique key of neither input; in parentheses, who can act:
        <code>cascade</code> <sup>3</sup>, an input has a join or a construct below it that is itself unproved or unsupported, so fixing that may fix this join;
        <code>partial-key</code> <sup>1</sup>, the equalities bind part of a known key and the rest of ON is a range or inequality, so the join is as constrained as written;
        <code>undeclared-key</code> <sup>2</sup>, no known key covers the equated columns, so a declared one would prove it).</dd>
      <dt>Base tables typed</dt>
      <dd>Base tables are the data entering the project: sources, seeds and snapshots the analyzed models read whose every read column has a known type, from a
        contract, the catalog or base facts. A join equality on an untyped column proves nothing, since an implicit
        cast can match unequal values.</dd>
      <dt>Base table facts</dt>
      <dd>Share of those base tables with a known unique key, and with at least one NOT NULL column, from trusted
        tests, contracts or base facts.</dd>
      <dt>Opaque relations</dt>
      <dd>Intermediate relations the analysis reads as tables, grouped by reason. Includes Python models,
        SQL models left out because their SQL does not parse or lower, incremental models, and tables explicitly
        configured as intermediates. The base-table counts exclude these relations.</dd>
      <dt>Analyzed with</dt>
      <dd>Hash of the sqlassert source tree the analysis ran on, and when it ran.</dd>
    </dl>
    <p>A dash in a dbt column means the checkout has no dbt project, or the fact is not extracted yet.</p>
  </section>
  <dialog id="why-unknown">
    <form method="dialog" style="float: right"><button>Close</button></form>
    <h2>Why unknown</h2>
    <p>Marks say who can act: <sup>1</sup> nobody, no proof exists; <sup>2</sup> the project, by supplying a missing
      fact; <sup>3</sup> sqlassert. See the column guide for each reason.</p>
    <table><thead><tr><th>Mark</th><th>Reason</th><th>Joins</th><th>%</th></tr></thead><tbody></tbody></table>
  </dialog>
  <footer>Generated {generated} by <code>scripts/audit.py</code>.</footer>
  <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
  <script src="https://cdn.datatables.net/2.3.5/js/dataTables.min.js"></script>
  <script>
    if (window.DataTable) {{
      const table = document.querySelector(".report table");
      table.classList.add("display", "compact");
      const sizes = {[label for _, label in SIZE_LIMITS] + ["XXL"]};
      for (const row of table.tBodies[0].rows) {{
        for (const cell of Array.from(row.cells).slice(2, 10)) {{
          const value = cell.textContent.trim();
          const sizeRank = sizes.indexOf(value);
          cell.dataset.order = sizeRank >= 0 ? sizeRank : value === "—" ? "-1" : value.replaceAll(",", "");
        }}
      }}
      new DataTable(table, {{ order: [], paging: false, autoWidth: false }});
      const dialog = document.getElementById("why-unknown");
      let reasons = null;
      document.addEventListener("click", event => {{
        const link = event.target.closest(".why-unknown");
        if (!link) return;
        event.preventDefault();
        const rows = JSON.parse(atob(link.dataset.reasons.replaceAll("-", "+").replaceAll("_", "/")));
        const total = rows.reduce((sum, row) => sum + row[2], 0);
        if (reasons) reasons.destroy();
        const body = dialog.querySelector("tbody");
        body.replaceChildren(...rows.map(([mark, reason, count]) => {{
          const row = body.insertRow();
          for (const value of [mark, reason, count.toLocaleString(), (100 * count / total).toFixed(1)]) row.insertCell().textContent = value;
          row.cells[0].dataset.order = mark === "¹" ? 9 : mark.length;
          row.cells[2].dataset.order = row.cells[3].dataset.order = count;
          return row;
        }}));
        reasons = new DataTable(dialog.querySelector("table"), {{ order: [], paging: false, searching: false, info: false }});
        dialog.showModal();
      }});
      // Scroll only the table, not the search box DataTables adds above it.
      document.querySelector(".report").classList.remove("scroll");
      table.parentElement.classList.add("scroll");
    }}
  </script>
</body>
</html>"""


def audit_repository(name, settings, sources, steps, force, dry_run):
    """Run one repository's steps in order; repositories run in parallel threads."""
    lines, failed = [], False
    for step in repository_steps(name, settings, sources):
        if step.name not in steps and not (step.name == "base_facts" and "analyze" in steps):
            continue
        label = f"{name} {step.name}" + (f" {step.path.stem}" if step.name in ("compile", "base_facts", "analyze") else "")
        record, reason = step.execute(force and step.name in steps, dry_run)
        if dry_run:
            lines.append(f"{label}: {'stale (' + reason + ')' if reason else 'fresh'}")
            continue
        outcome = "fresh" if reason is None else f"ran ({reason}) in {record['seconds']}s"
        if record and record["status"] == "failed":
            failed |= reason is not None
            outcome += f", failed: {record['error']}" if reason else ", failed earlier"
        lines.append(f"{label}: {outcome}")
    return lines, failed


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("repos", nargs="*", help="vendor directory names (default: all configured repos)")
    parser.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated subset of {', '.join(STEPS)}")
    parser.add_argument("--force", action="store_true", help="rerun the selected steps even when fresh")
    parser.add_argument("--dry-run", action="store_true", help="show what is stale and why; run nothing")
    parser.add_argument("-j", "--jobs", type=int, default=8,
                        help="repositories to process in parallel threads; 1 runs serially (default: %(default)s)")
    parser.add_argument("--markdown", action="store_true", help="print the inventory table as Markdown")
    args = parser.parse_args()
    steps = set(args.steps.split(","))
    if unknown := steps - set(STEPS):
        parser.error(f"unknown steps: {', '.join(sorted(unknown))}")
    repositories = load(CONFIG.read_text(encoding="utf-8"), Loader=CSafeLoader)["repositories"]
    if unknown := set(args.repos) - repositories.keys():
        parser.error(f"unknown repositories: {', '.join(sorted(unknown))}")

    sources = {"sqlassert": tree_digest(ROOT / "sqlassert"),
               "sqlassert_dbt": tree_digest(ROOT / "integrations" / "dbt" / "sqlassert_dbt")}
    failed = False
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(audit_repository, name, repositories[name], sources, steps, args.force, args.dry_run)
                   for name in args.repos or repositories]
        for future in as_completed(futures):
            lines, repository_failed = future.result()
            if lines:
                print("\n".join(lines), file=sys.stderr, flush=True)
            failed |= repository_failed
    if args.dry_run:
        return 0

    rows = collect(repositories)
    REPORT.write_text(render_html(repositories, rows))
    print(REPORT.as_uri(), file=sys.stderr)
    if args.markdown:
        print(markdown_table(repositories, rows, audit=False))
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
