"""Command line.

`sqlassert-dbt check [PROJECT]` checks a compiled dbt project's tests and
markers from its `target/manifest.json`, with `sqlassert check`'s exit codes.

`sqlassert-dbt build|test [PROJECT] [-- DBT_ARGS]` runs dbt with the markers
checked too: `dbt compile`, then the check, then `dbt build` (or `dbt test`)
with every test the SQL proves excluded. dbt runs the tests of what a model
reads in the same invocation, so they are facts, unless DBT_ARGS select nodes
(a selection may leave an upstream test out).

`sqlassert-dbt suggest PATH... [-- DBT_ARGS]` runs `dbt compile` on the
models in those files or directories and what they read, then lists what `sqlassert suggest` finds in
each: rewrites, markers to paste, smells, at the model file's lines.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from sqlassert.check import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE
from sqlassert_dbt.project import ProjectError, analyze_project, suggest_models
from sqlassert_dbt.report import DbtReport, exit_code, format_suggestions, format_text

# The dbt flags that narrow which nodes run: under any of them, the tests of
# what a model reads may not run, so they are no facts.
SELECTION_FLAGS = frozenset({"-s", "--select", "-m", "--models", "--model", "--exclude", "--selector", "--resource-type", "--resource-types"})


def run_dbt(arguments: list[str], *, to_stderr: bool = False) -> int:
    """Run dbt from PATH with `arguments`; its exit status. `to_stderr` sends
    its output there, leaving stdout to the report."""
    try:
        return subprocess.run(["dbt", *arguments], check=False, stdout=sys.stderr if to_stderr else None).returncode
    except FileNotFoundError:
        print("sqlassert-dbt: dbt is not on PATH", file=sys.stderr)
        return EXIT_USAGE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlassert-dbt")
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check",
        help="check the tests and markers of a compiled project, without dbt",
        description="Check a dbt project's tests and markers from its target/manifest.json (run `dbt compile` first). "
        f"Exit status: {EXIT_OK} every marker PROVED, {EXIT_FINDINGS} a marker UNKNOWN or a diagnostic reported, "
        f"{EXIT_USAGE} the manifest cannot be read.",
    )
    _common(check)
    check.add_argument("--manifest", help="manifest.json to read (default: PROJECT/target/manifest.json)")
    check.add_argument(
        "--trust-upstream-tests", action="store_true",
        help="take the passing tests of what each model reads as facts about it: true when they run first, as in `dbt build`",
    )
    for name, verb in (("build", "dbt build"), ("test", "dbt test")):
        command = commands.add_parser(
            name,
            help=f"`dbt compile`, check the markers, then `{verb}` without the tests the SQL proves",
            description=f"Run `dbt compile`, check the project, then `{verb}` excluding every test the SQL proves. "
            f"Arguments after -- go to dbt. Exit status: dbt's when it fails, else {EXIT_FINDINGS} when a marker is "
            f"UNKNOWN or a diagnostic is reported, else {EXIT_OK}.",
        )
        _common(command)
        command.add_argument("dbt_arguments", nargs=argparse.REMAINDER, help="-- then arguments for dbt")
    suggest = commands.add_parser(
        "suggest",
        help="`dbt compile`, then list the rewrites, markers and smells sqlassert finds in model files",
        description="Run `dbt compile` on the models and what they read, then list what sqlassert can prove about each "
        "model file: rewrites that keep its result, markers to paste, smells. Arguments after -- go to dbt. "
        f"Exit status: {EXIT_OK}, or {EXIT_FINDINGS} when an error is reported, {EXIT_USAGE} on bad arguments.",
        usage="sqlassert-dbt suggest [options] PATH... [-- DBT_ARGS]",
    )
    suggest.add_argument(
        "files", nargs="+", metavar="PATH",
        help="model .sql files or directories of models (the project's own directory: every model) of one dbt project",
    )
    suggest.add_argument(
        "--project-dir", help="dbt project directory (default: the nearest one holding the first PATH)",
    )
    suggest.add_argument("--dialect", help="analyze under this dialect instead of the adapter's")
    suggest.add_argument("--catalog", help=_CATALOG_HELP)
    suggest.add_argument("--explain", action="store_true", help="print each suggestion's evidence")
    suggest.add_argument(
        "--trust-upstream-tests", action="store_true",
        help="take the passing tests of what each model reads as facts about it: true when they run first, as in `dbt build`",
    )
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["suggest"] and "--" in argv:
        # Files take every positional, so the dbt arguments are split off by hand.
        split = argv.index("--")
        arguments = parser.parse_args(argv[:split])
        return _suggest(arguments, argv[split + 1:])
    arguments = parser.parse_args(argv)
    if arguments.command == "suggest":
        return _suggest(arguments, [])
    project = Path(arguments.project)

    if arguments.command == "check":
        report = _analyze(project, arguments.manifest, arguments.dialect, arguments.trust_upstream_tests, arguments.catalog)
        if report is None:
            return EXIT_USAGE
        sys.stdout.write(format_text(report, explain=arguments.explain))
        return exit_code(report, require_proved=arguments.require_proved)

    passed = [argument for argument in arguments.dbt_arguments if argument != "--"]
    located = [*passed, "--project-dir", str(project), *_profiles(project.resolve(), passed)]
    compiled = run_dbt(["compile", *located])
    if compiled != 0:
        return compiled
    selecting = any(argument.split("=")[0] in SELECTION_FLAGS for argument in passed)
    report = _analyze(project, None, arguments.dialect, not selecting, arguments.catalog)
    if report is None:
        return EXIT_USAGE
    sys.stdout.write(format_text(report, explain=arguments.explain))
    proved = proved_tests(report)
    ran = run_dbt([arguments.command, *located, *(["--exclude", *proved] if proved else [])])
    return ran if ran != 0 else exit_code(report, require_proved=arguments.require_proved)


def _suggest(arguments: argparse.Namespace, passed: list[str]) -> int:
    paths = [Path(path).resolve() for path in arguments.files]
    if arguments.project_dir is not None:
        project = Path(arguments.project_dir).resolve()
    else:
        first = paths[0]
        candidates = [first, *first.parents] if first.is_dir() else list(first.parents)
        project = next((folder for folder in candidates if (folder / "dbt_project.yml").is_file()), None)
        if project is None:
            print(f"sqlassert-dbt: no dbt_project.yml at or above {arguments.files[0]}; pass --project-dir", file=sys.stderr)
            return EXIT_USAGE
    relative = []
    for given, path in zip(arguments.files, paths):
        if not path.is_relative_to(project):
            print(f"sqlassert-dbt: {given} is outside the dbt project {project}", file=sys.stderr)
            return EXIT_USAGE
        relative.append(path.relative_to(project).as_posix())
    selecting = any(argument.split("=")[0] in SELECTION_FLAGS for argument in passed)
    # Only the models and what they read need compiled code; the whole project, for its own directory.
    selection = [] if selecting or "." in relative else ["--select", *(f"+path:{path}" for path in relative)]
    located = [*passed, *selection, "--project-dir", str(project), *_profiles(project, passed)]
    compiled = run_dbt(["compile", *located], to_stderr=True)
    if compiled != 0:
        return compiled
    try:
        report = suggest_models(
            project, relative, dialect=arguments.dialect, trust_upstream_tests=arguments.trust_upstream_tests,
            catalog_path=_path(arguments.catalog),
        )
    except ProjectError as error:
        print(f"sqlassert-dbt: {error}", file=sys.stderr)
        return EXIT_USAGE
    sys.stdout.write(format_suggestions(report, explain=arguments.explain))
    return EXIT_FINDINGS if report.failed else EXIT_OK


_CATALOG_HELP = (
    "read the columns and types of sources, seeds and other tables from this catalog.json (`dbt docs generate` "
    "writes target/catalog.json); trusted like a connection"
)


def _common(command: argparse.ArgumentParser) -> None:
    command.add_argument("project", nargs="?", default=".", help="dbt project directory (default: .)")
    command.add_argument("--dialect", help="analyze under this dialect instead of the adapter's")
    command.add_argument("--catalog", help=_CATALOG_HELP)
    command.add_argument("--explain", action="store_true", help="print each proof and failed proof")
    command.add_argument(
        "--require-proved", action="store_true",
        help=f"exit {EXIT_FINDINGS} when a dbt test is not proved from the SQL, too, not only a marker",
    )


def proved_tests(report: DbtReport) -> list[str]:
    """The names of the tests the SQL proves, to pass dbt as `--exclude`."""
    return sorted({test.name for test in report.tests if test.skippable})


def _analyze(project: Path, manifest: str | None, dialect: str | None, trust: bool, catalog: str | None) -> DbtReport | None:
    try:
        return analyze_project(
            project, manifest_path=_path(manifest), dialect=dialect, trust_upstream_tests=trust, catalog_path=_path(catalog),
        )
    except ProjectError as error:
        print(f"sqlassert-dbt: {error}", file=sys.stderr)
        return None


def _profiles(project: Path, passed: list[str]) -> list[str]:
    """`--profiles-dir PROJECT` when the project holds the profiles.yml dbt
    would not otherwise find: dbt looks in the working directory, then
    DBT_PROFILES_DIR, then ~/.dbt, never in the project."""
    if any(argument.split("=")[0] == "--profiles-dir" for argument in passed) or os.environ.get("DBT_PROFILES_DIR"):
        return []
    if not (project / "profiles.yml").is_file() or (Path.cwd() / "profiles.yml").is_file():
        return []
    return ["--profiles-dir", str(project)]


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


if __name__ == "__main__":
    sys.exit(main())
