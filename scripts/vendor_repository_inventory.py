"""Count SQL and dbt facts in a vendored Git checkout, and fetch its GitHub stars."""

from collections import Counter
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import subprocess

from yaml import CSafeLoader, YAMLError, load


VENDOR = Path(__file__).resolve().parents[1] / "vendor"
PROPERTY_TESTS = {"unique", "not_null", "accepted_values", "relationships", "dbt_utils.unique_combination_of_columns"}
TEST_BLOCK = re.compile(rb"{%-?\s*test\s+")
SQL_COMMENT = re.compile(rb"--[^\r\n]*|/\*.*?(?:\*/|\Z)|\{#.*?(?:#\}|\Z)", re.DOTALL)
SIZE_LIMITS = ((200, "XS"), (1_000, "S"), (10_000, "M"), (100_000, "L"), (1_000_000, "XL"))
WORKLOADS = {
    "Published benchmark", "Real-world project", "Real-world project · used in research",
    "Private project", "Demo",
}


def git(repo, *args, input_bytes=None):
    return subprocess.check_output(["git", "-C", str(repo), *args], input=input_bytes)


def head(repo):
    return git(repo, "rev-parse", "--verify", "HEAD").decode().strip()


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


def count_sql_lines(sql):
    uncommented = SQL_COMMENT.sub(lambda match: b"\n" * match[0].count(b"\n"), sql)
    return sum(bool(line.strip()) for line in uncommented.splitlines())


def inventory(repo, commit, settings):
    """Count SQL files, SQL lines and dbt declarations at a commit."""
    if settings["workload"] not in WORKLOADS:
        raise ValueError(f"expected workload to be one of {', '.join(sorted(WORKLOADS))}")
    tree = {}
    scope = settings.get("scope", ".")
    for record in git(repo, "ls-tree", "-rz", commit, "--", scope).split(b"\0"):
        if record:
            metadata, path = record.split(b"\t", 1)
            mode, _, oid = metadata.split()
            if mode in {b"100644", b"100755"}:
                tree[PurePosixPath(path.decode())] = oid
    if not tree:
        raise ValueError(f"no tracked files in {scope}")
    configs = {path for path in tree if path.name == "dbt_project.yml"}
    selected = configs
    if "dbt_projects" in settings:
        selected = {PurePosixPath(folder) / "dbt_project.yml" for folder in settings["dbt_projects"]}
    if missing := selected - configs:
        raise ValueError(f"missing dbt configs: {', '.join(map(str, sorted(missing)))}")

    files = blobs(repo, {
        path: oid for path, oid in tree.items()
        if path.suffix.lower() == ".sql" or (selected and path.suffix.lower() in {".yml", ".yaml"})
    })
    sql_files = [content for path, content in files.items() if path.suffix.lower() == ".sql"]
    sql_lines = sum(map(count_sql_lines, sql_files))
    facts = {
        "sql_files": len(sql_files),
        "sql_lines": sql_lines,
        "size": next((label for limit, label in SIZE_LIMITS if sql_lines < limit), "XXL"),
        "dbt": None,
    }
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
        facts["dbt"] = dict(zip(("models", "yaml_tests", "supported_tests", "sql_tests"), map(sum, totals), strict=True))
    return facts


def github_stars(url):
    prefix = "https://github.com/"
    if not url or not url.startswith(prefix):
        return None
    slug = url.removeprefix(prefix).lower()
    return int(subprocess.check_output(["gh", "api", f"repos/{slug}", "--jq", ".stargazers_count"]))
