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
from sqlassert.diagnostics import DUPLICATE_DECLARATION, Diagnostic
from sqlassert.engine import EnginePolicyError
from sqlassert.facts import ClingoEncoding
from sqlassert.properties import CandidateKey, UniqueJoin, UniqueSet
from sqlassert.provenance import Origin

_COVERS_UNIQUE_SET = "pub__covers_unique_set"
_CANDIDATE_KEY = "pub__candidate_key"
_UNIQUE_SET = "pub__unique_set"
_UNIQUE_SET_MEMBER = "pub__unique_set__columns"
_UNIQUE_JOIN = "pub__unique_join"
_UNIQUE_JOIN_SUBJECT = "pub__unique_join__join"
_MISSING_UNIQUE_SET_MEMBER = "pub__missing_unique_set_member"


class Outcome(Enum):
    """PROVED or UNKNOWN. UNKNOWN fails an assertion but is not a Disproof."""

    PROVED = "proved"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AssertionReport:
    assertion_id: str
    outcome: Outcome
    origin: Origin
    explanation: str = ""
    proving_unique_set: tuple[str, ...] = ()
    is_candidate_key: bool = False
    missing_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelationFacts:
    """What was proved about every Named Relation in one analysis.

    Tables and views use their declared name; CTEs use a ``CTE_``-prefixed
    report label. Anonymous Relation Expressions are omitted: ask about the
    assertion they feed into instead.

    Holds only Unique Sets for now; further properties join it as typed
    methods, the same way, rather than as a generic ask-anything query.
    """

    _unique_sets: dict[str, tuple[tuple[str, ...], ...]] = field(default_factory=dict)

    def unique_sets(self, relation: str) -> tuple[tuple[str, ...], ...]:
        """Every Unique Set proved for `relation`, canonically ordered for display."""
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

    def __init__(self, encoding: ClingoEncoding) -> None:
        self._encoding = encoding
        self._proof_keys: dict[str, tuple[str, ...]] = {}
        self._candidate_keys: frozenset[str] = frozenset()
        self._unique_joins: frozenset[str] = frozenset()
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

        self._proof_keys = _proof_keys(model)
        self._candidate_keys = _property_ids(model, _CANDIDATE_KEY)
        self._unique_joins = _unique_joins(model)
        self._unique_sets = _unique_sets(model, self._encoding)
        self._unique_set_relations = _unique_set_relations(self._unique_sets, self._encoding)
        self._missing_members = _missing_members(model)

    def report(
        self,
        assertions: Sequence[ir.Assertion],
        diagnostics: Sequence[Diagnostic] = (),
        named_relations: Sequence[ir.NamedRelation] = (),
    ) -> Report:
        if not self.stable_model_count:
            raise EnginePolicyError(
                "analysis requires exactly one stable model, the rule program produced none"
            )

        accept_proof = all(diagnostic.code != DUPLICATE_DECLARATION for diagnostic in diagnostics)
        return Report(
            tuple(self._assertion_report(assertion, accept_proof=accept_proof) for assertion in assertions),
            tuple(diagnostics),
            self._relation_facts(named_relations),
            self.stable_model_count,
        )

    def _relation_facts(self, named_relations: Sequence[ir.NamedRelation]) -> RelationFacts:
        """Every proved Unique Set grouped by its named relation."""
        names_by_id = {
            self._encoding.node_to_symbol[relation]: relation.report_name
            for relation in named_relations
            if relation in self._encoding.node_to_symbol
        }

        by_relation: dict[str, list[tuple[str, ...]]] = {}
        for key, relation_ids in self._unique_set_relations.items():
            members = self._unique_sets.get(key)
            if members is None:
                continue
            columns = self._column_names(members)
            for relation_id in relation_ids:
                name = names_by_id.get(relation_id)
                if name is None:
                    continue
                by_relation.setdefault(name.lower(), []).append(columns)

        return RelationFacts({name: tuple(unique_sets) for name, unique_sets in by_relation.items()})

    def _assertion_report(self, assertion: ir.Assertion, *, accept_proof: bool) -> AssertionReport:
        assertion_id = self._encoding.node_to_symbol[assertion]
        proved = accept_proof and self._property_holds(assertion)
        context = self._column_set_context(assertion)
        keys = self._proof_keys.get(context, ())
        proof_key = keys[0] if keys else context
        proving_unique_set = self._column_names(self._unique_sets.get(proof_key, ())) if proved else ()
        missing_columns = () if proved else self._missing_columns_for(context)
        return AssertionReport(
            assertion_id=assertion_id,
            outcome=Outcome.PROVED if proved else Outcome.UNKNOWN,
            origin=assertion.origin,
            explanation=self._explanation(assertion, proved, proving_unique_set, missing_columns),
            proving_unique_set=proving_unique_set,
            is_candidate_key=proved and (
                context in self._candidate_keys or any(key in self._candidate_keys for key in keys)
            ),
            missing_columns=missing_columns,
        )

    def _property_holds(self, assertion: ir.Assertion) -> bool:
        """Look up the requested public property; implication belongs in the rules."""
        property = assertion.property
        if isinstance(property, UniqueJoin):
            return self._encoding.node_to_symbol[property.join] in self._unique_joins
        requested_set = self._column_set_context(assertion)
        if isinstance(property, CandidateKey):
            return requested_set in self._candidate_keys
        if isinstance(property, UniqueSet):
            return requested_set in self._unique_sets
        return False

    def _column_set_context(self, assertion: ir.Assertion) -> str:
        if isinstance(assertion.property, UniqueJoin):
            return f"join_right_columns({self._encoding.node_to_symbol[assertion.property.join]})"
        return f"asserted_columns({self._encoding.node_to_symbol[assertion]})"

    def _explanation(self, assertion: ir.Assertion, proved: bool, proving_unique_set: tuple[str, ...], missing_columns: tuple[str, ...]) -> str:
        if isinstance(assertion.property, UniqueJoin):
            return self._unique_join_explanation(assertion, proved, proving_unique_set, missing_columns)
        if isinstance(assertion.property, UniqueSet):
            return self._unique_set_explanation(proved, proving_unique_set, missing_columns)
        return "Unknown: this assertion type has no explanation."

    def _unique_join_explanation(self, assertion: ir.Assertion, proved: bool, proving_unique_set: tuple[str, ...], missing_columns: tuple[str, ...]) -> str:
        if not isinstance(assertion.property, UniqueJoin):
            return "Unknown: this assertion does not contain a Unique Join property."
        join = assertion.property.join
        if proved:
            if not proving_unique_set:
                return "Proved: the join is known not to multiply rows from its left input."
            return f"Proved: the join covers the right side's unique set {_format_columns(proving_unique_set)}."
        if join.kind not in {ir.INNER, "left"}:
            return f"Unknown: {join.kind.upper()} joins are not supported for uniqueness proofs."
        right_symbol = self._encoding.node_to_symbol[join.right]
        right_keys = [key for key, relations in self._unique_set_relations.items() if right_symbol in relations]
        if not right_keys:
            return "Unknown: no unique set is known for the right side of this join."
        if missing_columns:
            closest = self._closest_key_columns(self._column_set_context(assertion))
            return f"Unknown: the join does not cover the closest known unique set {_format_columns(closest)}; missing {', '.join(missing_columns)}."
        return "Unknown: the join predicate does not establish coverage of a known unique set."

    def _unique_set_explanation(self, proved: bool, proving_unique_set: tuple[str, ...], missing_columns: tuple[str, ...]) -> str:
        if proved:
            return f"Proved: the relation has the unique set {_format_columns(proving_unique_set)}."
        if missing_columns:
            return f"Unknown: the closest known unique set requires {', '.join(missing_columns)}."
        return "Unknown: no known unique set covers the asserted columns."

    def _column_names(self, symbols: Sequence[str]) -> tuple[str, ...]:
        return tuple(
            node.name
            for symbol in symbols
            if isinstance((node := self._encoding.symbol_to_node.get(symbol)), ir.OutputColumn)
        )

    def _missing_columns_for(self, context: str) -> tuple[str, ...]:
        """The closest Unique Set(s)' missing columns, in canonical display order."""
        per_key = self._missing_members.get(context, {})
        seen: set[str] = set()
        ordered: list[str] = []
        for key in sorted(per_key):
            for symbol in self._unique_sets.get(key, ()):
                if symbol in per_key[key] and symbol not in seen:
                    seen.add(symbol)
                    node = self._encoding.symbol_to_node.get(symbol)
                    if isinstance(node, ir.OutputColumn):
                        ordered.append(node.name)
        return tuple(ordered)

    def _closest_key_columns(self, context: str) -> tuple[str, ...]:
        keys = self._missing_members.get(context, {})
        return self._column_names(self._unique_sets.get(sorted(keys)[0], ())) if keys else ()


def _format_columns(columns: tuple[str, ...]) -> str:
    return f"({', '.join(columns)})"


def _property_ids(model: clingo.Model, predicate: str) -> frozenset[str]:
    """Identities carrying a public property in the solved model."""
    return frozenset(
        str(symbol.arguments[0])
        for symbol in model.symbols(atoms=True)
        if symbol.name == predicate and len(symbol.arguments) == 1
    )


def _proof_keys(model: clingo.Model) -> dict[str, tuple[str, ...]]:
    """Known Unique Sets covered by each column set under analysis."""
    keys: dict[str, list[str]] = {}
    for symbol in model.symbols(atoms=True):
        if symbol.name == _COVERS_UNIQUE_SET and len(symbol.arguments) == 2:
            context, key = (str(argument) for argument in symbol.arguments)
            keys.setdefault(context, []).append(key)
    return {context: tuple(sorted(found)) for context, found in keys.items()}


def _unique_joins(model: clingo.Model) -> frozenset[str]:
    """Join subjects of established or accepted public UniqueJoin properties."""
    properties = _property_ids(model, _UNIQUE_JOIN)
    return frozenset(
        str(symbol.arguments[1])
        for symbol in model.symbols(atoms=True)
        if symbol.name == _UNIQUE_JOIN_SUBJECT and len(symbol.arguments) == 2
        and str(symbol.arguments[0]) in properties
    )


def _unique_set_relations(unique_sets: dict[str, tuple[str, ...]], encoding: ClingoEncoding) -> dict[str, tuple[str, ...]]:
    """Infer each Unique Set's Relation Expressions from its Output Columns."""
    relation_columns = {
        symbol: {encoding.node_to_symbol[column] for column in node.output_columns}
        for symbol, node in encoding.symbol_to_node.items()
        if isinstance(node, ir.RelationExpr)
    }
    return {
        key: tuple(relation for relation, columns in relation_columns.items() if set(members) <= columns)
        for key, members in unique_sets.items()
    }


def _unique_sets(model: clingo.Model, encoding: ClingoEncoding) -> dict[str, tuple[str, ...]]:
    """Every Unique Set, canonically ordered by its Output Columns for display."""
    keys = _property_ids(model, _UNIQUE_SET)
    members: dict[str, set[str]] = {key: set() for key in keys}
    for symbol in model.symbols(atoms=True):
        if symbol.name == _UNIQUE_SET_MEMBER and len(symbol.arguments) == 2:
            key, column = symbol.arguments
            if str(key) in keys:
                members[str(key)].add(str(column))
    column_order = {
        symbol: position
        for position, (node, symbol) in enumerate(encoding.node_to_symbol.items())
        if isinstance(node, ir.OutputColumn)
    }
    return {key: tuple(sorted(found, key=column_order.__getitem__)) for key, found in members.items()}


def _missing_members(model: clingo.Model) -> dict[str, dict[str, frozenset[str]]]:
    """Missing key members per column set, for explaining UNKNOWN outcomes."""
    members: dict[str, dict[str, set[str]]] = {}
    for symbol in model.symbols(atoms=True):
        if symbol.name == _MISSING_UNIQUE_SET_MEMBER and len(symbol.arguments) == 3:
            context, key, column = (str(argument) for argument in symbol.arguments)
            members.setdefault(context, {}).setdefault(key, set()).add(column)
    return {context: {key: frozenset(columns) for key, columns in found.items()} for context, found in members.items()}
