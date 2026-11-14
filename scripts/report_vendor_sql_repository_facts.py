# /// script
# requires-python = ">=3.13"
# dependencies = ["PyYAML==6.0.3", "markdown-it-py==4.0.0"]
# ///
"""Print a Markdown table of SQL and dbt facts from vendored Git repositories."""

import argparse
from collections import Counter
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
from tempfile import NamedTemporaryFile
import time

from markdown_it import MarkdownIt
from yaml import CSafeLoader, YAMLError, load


VENDOR = Path(__file__).resolve().parents[1] / "vendor"
STAR_CACHE = Path("/tmp") / f"sqlassert-github-stars-{os.getuid()}"
STAR_CACHE_TTL = 24 * 60 * 60
PROPERTY_TESTS = {"unique", "not_null", "accepted_values", "relationships", "dbt_utils.unique_combination_of_columns"}
TEST_BLOCK = re.compile(rb"{%-?\s*test\s+")
SQL_COMMENT = re.compile(rb"--[^\r\n]*|/\*.*?(?:\*/|\Z)|\{#.*?(?:#\}|\Z)", re.DOTALL)
SIZE_LIMITS = ((200, "XS"), (1_000, "S"), (10_000, "M"), (100_000, "L"), (1_000_000, "XL"))
TABLE = (
    "| Repository snapshot | Workload | Stars | SQL files | SQL lines | Size | dbt models | YAML data tests "
    "| Supported types | SQL data tests | What it contains |\n"
    "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |"
)
WORKLOADS = {
    "Published benchmark", "Real-world project", "Real-world project · used in research",
    "Private project", "Demo",
}


def git(repo, *args, input_bytes=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=input_bytes)


def blobs(repo, tree):
    """Read the selected Git blobs in one batch, independent of the working tree."""
    if not tree:
        return {}
    request = b"".join(oid + b"\n" for oid in tree.values())
    stream = BytesIO(git(repo, "cat-file", "--batch", input_bytes=request))
    files = {}
    for path in tree:
        _, kind, size = stream.readline().split()
        if kind != b"blob":
            raise ValueError(f"{path}: expected a Git blob")
        files[path] = stream.read(int(size))
        stream.read(1)
    return files


def read_yaml(files, path):
    try:
        return load(files[path], Loader=CSafeLoader)
    except YAMLError as error:
        raise ValueError(f"{path}: {error}") from error


def count_data_tests(value, counts):
    if isinstance(value, list):
        for item in value:
            count_data_tests(item, counts)
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("tests", "data_tests") and isinstance(child, list):
                for test in child:
                    if isinstance(test, str):
                        counts[test] += 1
                    elif isinstance(test, dict):
                        counts.update(test.keys() - {"arguments", "config"})
            else:
                count_data_tests(child, counts)


def project_facts(config, paths, files):
    settings = read_yaml(files, config)
    root = config.parent

    def directories(kind):
        key = f"{kind}-paths"
        if kind == "model" and key not in settings and "source-paths" in settings:
            key = "source-paths"
        return {root / folder for folder in settings.get(key, [f"{kind}s"])}

    model_dirs = directories("model")
    test_dirs = directories("test")
    property_dirs = model_dirs | test_dirs | directories("seed") | directories("snapshot")
    generic_dirs = {folder / "generic" for folder in test_dirs}
    counts = Counter()
    models = singular = 0
    for path in paths:
        parents = set(path.parents)
        if path.suffix.lower() == ".sql":
            models += bool(parents & model_dirs)
            if parents & test_dirs and not parents & generic_dirs and not TEST_BLOCK.search(files[path]):
                singular += 1
        elif path.suffix.lower() in {".yml", ".yaml"} and parents & property_dirs:
            doc = read_yaml(files, path)
            if isinstance(doc, dict):
                for key in ("models", "sources", "seeds", "snapshots"):
                    count_data_tests(doc.get(key), counts)
    supported = sum(counts[name] for name in PROPERTY_TESTS)
    return models, counts.total(), supported, singular


def github_stars(url):
    prefix = "https://github.com/"
    if not url.startswith(prefix):
        return None
    slug = url.removeprefix(prefix).lower()
    cache = STAR_CACHE / f"{slug}.txt"
    try:
        if time.time() - cache.stat().st_mtime < STAR_CACHE_TTL:
            return int(cache.read_text())
    except (FileNotFoundError, ValueError):
        pass

    stars = int(subprocess.check_output(["gh", "api", f"repos/{slug}", "--jq", ".stargazers_count"]))
    cache.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", dir=cache.parent, delete_on_close=False) as temporary:
        temporary.write(f"{stars}\n")
        temporary.close()
        Path(temporary.name).replace(cache)
    return stars


def count_sql_lines(sql):
    uncommented = SQL_COMMENT.sub(lambda match: b"\n" * match[0].count(b"\n"), sql)
    return sum(bool(line.strip()) for line in uncommented.splitlines())


AUDIT_HEADERS = ("Audit", "Tests proved", "Join forms", "Join cardinality", "Base table facts")


def load_audit_results():
    results = {}
    paths = (VENDOR.parent / "tmp" / "vendor-dbt-audit").glob("run-*/results.json")
    for path in sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path)):
        for result in json.loads(path.read_text()):
            if result.get("project") is not None:
                key = (result["repository"], result["commit"], result["project"])
                results[key] = result
    return results


def audit_cells(name, commit, settings, results):
    projects = settings.get("audit", {})
    saved = [results.get((name, commit, project), {}) for project in projects]
    analyzed = [result for result in saved if result.get("status") == "analyzed"]
    if not analyzed:
        status = "Unconfigured" if not projects else "Failed" if any(saved) else "Pending"
        return [status, "—", "—", "—", "—"]

    def breakdown(key):
        counts = Counter()
        for result in analyzed:
            counts.update({kind: entry["count"] for kind, entry in result["joins"].get(key, {}).items()})
        total = counts.total()
        return ", ".join(f"{kind} {count:,} ({100 * count / total:.2f}%)" for kind, count in sorted(counts.items())) or "—"

    counts = Counter()
    for result in analyzed:
        counts.update({key: result["tests"][key] for key in ("proved", "total")})
    flags = [result.get("has_base_table_properties") for result in analyzed]
    base_facts = "Yes" if any(flags) else "—" if None in flags else "No"
    return [
        f"{len(analyzed)}/{len(projects)} analyzed", f'{counts["proved"]:,}/{counts["total"]:,}',
        breakdown("by_type"), breakdown("by_cardinality"), base_facts,
    ]


def repository_row(name, settings, audit_results=None):
    workload = settings["workload"]
    if workload not in WORKLOADS:
        raise ValueError(f"{name}: expected workload to be one of {', '.join(sorted(WORKLOADS))}")
    repo = VENDOR / name
    if not (repo / ".git").exists():
        raise ValueError(f"{repo}: expected a Git checkout")
    commit = git(repo, "rev-parse", "--verify", "HEAD").decode().strip()
    tree = {}
    scope = settings.get("scope", ".")
    for record in git(repo, "ls-tree", "-rz", commit, "--", scope).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, _, oid = metadata.split()
            if mode in {b"100644", b"100755"}:
                tree[PurePosixPath(path.decode())] = oid
    if not tree:
        raise ValueError(f"{name}: no tracked files in {scope}")
    configs = {path for path in tree if path.name == "dbt_project.yml"}
    selected = configs
    if "dbt_projects" in settings:
        selected = {PurePosixPath(folder) / "dbt_project.yml" for folder in settings["dbt_projects"]}
    if missing := selected - configs:
        raise ValueError(f"{name}: missing dbt configs: {', '.join(map(str, sorted(missing)))}")

    files = blobs(repo, {
        path: oid for path, oid in tree.items()
        if path.suffix.lower() == ".sql" or (selected and path.suffix.lower() in {".yml", ".yaml"})
    })
    sql_files = [content for path, content in files.items() if path.suffix.lower() == ".sql"]
    sql_lines = sum(map(count_sql_lines, sql_files))
    size = next((label for limit, label in SIZE_LIMITS if sql_lines < limit), "XXL")
    columns = ["—"] * 4
    if selected:
        paths_by_project = {config: [] for config in selected}
        for path in files:
            for parent in path.parents:
                config = parent / "dbt_project.yml"
                if config in configs:
                    if config in selected:
                        paths_by_project[config].append(path)
                    break
        totals = zip(*(project_facts(config, paths, files) for config, paths in paths_by_project.items()), strict=True)
        columns = [f"{sum(values):,}" for values in totals]

    label = settings["label"]
    url = settings.get("url")
    snapshot = f"{label} (`{commit[:12]}`)"
    stars = None
    if url and workload != "Private project":
        stars = github_stars(url)
        tree_path = "-/tree" if url.startswith("https://gitlab.com/") else "tree"
        snapshot = f"[{label}]({url}/{tree_path}/{commit})"
    cells = [snapshot, workload, "—" if stars is None else f"{stars:,}"]
    cells.extend([f"{len(sql_files):,}", f"{sql_lines:,}", size, *columns, settings["description"]])
    if audit_results is not None:
        cells.extend(audit_cells(name, commit, settings, audit_results))
    return "| " + " | ".join(cells) + " |"


def render_html(markdown):
    table = MarkdownIt("commonmark", {"html": False}).enable("table").render(markdown)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SQL repository inventory</title>
  <link rel="stylesheet" href="https://cdn.datatables.net/2.3.5/css/dataTables.dataTables.min.css">
  <style>
    body {{ margin: clamp(1rem, 3vw, 3rem); font: 15px/1.5 system-ui, sans-serif; color: #172333; }}
    h1 {{ margin: 0 0 1rem; font-size: 1.75rem; }}
    .report {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ padding: .65rem .8rem; border-bottom: 1px solid #dde4ec; }}
    thead {{ background: #eaf0f7; }}
    td:nth-child(n+4):not(:last-child) {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
    a {{ color: #1455a0; }}
    .column-guide {{ max-width: 76ch; margin-top: 1.5rem; }}
    .column-guide dt {{ font-weight: 650; }}
    .column-guide dd {{ margin: .2rem 0 .8rem; }}
  </style>
</head>
<body>
  <h1>SQL repository inventory</h1>
  <div class="report">{table}</div>
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
      <dt>Audit</dt>
      <dd>Completed projects out of those configured for this repository at the displayed commit.</dd>
      <dt>Join forms and cardinality</dt>
      <dd>Counts and percentages pooled over completed projects, using separate totals for SQL forms and sqlassert proof sites.</dd>
      <dt>Base table facts</dt>
      <dd>Yes means at least one completed project had a known source or base-table unique key or NOT NULL fact.</dd>
      <dt>YAML data tests</dt>
      <dd>Generic dbt data test declarations under <code>tests</code> or <code>data_tests</code> in project YAML.
        These tests check properties of the data, such as whether a column is unique.</dd>
      <dt>Supported types</dt>
      <dd>How many of those YAML declarations use a test type sqlassert recognizes.
        The types counted are <code>unique</code>, <code>not_null</code>, <code>accepted_values</code>,
        <code>relationships</code>, and <code>dbt_utils.unique_combination_of_columns</code>.
        Whether a specific test can be skipped depends on its configuration and what sqlassert proves.</dd>
      <dt>SQL data tests</dt>
      <dd>Standalone dbt test query files. Each query returns rows that fail the check.</dd>
    </dl>
    <p>Counts include checked-in integration projects. Zero means no matching declarations or files were found.
      A dash in a dbt column means the checkout has no dbt project.</p>
  </section>
  <script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
  <script src="https://cdn.datatables.net/2.3.5/js/dataTables.min.js"></script>
  <script>
    if (window.DataTable) {{
      const table = document.querySelector(".report table");
      table.classList.add("display", "compact");
      const sizes = {[label for _, label in SIZE_LIMITS] + ["XXL"]};
      for (const row of table.tBodies[0].rows) {{
        for (const cell of Array.from(row.cells).slice(3, 10)) {{
          const value = cell.textContent.trim();
          const sizeRank = sizes.indexOf(value);
          cell.dataset.order = sizeRank >= 0 ? sizeRank : value === "—" ? "-1" : value.replaceAll(",", "");
        }}
      }}
      new DataTable(table, {{ order: [], paging: false }});
    }}
  </script>
</body>
</html>"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="*", help="vendor directory names (default: all configured repos)")
    parser.add_argument("--html", action="store_true", help="render a browser report with sorting and search")
    args = parser.parse_args()
    try:
        config = load((VENDOR / "vendor_config.yaml").read_text(encoding="utf-8"), Loader=CSafeLoader)
        repositories = config["repositories"]
        audit_results = load_audit_results() if args.html else None
        rows = [repository_row(name, repositories[name], audit_results) for name in args.repos or repositories]
    except (KeyError, OSError, ValueError, YAMLError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    header, alignment = TABLE.splitlines()
    if args.html:
        header += " " + " | ".join(AUDIT_HEADERS) + " |"
        alignment += " --- |" * len(AUDIT_HEADERS)
    markdown = "\n".join([header, alignment, *rows])
    print(render_html(markdown) if args.html else markdown)


if __name__ == "__main__":
    main()
