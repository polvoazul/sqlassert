"""Guards for the boundaries ADR 0002 draws.

Like the rule-policy test, this is a small structural guard rather than a
complete import analysis: it catches the boundary crossings that would let
SQLGlot nodes into the IR or Clingo symbols out of the engine.
"""

from __future__ import annotations

from pathlib import Path

import pytest


PACKAGE = Path(__file__).parents[1] / "sqlassert"

FORBIDDEN_IMPORTS = {
    # The IR and its inputs are plain immutable values.
    "ir/model.py": ("sqlglot", "clingo"),
    "knowledge.py": ("sqlglot", "clingo"),
    "provenance.py": ("sqlglot", "clingo"),
    "diagnostics.py": ("sqlglot", "clingo"),
    "naming.py": ("sqlglot", "clingo"),
    # SQL analysis never sees the solver.
    "sql_parse.py": ("clingo",),
    "ir/convert.py": ("clingo",),
    # Property reasoning and reporting never see the parser.
    "facts.py": ("sqlglot",),
    "engine.py": ("sqlglot",),
    "reporting.py": ("sqlglot",),
}


@pytest.mark.parametrize(("module", "forbidden"), sorted(FORBIDDEN_IMPORTS.items()))
def test_module_does_not_cross_its_boundary(module: str, forbidden: tuple[str, ...]):
    source = (PACKAGE / module).read_text()
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from "))
    }
    assert not imported & set(forbidden), f"{module} imports across its boundary: {sorted(imported & set(forbidden))}"
