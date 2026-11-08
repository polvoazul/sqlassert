"""dbt integration: check a dbt project's tests and Property Markers from its manifest, without dbt.

`analyze_project(project_dir)` reads `target/manifest.json` and returns a
`DbtReport`: which tests the SQL proves (skippable), which it does not, which
proved Properties no test states, and the outcome of every marker written in
a model file, mapped back to that file's lines.
"""

from sqlassert.check import EXIT_USAGE
from sqlassert.dbt.project import SUPPORTED_DIALECTS, ProjectError, analyze_project
from sqlassert.dbt.report import (
    DbtReport, Location, MarkerOutcome, Problem, Status, TestOutcome, Untested,
    exit_code, format_text,
)

__all__ = [
    "SUPPORTED_DIALECTS", "DbtReport", "Location", "MarkerOutcome", "Problem",
    "ProjectError", "Status", "TestOutcome", "Untested", "analyze_project", "command", "exit_code", "format_text",
]


def command(project_dir: str, *, manifest: str | None = None, dialect: str | None = None, explain: bool = False) -> int:
    """`sqlassert dbt`: print the report, and return the exit code (0 ok,
    1 findings, 2 the project cannot be read). The one seam a richer CLI
    (output formats, config) replaces."""
    import sys
    from pathlib import Path

    try:
        report = analyze_project(Path(project_dir), manifest_path=Path(manifest) if manifest else None, dialect=dialect)
    except ProjectError as error:
        print(f"sqlassert dbt: {error}", file=sys.stderr)
        return EXIT_USAGE
    sys.stdout.write(format_text(report, explain=explain))
    return exit_code(report)
