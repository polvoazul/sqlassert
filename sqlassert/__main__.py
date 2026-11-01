"""Command line: `sqlassert suggest FILE` prints the improvements and markers sqlassert can prove."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlassert.main import DEFAULT_DIALECT
from sqlassert.suggest import DEFAULT_MAX_SIZE, suggest

# How each suggested Property reads in the listing.
_KINDS = {
    "ExactlyOneJoin": "Exactly One Join",
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
    arguments = parser.parse_args(argv)

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
    # Improvements (High) first, then markers (Mid).
    items = (*report.improvements, *report.suggestions)
    rows = [
        (i.level.value, f"line {i.site.line}", i.site.label, i.rewrite.value, i.message) for i in report.improvements
    ] + [
        (s.level.value, f"line {s.site.line}", s.site.label, _KINDS[type(s.property).__name__], s.marker)
        for s in report.suggestions
    ]
    widths = [max((len(row[i]) for row in rows), default=0) for i in range(4)]
    for row, item in zip(rows, items):
        print("  ".join(cell.ljust(width) for cell, width in zip(row[:4], widths)) + "  " + row[4])
        if arguments.explain:
            print("\n".join("    " + line for line in item.evidence.explain().splitlines()))
    return 1 if report.diagnostics else 0


if __name__ == "__main__":
    sys.exit(main())
