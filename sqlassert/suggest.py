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
from sqlassert.ir.convert import IrParser, _Lowered
from sqlassert.main import DEFAULT_DIALECT
from sqlassert.properties import NonNullColumn, NonNullColumns, NonNullUniqueSet, Property, UniqueJoin, UniqueSet
from sqlassert.python_engine import Arena, Engine, Result
from sqlassert.sql_parse import SqlParser, marker_offset

DEFAULT_MAX_SIZE = 3


@dataclass(frozen=True)
class AttachmentSite:
    """Where a marker for one JOIN or Select Expression is written.

    `offset` indexes the SQL text: a join marker is inserted there, before the
    join's first keyword; a column marker is appended there, after the Select
    Expression's last token. Anonymous Select Expressions share a label, and
    are told apart by `line`.
    """

    label: str
    line: int
    offset: int
    trailing: bool


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
class SitedProgram:
    """A Program IR, plus the IR node behind every Attachment Site."""

    program: ir.Program
    knowledge: tuple[Property, ...]
    diagnostics: tuple[Diagnostic, ...]
    joins: tuple[tuple[AttachmentSite, ir.Join], ...]
    selects: tuple[tuple[AttachmentSite, ir.RelationExpr], ...]
    dialect: str


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
    sited = lower_with_sites(sql, dialect=dialect)
    if any(diagnostic.code in _BLOCKING for diagnostic in sited.diagnostics):
        return SuggestionReport(sql, (), sited.diagnostics)
    present = (*sited.program.declarations, *(assertion.property for assertion in sited.program.assertions))
    marked_non_null = {
        column
        for item in present
        for column in (
            (item.column,) if isinstance(item, NonNullColumn) else item.columns if isinstance(item, NonNullColumns) else ()
        )
    }
    suggestions: list[Suggestion] = []
    with Engine().session(sited.program, sited.knowledge) as arena:
        for site, join in sited.joins:
            suggestions.extend(_proved(arena, site, UniqueJoin(join=join), (), sited.dialect))
        for site, relation in sited.selects:
            suggestions.extend(_select_suggestions(arena, site, relation, max_size, marked_non_null, sited.dialect))
    marked = {_identity(item) for item in present}
    kept = tuple(
        sorted(
            (suggestion for suggestion in suggestions if _identity(suggestion.property) not in marked),
            key=lambda suggestion: suggestion.site.offset,
        )
    )
    return SuggestionReport(sql, kept, sited.diagnostics)


# A parse failure leaves nothing to suggest on; duplicate declarations make
# `analyze` refuse every proof, so nothing suggested could round-trip.
_BLOCKING = {diag.SQL_PARSE_FAILED, diag.UNATTACHED_MARKER, diag.DUPLICATE_DECLARATION}


def lower_with_sites(sql: str, *, dialect: str = DEFAULT_DIALECT) -> SitedProgram:
    """Parse and lower `sql` as `analyze` does, recording each Attachment Site."""
    ast = SqlParser(dialect).parse(sql)
    parser = _SiteRecordingIrParser(dialect)
    conversion = parser.parse(ast)
    joins = tuple(
        (site, relation) for node, relation in parser.joins
        if (site := _site(sql, node, _join_label(node, dialect), trailing=False)) is not None
    )
    selects = tuple(
        (site, relation) for node, relation in parser.selects
        if (site := _site(sql, node, _select_label(node), trailing=True)) is not None
    )
    return SitedProgram(
        conversion.program, conversion.knowledge, ast.diagnostics + conversion.diagnostics,
        tuple(sorted(joins, key=lambda pair: pair[0].offset)),
        tuple(sorted(selects, key=lambda pair: pair[0].offset)),
        dialect,
    )


def candidates(sited: SitedProgram, *, max_size: int = DEFAULT_MAX_SIZE) -> Iterator[Candidate]:
    """Every candidate Property at every Attachment Site, proved or not.

    Per JOIN: Unique Join. Per Select Expression: each output column
    non-null, and each set of up to `max_size` output columns, plus the set
    of all of them, as a Unique Set and as a Non-Null Unique Set. Only
    columns a marker can name unambiguously take part.
    """
    for site, join in sited.joins:
        yield _candidate(site, UniqueJoin(join=join), (), sited.dialect)
    for site, relation in sited.selects:
        columns = _nameable_columns(relation)
        for column in columns:
            yield _candidate(site, NonNullColumns(columns=frozenset((column,))), columns, sited.dialect)
        for members in _column_sets(columns, max_size):
            yield _candidate(site, UniqueSet(columns=frozenset(members)), columns, sited.dialect)
            yield _candidate(site, NonNullUniqueSet(columns=frozenset(members)), columns, sited.dialect)


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


class _SiteRecordingIrParser(IrParser):
    """An IrParser that remembers which IR node each JOIN and Select became.

    The IR carries no SQLGlot nodes by design, so the pairing is captured
    while lowering, at the two points where column and join markers attach.
    """

    def __init__(self, dialect: str) -> None:
        super().__init__(dialect)
        self.joins: list[tuple[exp.Join, ir.Join]] = []
        self.selects: list[tuple[exp.Select, ir.RelationExpr]] = []

    def _lower_join(self, left: _Lowered, join: exp.Join) -> _Lowered:
        lowered = super()._lower_join(left, join)
        assert isinstance(lowered.relation, ir.Join)
        self.joins.append((join, lowered.relation))
        return lowered

    def _register_column_markers(self, relation: ir.RelationExpr, select: exp.Select) -> None:
        self.selects.append((select, relation))
        super()._register_column_markers(relation, select)


def _site(sql: str, node: exp.Join | exp.Select, label: str, *, trailing: bool) -> AttachmentSite | None:
    offset = marker_offset(node)
    if offset is None:
        return None
    return AttachmentSite(label, sql.count("\n", 0, offset) + 1, offset, trailing)


def _join_label(join: exp.Join, dialect: str) -> str:
    keywords = " ".join(part for part in (join.side, join.kind, "JOIN") if part)
    source = join.this
    if isinstance(source, exp.Subquery):
        return f"{keywords} subquery {source.alias}".rstrip()
    return f"{keywords} {source.sql(dialect=dialect)}"


def _select_label(select: exp.Select) -> str:
    node = select.parent
    while node is not None:
        match node:
            case exp.CTE():
                return f"CTE {node.alias}"
            case exp.Create():
                table = node.this.this if isinstance(node.this, exp.Schema) else node.this
                return f"view {table.name}"
            case exp.Subquery() if node.alias:
                return f"subquery {node.alias}"
            case exp.Subquery():
                return "subquery"
            case _:
                node = node.parent
    return "Root Select"
