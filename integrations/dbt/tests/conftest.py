"""`compiled`: `tests/dbt_project` compiled by a real `dbt compile`, once per
session. Every test reads the manifest dbt writes, never a hand-written one."""

from __future__ import annotations

from pathlib import Path

import pytest
from dbt_runs import copy_source, dbt


@pytest.fixture(scope="session")
def compiled(tmp_path_factory: pytest.TempPathFactory) -> Path:
    project = copy_source(tmp_path_factory.mktemp("compiled"))
    assert dbt(project, "compile"), "dbt compile failed on tests/dbt_project"
    return project
