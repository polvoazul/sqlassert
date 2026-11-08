"""dbt integration: check a dbt project's tests and Property Markers from its manifest, without dbt.

`analyze_project(project_dir)` reads `target/manifest.json` and returns a
`DbtReport`: which tests the SQL proves (skippable), which it does not, which
proved Properties no test states, and the outcome of every marker written in
a model file, mapped back to that file's lines. `sqlassert-dbt` is its command
line (`__main__.py`).
"""

from sqlassert_dbt.project import SUPPORTED_DIALECTS, ProjectError, analyze_project
from sqlassert_dbt.report import (
    DbtReport, Location, MarkerOutcome, Problem, Status, TestOutcome, Untested,
    exit_code, format_text,
)

__all__ = [
    "SUPPORTED_DIALECTS", "DbtReport", "Location", "MarkerOutcome", "Problem",
    "ProjectError", "Status", "TestOutcome", "Untested", "analyze_project", "exit_code", "format_text",
]
