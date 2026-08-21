"""Run the property engine.

Clingo derives properties and their supporting evidence from ground facts and
nothing else: parsing, name binding, and provenance stay outside the solver. A
valid analysis has exactly one stable model; more than one is an engine-policy
failure rather than a result.
"""

from __future__ import annotations

from importlib import resources
from typing import Protocol

import clingo

from sqlassert.facts import GroundFacts

_PROGRAM = "base"
_MODELS_TO_DETECT_NONDETERMINISM = 2


class EnginePolicyError(RuntimeError):
    """The rule program did not behave deterministically."""


class ModelConsumer(Protocol):
    stable_model_count: int

    def on_model(self, model: clingo.Model) -> None: ...


def run(facts: GroundFacts, consumer: ModelConsumer) -> None:
    control = clingo.Control()
    control.configuration.solve.models = str(_MODELS_TO_DETECT_NONDETERMINISM)
    control.add(_PROGRAM, [], f"{rules()}\n{facts.text}\n")
    control.ground([(_PROGRAM, [])])
    control.solve(on_model=consumer.on_model)

    if consumer.stable_model_count != 1:
        raise EnginePolicyError(
            f"analysis requires exactly one stable model, the rule program produced {consumer.stable_model_count}"
        )


def rules() -> str:
    """Every rule resource, concatenated in a stable order."""
    directory = resources.files("sqlassert.rules")
    return "\n".join(
        resource.read_text(encoding="utf-8")
        for resource in sorted(directory.iterdir(), key=lambda entry: entry.name)
        if resource.name.endswith(".lp")
    )
