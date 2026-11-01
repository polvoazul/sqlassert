"""Suggest the Property Markers sqlassert can prove for a SQL Program.

Candidates are generated mechanically from the Program IR, one Attachment
Site at a time: every markable JOIN may be a Unique Join, and every Select
Expression's output columns may form Unique Sets or be non-null. `suggest`
proves them and keeps only what is PROVED, in its strongest, minimal form.
`candidates` is the generator alone, for callers that judge candidates by
other means.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, fields
from itertools import combinations

from sqlglot import exp

from sqlassert import diagnostics as diag
from sqlassert import ir
from sqlassert.diagnostics import Diagnostic
from sqlassert.ir.convert import IrParser
from sqlassert.main import DEFAULT_DIALECT
from sqlassert.properties import NonNullColumn, NonNullColumns, NonNullUniqueSet, Property, UniqueJoin, UniqueSet
from sqlassert.python_engine import Arena, Engine, Result
from sqlassert.provenance import AttachmentSite
from sqlassert.sql_parse import SqlParser

DEFAULT_MAX_SIZE = 3


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


@dataclass(frozen=True)
class SuggestionReport:
    sql: str
    suggestions: tuple[Suggestion, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

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


def suggest(sql: str, *, dialect: str = DEFAULT_DIALECT, max_size: int = DEFAULT_MAX_SIZE) -> SuggestionReport:
    """Every Property sqlassert can prove about `sql`, as markers to paste.

    Per Select Expression: each minimal Unique Set up to `max_size` columns,
    as a Non-Null Unique Set when that also holds, plus one minimal set found
    among wider ones when none that small exists; and the non-null columns no
    suggested Non-Null Unique Set already implies. Per JOIN: Unique Join.
    Properties the SQL already asserts or declares are left out.
    """
    ast = SqlParser(dialect).parse(sql)
    conversion = IrParser(dialect).parse(ast)
    program, diagnostics = conversion.program, ast.diagnostics + conversion.diagnostics
    if any(diagnostic.code in _BLOCKING for diagnostic in diagnostics):
        return SuggestionReport(sql, (), diagnostics)
    present = (*program.declarations, *(assertion.property for assertion in program.assertions))
    marked_non_null = {
        column
        for item in present
        for column in (
            (item.column,) if isinstance(item, NonNullColumn) else item.columns if isinstance(item, NonNullColumns) else ()
        )
    }
    suggestions: list[Suggestion] = []
    with Engine().session(program, conversion.knowledge) as arena:
        for site, relation in program.attachment_sites:
            match relation:
                case ir.Join() if not site.trailing:
                    suggestions.extend(_proved(arena, site, UniqueJoin(join=relation), (), dialect))
                case _:
                    suggestions.extend(_select_suggestions(arena, site, relation, max_size, marked_non_null, dialect))
    marked = {_identity(item) for item in present}
    kept = tuple(
        sorted(
            (suggestion for suggestion in suggestions if _identity(suggestion.property) not in marked),
            key=lambda suggestion: suggestion.site.offset,
        )
    )
    return SuggestionReport(sql, kept, diagnostics)


# A parse failure leaves nothing to suggest on; duplicate declarations make
# `analyze` refuse every proof, so nothing suggested could round-trip.
_BLOCKING = {diag.SQL_PARSE_FAILED, diag.UNATTACHED_MARKER, diag.DUPLICATE_DECLARATION}


def candidates(
    program: ir.Program, *, dialect: str = DEFAULT_DIALECT, max_size: int = DEFAULT_MAX_SIZE
) -> Iterator[Candidate]:
    """Every candidate Property at every Attachment Site of `program`, proved or not.

    Per JOIN: Unique Join. Per Select Expression: each output column
    non-null, and each set of up to `max_size` output columns, plus the set
    of all of them, as a Unique Set and as a Non-Null Unique Set. Only
    columns a marker can name unambiguously take part.
    """
    for site, relation in program.attachment_sites:
        match relation:
            case ir.Join() if not site.trailing:
                yield _candidate(site, UniqueJoin(join=relation), (), dialect)
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
    return suggestions


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


def _candidate(site: AttachmentSite, property: Property, order: tuple[ir.OutputColumn, ...], dialect: str) -> Candidate:
    """`property` at `site`; its marker lists columns in `order`, the output order."""
    match property:
        case UniqueJoin():
            return Candidate(site, property, "/**ASSERT UNIQUE**/")
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
