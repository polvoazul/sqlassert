"""Command line: `sqlassert check PATH...` checks the assertions in SQL Programs for CI (`check.py`);
`sqlassert suggest FILE` prints the rewrites and markers sqlassert can prove.
dbt projects are checked by the separate `sqlassert-dbt` package (`integrations/dbt/`)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlassert")
    commands = parser.add_subparsers(dest="command", required=True)
    suggest_command = commands.add_parser(
        "suggest", help="list the rewrites and markers sqlassert can prove for a SQL Program"
    )
    suggest_command.add_argument("file", help="SQL Program to read, or - for stdin")
    suggest_command.add_argument("--dialect", default=argparse.SUPPRESS)
    suggest_command.add_argument(
        "--max-size", type=int, default=argparse.SUPPRESS, help="widest Unique Set searched exhaustively"
    )
    suggest_command.add_argument("--explain", action="store_true", help="print each suggestion's evidence")
    suggest_command.add_argument(
        "--annotate", action="store_true",
        help="paste every suggested marker into FILE (for -, print the annotated SQL instead of the list)",
    )
    check_command = commands.add_parser(
        "check",
        help="analyze SQL Programs and fail when an assertion is UNKNOWN or a Diagnostic is reported",
        description="Analyze each SQL Program from its SQL alone. Exit status: "
        "0 every assertion PROVED (warnings allowed), 1 an assertion UNKNOWN "
        "or a Diagnostic reported, 2 bad arguments, configuration, or unreadable SQL. "
        "Defaults come from [tool.sqlassert] in the nearest pyproject.toml; flags replace them, except --ignore, which adds to them.",
    )
    check_command.add_argument(
        "paths", nargs="*", metavar="PATH",
        help="SQL files, directories (every *.sql under them), or globs; - for stdin (default: [tool.sqlassert] paths)",
    )
    check_command.add_argument("--dialect", help="SQL dialect (default: [tool.sqlassert] dialect, else duckdb)")
    ignoring = check_command.add_mutually_exclusive_group()
    ignoring.add_argument(
        "--ignore", action="append", metavar="CODE",
        help="also drop Findings with this code, e.g. undeclared-table, besides [tool.sqlassert] ignore; "
        "repeatable or comma-separated",
    )
    ignoring.add_argument(
        "--only-ignore", action="append", metavar="CODE",
        help="drop Findings with these codes instead of [tool.sqlassert] ignore; repeatable or comma-separated "
        "(--only-ignore '' ignores nothing)",
    )
    check_command.add_argument("--format", choices=("github", "json", "sarif", "text"), default="text")
    check_command.add_argument("--explain", action="store_true", help="print each UNKNOWN assertion's evidence")
    arguments = parser.parse_args(argv)
    if arguments.command == "check":
        return _check(arguments)
    return _suggest(arguments)


def _suggest(arguments: argparse.Namespace) -> int:
    from sqlassert import check
    from sqlassert.suggest import suggest

    try:
        sql = check.read_sql(Path(arguments.file))
    except check.UsageError as error:
        print(f"sqlassert suggest: {error}", file=sys.stderr)
        return check.EXIT_USAGE
    options = {name: getattr(arguments, name) for name in ("dialect", "max_size") if hasattr(arguments, name)}
    report = suggest(sql, **options)
    for diagnostic in report.diagnostics:
        print(f"{diagnostic.code}: {diagnostic.message}", file=sys.stderr)
    for warning in report.warnings:
        print(f"warning: {warning.code}: {warning.message}", file=sys.stderr)
    if arguments.annotate and arguments.file == "-":
        sys.stdout.write(report.annotated())
        return 1 if report.diagnostics else 0
    if arguments.annotate and report.suggestions:
        Path(arguments.file).write_text(report.annotated())
    # Rewrites first, then markers, then hazards.
    items = (*report.rewrites, *report.suggestions, *report.hazards)
    rows = [
        (i.suggest_type.value, f"line {i.site.line}", i.site.label, i.kind.value, i.message) for i in report.rewrites
    ] + [
        (s.suggest_type.value, f"line {s.site.line}", s.site.label, s.kind, s.marker)
        for s in report.suggestions
    ] + [
        (s.suggest_type.value, f"line {s.site.line}", s.site.label, s.concern.value, s.message) for s in report.hazards
    ]
    widths = [max((len(row[i]) for row in rows), default=0) for i in range(4)]
    for row, item in zip(rows, items):
        print("  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths)) + "  " + row[4])
        if arguments.explain:
            print(check.indented(item.evidence.explain()))
    return 1 if report.diagnostics else 0


def _check(arguments: argparse.Namespace) -> int:
    from sqlassert import check

    try:
        config = check.load_config(Path.cwd())
        dialect = arguments.dialect or config.dialect
        ignore = (
            _codes(arguments.only_ignore) if arguments.only_ignore is not None
            else config.ignore | _codes(arguments.ignore or ())
        )
        check.validate(dialect, ignore)
        if arguments.paths:
            paths = check.expand(arguments.paths, Path.cwd())
        elif config.paths:
            paths = check.expand(config.paths, config.root)
        else:
            raise check.UsageError("no SQL to check: name files, directories or globs, or set paths in [tool.sqlassert]")
        programs = [(check.display(path), check.read_sql(path)) for path in paths]
    except check.UsageError as error:
        print(f"sqlassert check: {error}", file=sys.stderr)
        return check.EXIT_USAGE
    checked = [check.check(path, sql, dialect=dialect, ignore=ignore) for path, sql in programs]
    sys.stdout.write(check.FORMATS[arguments.format](checked, explain=arguments.explain))
    return check.exit_code(checked)


def _codes(values: Iterable[str]) -> frozenset[str]:
    """The codes of repeated, comma-separated flag values."""
    return frozenset(code.strip() for value in values for code in value.split(",") if code.strip())
