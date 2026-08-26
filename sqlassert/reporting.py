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
from dataclasses import dataclass, field
from enum import Enum

import clingo

from sqlassert import ir
from sqlassert.diagnostics import Diagnostic
from sqlassert.engine import EnginePolicyError
from sqlassert.provenance import Origin, OriginRegistry

_PROVED = "proved"
_PROOF_KEY = "proof_key"
_PROVED_BY_CANDIDATE_KEY = "proved_by_candidate_key"
_UNIQUE_SET = "unique_set"
_UNIQUE_SET_MEMBER = "unique_set_member"
_ASSERTION_MISSING_MEMBER = "assertion_missing_member"


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
    is_candidate_key: bool = False
    missing_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationFacts:
    """What was proved about every named relation -- a table or a view -- in
    one analysis, queryable by name.

    An unnamed relation (a CTE, a derived subquery, an Aggregate or Distinct
    result) has no caller-visible name to ask about and is never included:
    ask about the assertion it feeds into instead.

    Holds only Unique Sets for now; further properties join it as typed
    methods, the same way, rather than as a generic ask-anything query.
    """

    _unique_sets: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)

    def unique_sets(self, relation: str) -> tuple[tuple[str, ...], ...]:
        """Every Unique Set proved for `relation`, each as its ordered columns."""
        return self._unique_sets.get(relation.lower(), ())

    def is_unique(self, relation: str, columns: Sequence[str]) -> bool:
        """Whether `columns` is guaranteed unique on `relation`: some proved
        Unique Set's members are all among `columns`."""
        candidate = {column.lower() for column in columns}
        return any(set(unique_set) <= candidate for unique_set in self.unique_sets(relation))


@dataclass(frozen=True)
class Report:
    assertions: tuple[AssertionReport, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    facts: RelationFacts = field(default_factory=RelationFacts)
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
        self._candidate_key_proofs: frozenset[str] = frozenset()
        self._unique_sets: dict[str, tuple[str, ...]] = {}
        self._unique_set_relations: dict[str, tuple[str, ...]] = {}
        self._missing_members: dict[str, dict[str, frozenset[str]]] = {}
        self.stable_model_count = 0

    def on_model(self, model: clingo.Model) -> None:
        if self.stable_model_count:
            raise EnginePolicyError(
                "analysis requires exactly one stable model, the rule program produced more"
            )
        self.stable_model_count = 1

        self._proved = _proved(model)
        self._proof_keys = _proof_keys(model)
        self._candidate_key_proofs = _candidate_key_proofs(model)
        self._unique_sets = _unique_sets(model)
        self._unique_set_relations = _unique_set_relations(model)
        self._missing_members = _missing_members(model)

    def report(
        self,
        assertions: Sequence[ir.UniqueJoinAssertion | ir.UniqueSetAssertion],
        diagnostics: Sequence[Diagnostic] = (),
        definitions: Sequence[ir.RelationDefinition] = (),
    ) -> Report:
        if not self.stable_model_count:
            raise EnginePolicyError(
                "analysis requires exactly one stable model, the rule program produced none"
            )

        return Report(
            tuple(self._assertion_report(assertion) for assertion in assertions),
            tuple(diagnostics),
            self._relation_facts(definitions),
            self.stable_model_count,
        )

    def _relation_facts(self, definitions: Sequence[ir.RelationDefinition]) -> RelationFacts:
        """Every proved Unique Set, grouped by the name of the Relation
        Definition it belongs to -- a table or view's own `name`, or a CTE's
        `report_name` -- skipping the truly anonymous ones (a bare Project,
        Aggregate, or Distinct with no declared name of its own to group
        under)."""
        names_by_id = {
            definition.id: definition.name or definition.report_name
            for definition in definitions
            if definition.name or definition.report_name
        }

        by_relation: dict[str, list[tuple[str, ...]]] = {}
        for key, relation_ids in self._unique_set_relations.items():
            columns = self._unique_sets.get(key)
            if columns is None:
                continue
            for relation_id in relation_ids:
                name = names_by_id.get(relation_id)
                if name is None:
                    continue
                by_relation.setdefault(name.lower(), []).append(columns)

        return RelationFacts({name: tuple(unique_sets) for name, unique_sets in by_relation.items()})

    def _assertion_report(self, assertion: ir.UniqueJoinAssertion | ir.UniqueSetAssertion) -> AssertionReport:
        proved = assertion.id in self._proved
        keys = self._proof_keys.get(assertion.id, ())
        return AssertionReport(
            assertion_id=assertion.id,
            outcome=Outcome.PROVED if proved else Outcome.UNKNOWN,
            origin=self._origins.resolve(assertion.origin_id),
            proving_unique_set=self._unique_sets.get(keys[0], ()) if proved and keys else (),
            is_candidate_key=proved and assertion.id in self._candidate_key_proofs,
            missing_columns=() if proved else self._missing_columns_for(assertion.id),
        )

    def _missing_columns_for(self, assertion_id: str) -> tuple[str, ...]:
        """The closest Unique Set(s)' missing columns, in their declared order."""
        per_key = self._missing_members.get(assertion_id, {})
        seen: set[str] = set()
        ordered: list[str] = []
        for key in sorted(per_key):
            for column in self._unique_sets.get(key, ()):
                if column in per_key[key] and column not in seen:
                    seen.add(column)
                    ordered.append(column)
        return tuple(ordered)


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


def _candidate_key_proofs(model: clingo.Model) -> frozenset[str]:
    """Every assertion proved by a Unique Set that is also a Candidate Key."""
    return frozenset(
        str(symbol.arguments[0])
        for symbol in model.symbols(atoms=True)
        if symbol.name == _PROVED_BY_CANDIDATE_KEY and len(symbol.arguments) == 1
    )


def _unique_set_relations(model: clingo.Model) -> dict[str, tuple[str, ...]]:
    """Every Relation Definition each Unique Set belongs to, read from the model.

    A key can belong to more than one relation: Filter's propagation
    (`rules/propagation.lp`) reuses its input's own Unique Set key rather
    than minting a new one, so the same key legitimately describes both a
    Filter and whatever it filters.
    """
    relations: dict[str, list[str]] = {}
    for symbol in model.symbols(atoms=True):
        if symbol.name == _UNIQUE_SET and len(symbol.arguments) == 2:
            key, relation = (str(argument) for argument in symbol.arguments)
            relations.setdefault(key, []).append(relation)
    return {key: tuple(found) for key, found in relations.items()}


def _unique_sets(model: clingo.Model) -> dict[str, tuple[str, ...]]:
    """The columns of every Unique Set, in declared order, read from the model."""
    members: dict[str, list[tuple[int, str]]] = {}
    for symbol in model.symbols(atoms=True):
        if symbol.name == _UNIQUE_SET_MEMBER and len(symbol.arguments) == 3:
            key, position, column = symbol.arguments
            members.setdefault(str(key), []).append((position.number, column.string))
    return {key: tuple(column for _, column in sorted(found)) for key, found in members.items()}


def _missing_members(model: clingo.Model) -> dict[str, dict[str, frozenset[str]]]:
    """Best-effort UNKNOWN evidence: per assertion, per closest Unique Set, its missing columns."""
    members: dict[str, dict[str, set[str]]] = {}
    for symbol in model.symbols(atoms=True):
        if symbol.name == _ASSERTION_MISSING_MEMBER and len(symbol.arguments) == 3:
            assertion, key, column = (str(symbol.arguments[0]), str(symbol.arguments[1]), symbol.arguments[2].string)
            members.setdefault(assertion, {}).setdefault(key, set()).add(column)
    return {assertion: {key: frozenset(columns) for key, columns in found.items()} for assertion, found in members.items()}
