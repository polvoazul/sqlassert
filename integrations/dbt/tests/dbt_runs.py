"""Run dbt itself (dbt-duckdb, in memory, no warehouse) on copies of
`tests/dbt_project`."""

from __future__ import annotations

import shutil
from pathlib import Path

from dbt.cli.main import dbtRunner

SOURCE = Path(__file__).parent / "dbt_project"


def copy_source(directory: Path) -> Path:
    project = directory / "project"
    shutil.copytree(SOURCE, project, ignore=shutil.ignore_patterns("target", "logs"))
    return project


def dbt(project: Path, *arguments: str) -> bool:
    """Run one dbt command on `project` in process; whether it succeeded."""
    result = dbtRunner().invoke([
        "--quiet", "--no-send-anonymous-usage-stats", "--no-partial-parse", "--log-path", str(project / "logs"),
        *arguments, "--project-dir", str(project), "--profiles-dir", str(project),
    ])
    return result.success
