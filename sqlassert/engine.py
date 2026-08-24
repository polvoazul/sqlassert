"""Run the property engine.

Clingo derives properties and their supporting evidence from ground facts and
nothing else: parsing, name resolution, and provenance stay outside the solver.
A valid analysis has exactly one stable model; more than one is an engine-policy
failure rather than a result.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Protocol

import clingo

from sqlassert import ir
from sqlassert.facts import ground_facts
from sqlassert.knowledge import Knowledge
from sqlassert.naming import NameGiver

_PROGRAM = "base"
_MODELS_TO_DETECT_NONDETERMINISM = 2


class EnginePolicyError(RuntimeError):
    """The rule program did not behave deterministically."""


class ModelConsumer(Protocol):
    stable_model_count: int

    def on_model(self, model: clingo.Model) -> None: ...


@dataclass
class Engine:
    """Grounds one program and solves it, reporting through its consumer.

    The consumer is bound at construction because it accumulates the results of
    this one solve, so an instance analyses exactly one program.
    """

    consumer: ModelConsumer
    names: NameGiver

    def run(self, program: ir.Program, knowledge: Knowledge) -> None:
        facts = ground_facts(program, knowledge, self.names)

        control = clingo.Control()
        control.configuration.solve.models = str(_MODELS_TO_DETECT_NONDETERMINISM)
        control.add(_PROGRAM, [], f"{rules()}\n{facts}\n")
        control.ground([(_PROGRAM, [])])
        control.solve(on_model=self.consumer.on_model)

        if self.consumer.stable_model_count != 1:
            raise EnginePolicyError(
                "analysis requires exactly one stable model, the rule program produced "
                f"{self.consumer.stable_model_count}"
            )


def rules() -> str:
    """Every rule resource, concatenated in a stable order."""
    directory = resources.files("sqlassert.rules")
    return "\n".join(
        resource.read_text(encoding="utf-8")
        for resource in sorted(directory.iterdir(), key=lambda entry: entry.name)
        if resource.name.endswith(".lp")
    )
