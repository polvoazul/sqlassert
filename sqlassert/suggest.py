"""Suggest the Property Markers sqlassert can prove for a SQL Program.

Three levels of suggestion. High: Improvements, rewrites of the SQL itself
that a proof shows leave its result unchanged (`improvements.py`). Mid:
markers that lock in what is provable but not asserted. Low: Smells, places
whose result the SQL leaves unspecified (`smells.py`).

Marker candidates are generated mechanically from the Program IR, one
Attachment Site at a time: every markable JOIN may be a Unique Join (or
Exactly One), every Select Expression's output columns may form Unique Sets,
be non-null, or have a small Enumerated Domain, and every markable simple
CASE may be exhaustive. `suggest` proves them and keeps only what is PROVED,
in its strongest, minimal form. `candidates` is the generator alone, for
callers that judge candidates by other means.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, fields
from decimal import Decimal
from itertools import combinations
from typing import ClassVar

from sqlglot import exp

from sqlassert import diagnostics as diag
from sqlassert import ir
from sqlassert.diagnostics import Diagnostic
from sqlassert.improvements import Improvement, Level, Rewrite, improvements
from sqlassert.ir.convert import IrConversionResult, IrParser
from sqlassert.smells import Concern, Smell, smells
from sqlassert.main import DEFAULT_DIALECT
from sqlassert.properties import (
    EnumeratedDomain, ExactlyOneJoin, ExhaustiveCase, ManyToOneJoin, NonNullColumn, NonNullColumns, NonNullUniqueSet,
    OneToManyJoin, OneToOneJoin, Property, UniqueJoin, UniqueSet,
)
from sqlassert.python_engine import Arena, Engine, Result
from sqlassert.provenance import AttachmentSite
from sqlassert.rules.domain import _domain
from sqlassert.sql_parse import ParsedProgram, SqlParser

DEFAULT_MAX_SIZE = 3

# The most values an ENUM marker lists. A short closed list is a category
# worth locking in; a longer one is mostly a long IN list restated, and its
# marker would be noise at every site the column passes through.
MAX_ENUM_VALUES = 10

__all__ = [
    "Candidate", "Concern", "Improvement", "Level", "Rewrite", "Smell", "Suggestion", "SuggestionReport", "annotate",
    "candidates", "suggest", "suggest_parsed",
]


@dataclass(frozen=True)
class Candidate:
    """A Property worth trying at one Attachment Site, with its marker text."""

    site: AttachmentSite
    property: Property
    marker: str


@dataclass(frozen=True)
class Suggestion:
    """A proved Candidate: its marker can be pasted and will be PROVED."""

    site: AttachmentSite
    property: Property
    marker: str
    evidence: Result[bool]
    level: ClassVar[Level] = Level.MID

    @property
    def kind(self) -> str:
        """The Property's name, as a listing shows it."""
        return _KINDS[type(self.property)]


@dataclass(frozen=True)
class SuggestionReport:
    """`improvements` (High) rewrite the SQL; `suggestions` (Mid) are markers
    to paste; `smells` (Low) point at results the SQL leaves unspecified."""

    sql: str
    suggestions: tuple[Suggestion, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    improvements: tuple[Improvement, ...] = ()
    warnings: tuple[Diagnostic, ...] = ()
    smells: tuple[Smell, ...] = ()

    def annotated(self, chosen: Iterable[Suggestion] | None = None) -> str:
        """The SQL with the chosen suggestions' markers pasted in (all by default)."""
        return annotate(self.sql, self.suggestions if chosen is None else chosen)


def annotate(sql: str, items: Iterable[Candidate | Suggestion]) -> str:
    """`sql` with each item's marker pasted at its Attachment Site.

    Markers never add a line break, so every line number in `sql` still holds.
    """
    inserts: dict[int, list[Candidate | Suggestion]] = {}
    for item in items:
        inserts.setdefault(item.site.offset, []).append(item)
    for offset in sorted(inserts, reverse=True):
        text = "".join(f" {item.marker}" if item.site.trailing else f"{item.marker} " for item in inserts[offset])
        sql = sql[:offset] + text + sql[offset:]
    return sql


def suggest(
    sql: str, *, dialect: str = DEFAULT_DIALECT, max_size: int = DEFAULT_MAX_SIZE, trust_unenforced_keys: bool = True,
) -> SuggestionReport:
    """Every Property sqlassert can prove about `sql`, as markers to paste,
    and every Improvement a proof justifies.

    Per Select Expression: each minimal Unique Set up to `max_size` columns,
    as a Non-Null Unique Set when that also holds, plus one minimal set found
    among wider ones when none that small exists; and the non-null columns no
    suggested Non-Null Unique Set already implies; and an Enumerated Domain of
    up to `MAX_ENUM_VALUES` values for each column not written as a literal.
    Per JOIN: Exactly One Join, else Unique Join, unless the join already
    carries a marker. Per simple CASE a marker can precede: Exhaustive Case.
    Properties the SQL already asserts or declares are left out.
    """
    ast = SqlParser(dialect).parse(sql)
    return suggest_parsed(sql, ast, IrParser(dialect, trust_unenforced_keys).parse(ast), dialect=dialect, max_size=max_size)


def suggest_parsed(
    sql: str, ast: ParsedProgram, conversion: IrConversionResult, *, dialect: str = DEFAULT_DIALECT,
    max_size: int = DEFAULT_MAX_SIZE,
) -> SuggestionReport:
    """`suggest` of a Program already parsed and lowered: for a caller that
    adds Property Declarations to `conversion.program` before proving."""
    program, diagnostics = conversion.program, ast.diagnostics + conversion.diagnostics
    if any(diagnostic.code in _BLOCKING for diagnostic in diagnostics):
        return SuggestionReport(sql, (), diagnostics, warnings=conversion.warnings)
    present = (*program.declarations, *(assertion.property for assertion in program.assertions))
    marked_non_null = {
        column
        for item in present
        for column in (
            (item.column,) if isinstance(item, NonNullColumn) else item.columns if isinstance(item, NonNullColumns) else ()
        )
    }
    marked_joins = {item.join for item in present if isinstance(item, _JOIN_PROPERTIES)}
    marked_cases = {item.case for item in present if isinstance(item, ExhaustiveCase)}
    reachable = set(program.nodes())
    suggestions: list[Suggestion] = []
    with Engine().session(program, conversion.knowledge) as arena:
        found = improvements(ast, program, arena, dialect)
        concerns = smells(ast, program, arena)
        for site, relation in program.attachment_sites:
            match relation:
                case ir.Join() if not site.trailing and relation not in marked_joins and relation in reachable:
                    # The strongest proved form. Not many to many is implied by each
                    # cardinality, so it is never suggested on its own; a semi or
                    # anti join outputs no right rows, so no cardinality applies.
                    kinds = (ExactlyOneJoin,) if relation.kind in {"semi", "anti"} else _JOIN_PROPERTIES[:4]
                    suggestions.extend(next(
                        (proved for kind in kinds if (proved := _proved(arena, site, kind(join=relation), (), dialect))), []
                    ))
                case ir.Join() if not site.trailing:
                    # Already marked, or unreachable: lowered inside a Select that is
                    # opaque as a whole (one with HAVING, say), so nothing is known about it.
                    pass
                case ir.SimpleCase() if relation not in marked_cases and relation in reachable and _markable(relation):
                    suggestions.extend(_proved(arena, site, ExhaustiveCase(case=relation), (), dialect))
                case ir.SimpleCase():
                    pass  # Already marked, unreachable, or with a WHEN no marker allows.
                case _ if relation in reachable:
                    suggestions.extend(_select_suggestions(arena, site, relation, max_size, marked_non_null, dialect))
    marked = {_identity(item) for item in present}
    kept = tuple(
        sorted(
            (suggestion for suggestion in suggestions if _identity(suggestion.property) not in marked),
            key=lambda suggestion: suggestion.site.offset,
        )
    )
    return SuggestionReport(sql, kept, diagnostics, tuple(found), conversion.warnings, tuple(concerns))


# How each suggested Property reads in a listing.
_KINDS: dict[type[Property], str] = {
    ExactlyOneJoin: "Exactly One Join",
    OneToOneJoin: "One To One Join",
    ManyToOneJoin: "Many To One Join",
    OneToManyJoin: "One To Many Join",
    UniqueJoin: "Unique Join",
    UniqueSet: "Unique Set",
    NonNullUniqueSet: "Non-Null Unique Set",
    NonNullColumns: "Not Null",
    EnumeratedDomain: "Enumerated Domain",
    ExhaustiveCase: "Exhaustive Case",
}

# The join Properties, strongest first.
_JOIN_PROPERTIES = (ExactlyOneJoin, OneToOneJoin, ManyToOneJoin, OneToManyJoin, UniqueJoin)

# A parse failure leaves nothing to suggest on; duplicate declarations make
# `analyze` refuse every proof, so nothing suggested could round-trip.
_BLOCKING = {diag.SQL_PARSE_FAILED, diag.UNATTACHED_MARKER, diag.DUPLICATE_DECLARATION, diag.UNSUPPORTED_NATURAL_JOIN}


def candidates(
    program: ir.Program, *, dialect: str = DEFAULT_DIALECT, max_size: int = DEFAULT_MAX_SIZE
) -> Iterator[Candidate]:
    """Every candidate Property at every Attachment Site of `program`, proved or not.

    Per JOIN but a semi or anti one: Unique Join. Per markable simple CASE: Exhaustive Case. Per
    Select Expression: each output column non-null, and each set of up to
    `max_size` output columns, plus the set of all of them, as a Unique Set
    and as a Non-Null Unique Set. Only columns a marker can name
    unambiguously take part. Enumerated Domains are not candidates: which
    values to list is itself something only analysis finds.
    """
    for site, relation in program.attachment_sites:
        match relation:
            case ir.Join() if not site.trailing and relation.kind not in {"semi", "anti"}:
                yield _candidate(site, UniqueJoin(join=relation), (), dialect)
            case ir.Join() if not site.trailing:
                pass  # No cardinality applies to a semi or anti join.
            case ir.SimpleCase() if _markable(relation):
                yield _candidate(site, ExhaustiveCase(case=relation), (), dialect)
            case ir.SimpleCase():
                pass
            case _:
                columns = _nameable_columns(relation)
                for column in columns:
                    yield _candidate(site, NonNullColumns(columns=frozenset((column,))), columns, dialect)
                for members in _column_sets(columns, max_size):
                    yield _candidate(site, UniqueSet(columns=frozenset(members)), columns, dialect)
                    yield _candidate(site, NonNullUniqueSet(columns=frozenset(members)), columns, dialect)


def _select_suggestions(
    arena: Arena,
    site: AttachmentSite,
    relation: ir.RelationExpr,
    max_size: int,
    marked_non_null: set[ir.OutputColumn],
    dialect: str,
) -> list[Suggestion]:
    columns = _nameable_columns(relation)
    suggestions = []
    # Proof is monotone in the column set: when the whole set is not unique,
    # no subset is, and enumerating them would be wasted work.
    minimal: list[tuple[ir.OutputColumn, ...]] = []
    if columns and arena.prove(UniqueSet(columns=frozenset(columns))).value:
        for members in _column_sets(columns, max_size):
            if any(set(found) <= set(members) for found in minimal):
                continue
            if arena.prove(UniqueSet(columns=frozenset(members))).value:
                minimal.append(_shrink(arena, members))
    for members in minimal:
        strong = _proved(arena, site, NonNullUniqueSet(columns=frozenset(members)), columns, dialect)
        suggestions.extend(strong or _proved(arena, site, UniqueSet(columns=frozenset(members)), columns, dialect))

    implied = {column for s in suggestions if isinstance(s.property, NonNullUniqueSet) for column in s.property.columns}
    non_null = frozenset(
        column for column in columns
        if column not in implied | marked_non_null
        and arena.prove(NonNullColumns(columns=frozenset((column,)))).value
    )
    if non_null:
        suggestions.extend(_proved(arena, site, NonNullColumns(columns=non_null), columns, dialect))

    for column in columns:
        if isinstance(column.scalar_expr, ir.Constant):
            continue  # The marker would restate the literal.
        # Which values to list comes from the domain analysis; the marker is
        # then proved like any other.
        domain = _domain(column).value
        if domain and len(domain) <= MAX_ENUM_VALUES:
            suggestions.extend(_proved(arena, site, EnumeratedDomain(column=column, values=domain), columns, dialect))
    return suggestions


def _markable(case: ir.SimpleCase) -> bool:
    """A case marker requires every WHEN to be a distinct non-null literal."""
    values = [when.value for when in case.whens if isinstance(when, ir.Constant) and when.value is not None]
    return len(values) == len(case.whens) and len(set(values)) == len(values)


def _proved(
    arena: Arena, site: AttachmentSite, property: Property, order: tuple[ir.OutputColumn, ...], dialect: str
) -> list[Suggestion]:
    """`property` as a one-item list when it is proved, else empty."""
    evidence = arena.prove(property)
    if not evidence.value:
        return []
    candidate = _candidate(site, property, order, dialect)
    return [Suggestion(site, property, candidate.marker, evidence)]


def _shrink(arena: Arena, members: tuple[ir.OutputColumn, ...]) -> tuple[ir.OutputColumn, ...]:
    """Drop columns while the rest stays unique. Every set up to `max_size` was
    tried smallest first, so only a set wider than that can shrink."""
    for column in members:
        rest = tuple(other for other in members if other is not column)
        if rest and arena.prove(UniqueSet(columns=frozenset(rest))).value:
            return _shrink(arena, rest)
    return members


def _column_sets(columns: tuple[ir.OutputColumn, ...], max_size: int) -> Iterator[tuple[ir.OutputColumn, ...]]:
    """Column sets smallest first, ending with all columns when wider than `max_size`."""
    for size in range(1, min(max_size, len(columns)) + 1):
        yield from combinations(columns, size)
    if len(columns) > max_size:
        yield columns


def _nameable_columns(relation: ir.RelationExpr) -> tuple[ir.OutputColumn, ...]:
    """Output columns a marker names unambiguously: named, and named once."""
    names = [column.name.lower() for column in relation.output_columns]
    return tuple(column for column in relation.output_columns if column.name and names.count(column.name.lower()) == 1)


def _identity(item: Property) -> tuple:
    """Properties compare by identity; this compares them by kind and fields."""
    return (type(item), tuple(getattr(item, field.name) for field in fields(item)))


def _value_order(value: ir.LiteralValue) -> tuple:
    """Booleans, then numbers by value, then strings."""
    match value.type:
        case "numeric":
            return (1, Decimal(value.text), value.text)
        case "string":
            return (2, 0, value.text)
        case _:
            return (0, 0, value.text)


def _candidate(site: AttachmentSite, property: Property, order: tuple[ir.OutputColumn, ...], dialect: str) -> Candidate:
    """`property` at `site`; its marker lists columns in `order`, the output order."""
    match property:
        case ExactlyOneJoin():
            return Candidate(site, property, "/**ASSERT EXACTLY ONE**/")
        case OneToOneJoin():
            return Candidate(site, property, "/**ASSERT ONE TO ONE**/")
        case ManyToOneJoin():
            return Candidate(site, property, "/**ASSERT MANY TO ONE**/")
        case OneToManyJoin():
            return Candidate(site, property, "/**ASSERT ONE TO MANY**/")
        case UniqueJoin():
            return Candidate(site, property, "/**ASSERT UNIQUE**/")
        case ExhaustiveCase():
            return Candidate(site, property, "/**ASSERT EXHAUSTIVE**/")
        case EnumeratedDomain(column=column, values=values):
            name = exp.to_identifier(column.name).sql(dialect=dialect)
            listed = ", ".join(value.sql() for value in sorted(values, key=_value_order))
            return Candidate(site, property, f"/**ASSERT ENUM({name} IN ({listed}))**/")
        case NonNullUniqueSet(columns=columns):
            kind = "NOT NULL UNIQUE"
        case UniqueSet(columns=columns):
            kind = "UNIQUE"
        case NonNullColumns(columns=columns):
            kind = "NOT NULL"
        case _:
            raise TypeError(f"No marker asserts {type(property).__name__}")
    names = ", ".join(exp.to_identifier(column.name).sql(dialect=dialect) for column in order if column in columns)
    return Candidate(site, property, f"/**ASSERT {kind}({names})**/")
