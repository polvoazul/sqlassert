"""Command line: `sqlassert suggest FILE` prints the improvements and markers sqlassert can prove;
`sqlassert dbt [PROJECT]` checks a dbt project's tests and markers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlassert.main import DEFAULT_DIALECT
from sqlassert.suggest import DEFAULT_MAX_SIZE, suggest

# How each suggested Property reads in the listing.
_KINDS = {
    "ExactlyOneJoin": "Exactly One Join",
    "OneToOneJoin": "One To One Join",
    "ManyToOneJoin": "Many To One Join",
    "OneToManyJoin": "One To Many Join",
    "UniqueJoin": "Unique Join",
    "UniqueSet": "Unique Set",
    "NonNullUniqueSet": "Non-Null Unique Set",
    "NonNullColumns": "Not Null",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sqlassert")
    commands = parser.add_subparsers(dest="command", required=True)
    suggest_command = commands.add_parser(
        "suggest", help="list the improvements and markers sqlassert can prove for a SQL Program"
    )
    suggest_command.add_argument("file", help="SQL Program to read, or - for stdin")
    suggest_command.add_argument("--dialect", default=DEFAULT_DIALECT)
    suggest_command.add_argument(
        "--max-size", type=int, default=DEFAULT_MAX_SIZE, help="widest Unique Set searched exhaustively"
    )
    suggest_command.add_argument("--explain", action="store_true", help="print each suggestion's evidence")
    suggest_command.add_argument(
        "--annotate", action="store_true",
        help="paste every suggested marker into FILE (for -, print the annotated SQL instead of the list)",
    )
    dbt_command = commands.add_parser("dbt", help="check a dbt project's tests and markers from its target/manifest.json")
    dbt_command.add_argument("project", nargs="?", default=".", help="dbt project directory (default: .)")
    dbt_command.add_argument("--manifest", help="manifest.json to read (default: PROJECT/target/manifest.json)")
    dbt_command.add_argument("--dialect", help="analyze under this dialect instead of the adapter's")
    dbt_command.add_argument("--explain", action="store_true", help="print each proof and failed proof")
    arguments = parser.parse_args(argv)
    if arguments.command == "dbt":
        from sqlassert.dbt import command

        return command(arguments.project, manifest=arguments.manifest, dialect=arguments.dialect, explain=arguments.explain)

    sql = sys.stdin.read() if arguments.file == "-" else Path(arguments.file).read_text()
    report = suggest(sql, dialect=arguments.dialect, max_size=arguments.max_size)
    for diagnostic in report.diagnostics:
        print(f"{diagnostic.code}: {diagnostic.message}", file=sys.stderr)
    for warning in report.warnings:
        print(f"warning: {warning.code}: {warning.message}", file=sys.stderr)
    if arguments.annotate and arguments.file == "-":
        sys.stdout.write(report.annotated())
        return 1 if report.diagnostics else 0
    if arguments.annotate and report.suggestions:
        Path(arguments.file).write_text(report.annotated())
    # Improvements (High) first, then markers (Mid), then smells (Low).
    items = (*report.improvements, *report.suggestions, *report.smells)
    rows = [
        (i.level.value, f"line {i.site.line}", i.site.label, i.rewrite.value, i.message) for i in report.improvements
    ] + [
        (s.level.value, f"line {s.site.line}", s.site.label, _KINDS[type(s.property).__name__], s.marker)
        for s in report.suggestions
    ] + [
        (s.level.value, f"line {s.site.line}", s.site.label, s.concern.value, s.message) for s in report.smells
    ]
    widths = [max((len(row[i]) for row in rows), default=0) for i in range(4)]
    for row, item in zip(rows, items):
        print("  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths)) + "  " + row[4])
        if arguments.explain:
            print("\n".join("    " + line for line in item.evidence.explain().splitlines()))
    return 1 if report.diagnostics else 0


if __name__ == "__main__":
    sys.exit(main())
