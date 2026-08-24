"""Consume the live Clingo model and retain only a durable Report.

`on_model` runs while the model is alive, so it may use Clingo's native symbol
APIs — but it keeps only plain values, never the model. Assembling the Report
happens afterwards, which is why nothing about the assertions is needed until
then. The Reporter never prints.

Being the one thing that sees every model, the Reporter is also what enforces
the one-model policy: `on_model` refuses a second call, and `report` refuses to
assemble without a first.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

import clingo

from sqlassert import ir
from sqlassert.diagnostics import Diagnostic
from sqlassert.engine import EnginePolicyError
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
    """Evidence harvested from one solve, and the Report assembled from it."""

    def __init__(self, origins: OriginRegistry) -> None:
        self._origins = origins
        self._proved: frozenset[str] = frozenset()
        self._proof_keys: dict[str, tuple[str, ...]] = {}
        self._unique_sets: dict[str, tuple[str, ...]] = {}
        self.stable_model_count = 0

    def on_model(self, model: clingo.Model) -> None:
        if self.stable_model_count:
            raise EnginePolicyError(
                "analysis requires exactly one stable model, the rule program produced more"
            )
        self.stable_model_count = 1

        self._proved = _proved(model)
        self._proof_keys = _proof_keys(model)
        self._unique_sets = _unique_sets(model)

    def report(
        self,
        assertions: Sequence[ir.UniqueJoinAssertion],
        diagnostics: Sequence[Diagnostic] = (),
    ) -> Report:
        if not self.stable_model_count:
            raise EnginePolicyError(
                "analysis requires exactly one stable model, the rule program produced none"
            )

        return Report(
            tuple(self._assertion_report(assertion) for assertion in assertions),
            tuple(diagnostics),
            self.stable_model_count,
        )

    def _assertion_report(self, assertion: ir.UniqueJoinAssertion) -> AssertionReport:
        proved = assertion.id in self._proved
        keys = self._proof_keys.get(assertion.id, ())
        return AssertionReport(
            assertion_id=assertion.id,
            outcome=Outcome.PROVED if proved else Outcome.UNKNOWN,
            origin=self._origins.resolve(assertion.origin_id),
            proving_unique_set=self._unique_sets.get(keys[0], ()) if proved and keys else (),
        )


def _proved(model: clingo.Model) -> frozenset[str]:
    """Every assertion the engine proved."""
    return frozenset(
        str(symbol.arguments[0])
        for symbol in model.symbols(atoms=True)
        if symbol.name == _PROVED and len(symbol.arguments) == 1
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
