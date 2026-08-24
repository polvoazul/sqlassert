"""Run the property engine.

Clingo derives properties and their supporting evidence from ground facts and
nothing else: parsing, name resolution, and provenance stay outside the solver.
The engine asks for enough models to expose nondeterminism; whoever consumes
them enforces the one-model policy, because only the consumer can tell how many
it was handed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources

import clingo

from sqlassert.facts import ground_facts
from sqlassert.ir.convert import IrConversionResult
from sqlassert.naming import NameGiver

_PROGRAM = "base"
_MODELS_TO_DETECT_NONDETERMINISM = 2


class EnginePolicyError(RuntimeError):
    """The rule program did not behave deterministically."""


@dataclass
class Engine:
    """Grounds one conversion and solves it, handing each model to a callback."""

    names: NameGiver

    def run(self, ir: IrConversionResult, on_solution_callback: Callable[[clingo.Model], None]) -> None:
        facts = ground_facts(ir.program, ir.knowledge, self.names)

        control = clingo.Control()
        control.configuration.solve.models = str(_MODELS_TO_DETECT_NONDETERMINISM)
        control.add(_PROGRAM, [], f"{rules()}\n{facts}\n")
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
