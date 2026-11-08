"""Command line: `sqlassert-dbt [PROJECT]` checks a compiled dbt project's
tests and markers from its `target/manifest.json`, with `sqlassert check`'s
exit codes."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlassert.check import EXIT_FINDINGS, EXIT_OK, EXIT_USAGE
from sqlassert_dbt.project import ProjectError, analyze_project
from sqlassert_dbt.report import exit_code, format_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sqlassert-dbt",
        description="Check a dbt project's tests and markers from its target/manifest.json (run `dbt compile` first). "
        f"Exit status: {EXIT_OK} every marker PROVED, {EXIT_FINDINGS} a marker UNKNOWN or a diagnostic reported, "
        f"{EXIT_USAGE} the manifest cannot be read.",
    )
    parser.add_argument("project", nargs="?", default=".", help="dbt project directory (default: .)")
    parser.add_argument("--manifest", help="manifest.json to read (default: PROJECT/target/manifest.json)")
    parser.add_argument("--dialect", help="analyze under this dialect instead of the adapter's")
    parser.add_argument("--explain", action="store_true", help="print each proof and failed proof")
    arguments = parser.parse_args(argv)
    try:
        report = analyze_project(
            Path(arguments.project),
            manifest_path=Path(arguments.manifest) if arguments.manifest else None,
            dialect=arguments.dialect,
        )
    except ProjectError as error:
        print(f"sqlassert-dbt: {error}", file=sys.stderr)
        return EXIT_USAGE
    sys.stdout.write(format_text(report, explain=arguments.explain))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
