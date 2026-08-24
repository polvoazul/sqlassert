"""Consume the live Clingo model and retain only a durable Report.

The Reporter is created for one analysis and its bound `on_model` is handed to
the solver, so it may use Clingo's native `contains` and symbol APIs while the
model is alive. It never stores the model itself, and it never prints.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import clingo

from sqlassert import ir
from sqlassert.diagnostics import Diagnostic
from sqlassert.provenance import Origin, OriginRegistry

_PROVED = "proved"
_PROOF_KEY = "proof_key"
_UNIQUE_SET_MEMBER = "unique_set_member"


class Outcome(Enum):
    """PROVED or UNKNOWN. UNKNOWN fails an assertion but is not a Disproof."""

    PROVED = "proved"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AssertionReport:
    assertion_id: str
    outcome: Outcome
    origin: Origin
    proving_unique_set: tuple[str, ...] = ()


@dataclass(frozen=True)
class Report:
    assertions: tuple[AssertionReport, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    stable_model_count: int = 0

    @property
    def proved(self) -> bool:
        """Every assertion proved, and nothing about the program was unsupported."""
        return not self.diagnostics and all(
            assertion.outcome is Outcome.PROVED for assertion in self.assertions
        )


class Reporter:
    def __init__(self, assertions: Sequence[ir.UniqueJoinAssertion], origins: OriginRegistry) -> None:
        self._assertions = tuple(assertions)
        self._origins = origins
        self._reports: tuple[AssertionReport, ...] = ()
        self.stable_model_count = 0

    def on_model(self, model: clingo.Model) -> None:
        self.stable_model_count += 1
        if self.stable_model_count > 1:
            # A second model is an engine-policy failure; the engine raises.
            return

        proof_keys = _proof_keys(model)
        unique_sets = _unique_sets(model)
        self._reports = tuple(
            self._assertion_report(assertion, model, proof_keys, unique_sets)
            for assertion in self._assertions
        )

    def report(self, diagnostics: Sequence[Diagnostic] = ()) -> Report:
        return Report(self._reports, tuple(diagnostics), self.stable_model_count)

    def _assertion_report(
        self,
        assertion: ir.UniqueJoinAssertion,
        model: clingo.Model,
        proof_keys: dict[str, tuple[str, ...]],
        unique_sets: dict[str, tuple[str, ...]],
    ) -> AssertionReport:
        proved = model.contains(clingo.Function(_PROVED, [clingo.Function(assertion.id)]))
        keys = proof_keys.get(assertion.id, ())
        return AssertionReport(
            assertion_id=assertion.id,
            outcome=Outcome.PROVED if proved else Outcome.UNKNOWN,
            origin=self._origins.resolve(assertion.origin_id),
            proving_unique_set=unique_sets.get(keys[0], ()) if proved and keys else (),
        )


def _proof_keys(model: clingo.Model) -> dict[str, tuple[str, ...]]:
    """Which Unique Sets proved which assertion, read from the live model."""
    keys: dict[str, list[str]] = {}
    for symbol in model.symbols(atoms=True):
        if symbol.name == _PROOF_KEY and len(symbol.arguments) == 2:
            assertion, key = (str(argument) for argument in symbol.arguments)
            keys.setdefault(assertion, []).append(key)
    return {assertion: tuple(sorted(found)) for assertion, found in keys.items()}


def _unique_sets(model: clingo.Model) -> dict[str, tuple[str, ...]]:
    """The columns of every Unique Set, in declared order, read from the model."""
    members: dict[str, list[tuple[int, str]]] = {}
    for symbol in model.symbols(atoms=True):
        if symbol.name == _UNIQUE_SET_MEMBER and len(symbol.arguments) == 3:
            key, position, column = symbol.arguments
            members.setdefault(str(key), []).append((position.number, column.string))
    return {key: tuple(column for _, column in sorted(found)) for key, found in members.items()}
