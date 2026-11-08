"""Command line.

`sqlassert-dbt check [PROJECT]` checks a compiled dbt project's tests and
markers from its `target/manifest.json`, with `sqlassert check`'s exit codes.

`sqlassert-dbt build|test [PROJECT] [-- DBT_ARGS]` runs dbt with the markers
checked too: `dbt compile`, then the check, then `dbt build` (or `dbt test`)
with every test the SQL proves excluded. dbt runs the tests of what a model
reads in the same invocation, so they are facts, unless DBT_ARGS select nodes
(a selection may leave an upstream test out).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from sqlassert.check import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE
from sqlassert_dbt.project import ProjectError, analyze_project
from sqlassert_dbt.report import DbtReport, exit_code, format_text

# The dbt flags that narrow which nodes run: under any of them, the tests of
# what a model reads may not run, so they are no facts.
SELECTION_FLAGS = frozenset({"-s", "--select", "-m", "--models", "--model", "--exclude", "--selector", "--resource-type", "--resource-types"})


def run_dbt(arguments: list[str]) -> int:
    """Run dbt from PATH with `arguments`; its exit status."""
    try:
        return subprocess.run(["dbt", *arguments], check=False).returncode
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
    arguments = parser.parse_args(argv)
    project = Path(arguments.project)

    if arguments.command == "check":
        report = _analyze(project, arguments.manifest, arguments.dialect, arguments.trust_upstream_tests)
        if report is None:
            return EXIT_USAGE
        sys.stdout.write(format_text(report, explain=arguments.explain))
        return exit_code(report)

    passed = [argument for argument in arguments.dbt_arguments if argument != "--"]
    located = [*passed, "--project-dir", str(project)]
    compiled = run_dbt(["compile", *located])
    if compiled != 0:
        return compiled
    selecting = any(argument.split("=")[0] in SELECTION_FLAGS for argument in passed)
    report = _analyze(project, None, arguments.dialect, not selecting)
    if report is None:
        return EXIT_USAGE
    sys.stdout.write(format_text(report, explain=arguments.explain))
    proved = proved_tests(report)
    ran = run_dbt([arguments.command, *located, *(["--exclude", *proved] if proved else [])])
    return ran if ran != 0 else exit_code(report)


def _common(command: argparse.ArgumentParser) -> None:
    command.add_argument("project", nargs="?", default=".", help="dbt project directory (default: .)")
    command.add_argument("--dialect", help="analyze under this dialect instead of the adapter's")
    command.add_argument("--explain", action="store_true", help="print each proof and failed proof")


def proved_tests(report: DbtReport) -> list[str]:
    """The names of the tests the SQL proves, to pass dbt as `--exclude`."""
    return sorted({test.name for test in report.tests if test.skippable})


def _analyze(project: Path, manifest: str | None, dialect: str | None, trust: bool) -> DbtReport | None:
    try:
        return analyze_project(
            project, manifest_path=Path(manifest) if manifest else None, dialect=dialect, trust_upstream_tests=trust,
        )
    except ProjectError as error:
        print(f"sqlassert-dbt: {error}", file=sys.stderr)
        return None


if __name__ == "__main__":
    sys.exit(main())
