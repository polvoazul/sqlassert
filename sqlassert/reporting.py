"""The durable Report an analysis returns: assertion outcomes, diagnostics, and relation facts.

The Report holds plain values only, plus each assertion's evidence. It never prints.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from sqlassert import properties as props
from sqlassert.diagnostics import Diagnostic
from sqlassert.provenance import Origin

if TYPE_CHECKING:
    from sqlassert.python_engine import Result


class Outcome(Enum):
    """PROVED or UNKNOWN. UNKNOWN fails an assertion but is not a Disproof."""

    PROVED = "proved"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AssertionReport:
    """One assertion's outcome. `kind` is the marker that states it
    (`ASSERTED_KINDS`), so a failure names what was not proved."""

    assertion_id: str
    outcome: Outcome
    origin: Origin
    kind: str
    evidence: Result[bool] | None = field(default=None, compare=False)


# The marker each assertable Property is written as, in the words of its `/**ASSERT ...**/`.
ASSERTED_KINDS: dict[type[props.Property], str] = {
    props.NonNullColumns: "not-null",
    props.NonNullElements: "not-null-elements",
    props.UniqueSet: "unique",
    props.NonNullUniqueSet: "not-null-unique",  # also written PRIMARY KEY
    props.FunctionalDependency: "determines",
    props.EnumeratedDomain: "enum",
    props.ReferentialCoverage: "foreign-key",
    props.ForeignKey: "foreign-key",
    props.ExhaustiveCase: "exhaustive",
    props.UniqueJoin: "unique-join",
    props.ManyToOneJoin: "many-to-one-join",
    props.OneToManyJoin: "one-to-many-join",
    props.OneToOneJoin: "one-to-one-join",
    props.ExactlyOneJoin: "exactly-one-join",
}


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
        """The minimal Unique Sets proved for `relation`, smallest first.

        Every minimal set of up to three columns; when none is that small,
        one minimal set. A superset of any of them is unique too."""
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
    warnings: tuple[Diagnostic, ...] = ()

    @property
    def proved(self) -> bool:
        """Every assertion proved, and nothing about the program was unsupported.

        Warnings do not count: they explain UNKNOWN outcomes, never cause them."""
        return not self.diagnostics and all(
            assertion.outcome is Outcome.PROVED for assertion in self.assertions
        )

