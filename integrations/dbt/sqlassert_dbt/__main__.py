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
import secrets
import shutil
import tempfile
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from sqlassert.check import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE
from sqlassert_dbt.project import ProjectError, analyze_project, suggest_models
from sqlassert_dbt.report import (
    DEFAULT_WARNING_LIMIT, DbtReport, Location, SuggestReport, WarningLimit, exit_code, format_suggestion_blocks, format_suggestion_summary,
    format_text,
)

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
    _catalog(suggest)
    _warning_limit(suggest)
    suggest.add_argument(
        "--dbt-compile-inplace", action="store_true",
        help="let dbt compile into the project's own target/ and logs/ (keeping its partial-parse cache) "
        "instead of a temporary directory",
    )
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
        report = _analyze(project, arguments.manifest, arguments.dialect, arguments.trust_upstream_tests, arguments)
        if report is None:
            return EXIT_USAGE
        sys.stdout.write(format_text(report, explain=arguments.explain, warning_limit=arguments.warning_limit))
        return exit_code(report, require_proved=arguments.require_proved)

    passed = [argument for argument in arguments.dbt_arguments if argument != "--"]
    located = [*passed, "--project-dir", str(project), *_profiles(project.resolve(), passed)]
    compiled = run_dbt(["compile", *located])
    if compiled != 0:
        return compiled
    selecting = any(argument.split("=")[0] in SELECTION_FLAGS for argument in passed)
    report = _analyze(project, None, arguments.dialect, not selecting, arguments)
    if report is None:
        return EXIT_USAGE
    sys.stdout.write(format_text(report, explain=arguments.explain, warning_limit=arguments.warning_limit))
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
    # dbt writes target/ and logs/ as it compiles; suggest leaves the project's
    # own alone, unless the caller names where.
    scratch = Path(tempfile.mkdtemp(prefix="sqlassert-dbt-"))
    shortcut = None
    try:
        given = _option(passed, "--target-path")
        inplace = arguments.dbt_compile_inplace
        target = project / given if given is not None else project / "target" if inplace else scratch / "target"
        if given is None and not inplace:
            located += ["--target-path", str(target)]
            shortcut = _Shortcut(target / "compiled")
        if _option(passed, "--log-path") is None and not inplace:
            located += ["--log-path", str(scratch / "logs")]
        compiled = run_dbt(["compile", *located], to_stderr=True)
        if compiled != 0:
            return compiled
        return _report_suggestions(arguments, project, relative, target / "manifest.json", shortcut)
    finally:
        if shortcut is not None and shortcut.link is not None:
            print(f"sqlassert-dbt: compiled code kept for the locations above: {shortcut.link} -> {shortcut.compiled}", file=sys.stderr)
        else:
            shutil.rmtree(scratch, ignore_errors=True)


# Where the link goes: /tmp is short; macOS's own temporary directory is not.
_SHORT_ROOT = Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())


class _Shortcut:
    """A short link, made on first use, to the compiled code in suggest's own
    temporary target: a location dbt's rendering cannot map back to the model
    file points at the compiled file, and the link keeps that path short and
    the file there after the run."""

    def __init__(self, compiled: Path) -> None:
        self.compiled = compiled
        self.link: Path | None = None

    def location(self, location: Location) -> Location:
        path = Path(location.path)
        if not path.is_absolute() or not path.is_relative_to(self.compiled):
            return location
        if self.link is None:
            while True:
                link = _SHORT_ROOT / f"sqlassert-{secrets.token_hex(3)}"
                try:
                    link.symlink_to(self.compiled, target_is_directory=True)
                    break
                except FileExistsError:
                    continue
            self.link = link
        return replace(location, path=str(self.link / path.relative_to(self.compiled)))

    def report(self, report: SuggestReport) -> SuggestReport:
        return replace(
            report,
            suggestions=tuple(replace(item, location=self.location(item.location)) for item in report.suggestions),
            problems=tuple(
                replace(problem, location=self.location(problem.location)) if problem.location is not None else problem
                for problem in report.problems
            ),
        )


def _report_suggestions(
    arguments: argparse.Namespace, project: Path, relative: list[str], manifest: Path, shortcut: _Shortcut | None = None,
) -> int:
    limit = WarningLimit(arguments.warning_limit)

    def shown(part: SuggestReport) -> None:
        part = shortcut.report(part) if shortcut is not None else part
        print(format_suggestion_blocks(part, explain=arguments.explain, limit=limit), end="", flush=True)

    try:
        report = suggest_models(
            project, relative, manifest_path=manifest, dialect=arguments.dialect,
            trust_upstream_tests=arguments.trust_upstream_tests,
            catalog_path=_path(arguments.catalog), read_catalog=not arguments.no_catalog,
            on_model=shown,
        )
    except ProjectError as error:
        print(f"sqlassert-dbt: {error}", file=sys.stderr)
        return EXIT_USAGE
    sys.stdout.write(format_suggestion_summary(report, limit))
    return EXIT_FINDINGS if report.failed else EXIT_OK


_CATALOG_HELP = (
    "read the columns and types of sources, seeds and other tables from this catalog.json instead of "
    "PROJECT/target/catalog.json, which is read when present (`dbt docs generate` writes it)"
)


def _catalog(command: argparse.ArgumentParser) -> None:
    reading = command.add_mutually_exclusive_group()
    reading.add_argument("--catalog", help=_CATALOG_HELP)
    reading.add_argument(
        "--no-catalog", action="store_true", help="read no catalog.json: the manifest's columns only",
    )


def _warning_limit(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--warning-limit", type=int, default=DEFAULT_WARNING_LIMIT, metavar="N",
        help=f"print at most N warnings of each code, then say the rest are suppressed (default {DEFAULT_WARNING_LIMIT}; 0: all)",
    )


def _common(command: argparse.ArgumentParser) -> None:
    command.add_argument("project", nargs="?", default=".", help="dbt project directory (default: .)")
    _warning_limit(command)
    command.add_argument("--dialect", help="analyze under this dialect instead of the adapter's")
    _catalog(command)
    command.add_argument("--explain", action="store_true", help="print each proof and failed proof")
    command.add_argument(
        "--require-proved", action="store_true",
        help=f"exit {EXIT_FINDINGS} when a dbt test is not proved from the SQL, too, not only a marker",
    )


def proved_tests(report: DbtReport) -> list[str]:
    """The names of the tests the SQL proves, to pass dbt as `--exclude`."""
    return sorted({test.name for test in report.tests if test.skippable})


def _analyze(
    project: Path, manifest: str | None, dialect: str | None, trust: bool, arguments: argparse.Namespace,
) -> DbtReport | None:
    try:
        return analyze_project(
            project, manifest_path=_path(manifest), dialect=dialect, trust_upstream_tests=trust,
            catalog_path=_path(arguments.catalog), read_catalog=not arguments.no_catalog,
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


def _option(passed: list[str], name: str) -> str | None:
    """The value the caller gave dbt for `name` (`--name V` or `--name=V`)."""
    for index, argument in enumerate(passed):
        if argument == name and index + 1 < len(passed):
            return passed[index + 1]
        if argument.startswith(name + "="):
            return argument.split("=", 1)[1]
    return None


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


if __name__ == "__main__":
    sys.exit(main())
