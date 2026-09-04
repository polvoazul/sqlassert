"""Run the property engine.

Clingo derives properties from ground facts: parsing, name resolution, and
SQL source locations stay outside the solver.
The engine asks for enough models to expose nondeterminism; whoever consumes
them enforces the one-model policy, because only the consumer can tell how many
it was handed.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources

import clingo

from sqlassert.facts import ClingoEncoding

_PROGRAM = "base"
_MODELS_TO_DETECT_NONDETERMINISM = 2


class EnginePolicyError(RuntimeError):
    """The rule program did not behave deterministically."""


@dataclass
class Engine:
    """Grounds one encoding and solves it, handing each model to a callback."""

    def run(self, encoding: ClingoEncoding, on_solution_callback: Callable[[clingo.Model], None]) -> None:
        control = clingo.Control()
        # clingo's Configuration is a dynamic C-extension proxy typed only as
        # `None | str | Configuration`; there is no narrower stub to satisfy.
        control.configuration.solve.models = str(_MODELS_TO_DETECT_NONDETERMINISM)  # ty: ignore[invalid-assignment]
        control.add(_PROGRAM, [], f"{rules()}\n{encoding.inheritance_rules}\n{encoding.facts}\n")
        control.ground([(_PROGRAM, [])])
        control.solve(on_model=on_solution_callback)


def rules() -> str:
    """Every rule resource, concatenated in a stable order."""
    directory = resources.files("sqlassert.rules")
    return "\n".join(
        resource.read_text(encoding="utf-8")
        for resource in sorted(directory.iterdir(), key=lambda entry: entry.name)
        if resource.name.endswith(".lp")
    )
