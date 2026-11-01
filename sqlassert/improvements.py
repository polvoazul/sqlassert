"""High-level suggestions: native SQL improvements justified by a proof.

Each Improvement names a rewrite that leaves the program's result unchanged,
as a multiset, and the evidence that it does:

- An outer join that never keeps a padded row is an inner join: it matches
  exactly one row, or a null-rejecting conjunct later in the same FROM and
  WHERE removes every row it pads (padding promotion).
- A join none of whose columns is used outside its own ON is redundant when
  it keeps every left row once (a unique LEFT JOIN, an Exactly One JOIN), and
  only filters when it is a unique inner join (a `WHERE EXISTS`).
- A `DISTINCT` over rows that already differ, NULL equal to NULL, on the
  selected columns; a `GROUP BY` column the remaining grouping columns
  strictly determine.
- A CTE nothing references.

Proofs come from the rules' internals (null sources, strict closure) as well
as `Arena.prove`, since none of these facts is a Property. Whether a column
is used is read from the syntax: the IR keeps neither ORDER BY nor the
columns inside opaque expressions. Anything the checks cannot see whole --
a padded row leaving its own Select Expression, a join whose right side's
columns are unknown -- is left alone.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from sqlglot import exp

from sqlassert import ir
from sqlassert.properties import ExactlyOneJoin, UniqueJoin
from sqlassert.provenance import AttachmentSite
from sqlassert.python_engine import Arena, Result
from sqlassert.rules.column_set import RowIdentity
from sqlassert.rules.nullability import Padding, _null_rejected, _null_sources, _on_rejected
from sqlassert.rules.uniqueness import _closure, _reaches_row_identity
from sqlassert.sql_parse import ParsedProgram, marker_position


class Level(Enum):
    """How directly a suggestion improves the SQL; HIGH ranks first."""

    HIGH = "high"
    MID = "mid"


class Rewrite(Enum):
    INNER_JOIN = "INNER JOIN"
    REDUNDANT_JOIN = "Redundant Join"
    FILTER_JOIN = "Filter-Only Join"
    REDUNDANT_DISTINCT = "Redundant DISTINCT"
    REDUNDANT_GROUPING = "Redundant GROUP BY"
    UNUSED_CTE = "Unused CTE"


@dataclass(frozen=True)
class Improvement:
    """A result-preserving rewrite of the SQL at one site, with its proof."""

    site: AttachmentSite
    rewrite: Rewrite
    message: str
    evidence: Result[bool]
    level: ClassVar[Level] = Level.HIGH


def improvements(ast: ParsedProgram, program: ir.Program, arena: Arena, dialect: str) -> list[Improvement]:
    """Every Improvement for `program`, in source order. `arena` must be active."""
    syntax = _syntax_at_sites(ast)
    parents: dict[ir.RelationExpr, list[ir.RelationExpr]] = {}
    for relation in program.relations():
        for child in ir.children(relation):
            if isinstance(child, ir.RelationExpr):
                parents.setdefault(child, []).append(relation)
    found: list[Improvement] = []
    for site, relation in program.attachment_sites:
        if relation not in parents and relation is not program.root:
            # Unreachable: lowered inside a Select that is opaque as a whole.
            # (Every reachable relation but the root is some relation's input.)
            continue
        node = syntax.get((site.offset, site.trailing))
        match relation:
            case ir.Join() if isinstance(node, exp.Join):
                removed = _redundant_join(arena, site, relation, node, dialect)
                inner = None if removed else _inner_join(arena, site, relation, parents)
                found.extend(item for item in (removed, inner) if item is not None)
            case ir.Distinct():
                found.extend(_redundant_distinct(site, relation))
            case ir.Aggregate():
                found.extend(_redundant_grouping(site, relation))
            case _:
                pass  # Other Select Expressions have nothing to rewrite yet.
    found.extend(_unused_ctes(ast))
    return sorted(found, key=lambda item: item.site.offset)


def _syntax_at_sites(ast: ParsedProgram) -> dict[tuple[int, bool], exp.Join | exp.Select]:
    """Each Join and Select by its Attachment Site's (offset, trailing)."""
    index: dict[tuple[int, bool], exp.Join | exp.Select] = {}
    for node in _nodes(ast, exp.Join, exp.Select):
        position = marker_position(node)
        if position is not None:
            index[(position[0], isinstance(node, exp.Select))] = node
    return index


def _nodes(ast: ParsedProgram, *types: type[exp.Expression]) -> Iterator:
    for statement in (*ast.create_statements, ast.root_select):
        if statement is not None:
            yield from statement.find_all(*types)


# Outer joins that cannot pad ------------------------------------------------------


def _inner_join(
    arena: Arena, site: AttachmentSite, join: ir.Join, parents: dict[ir.RelationExpr, list[ir.RelationExpr]]
) -> Improvement | None:
    """An outer join none of whose padded rows reaches the result.

    Only the joins above it in the same FROM and its own WHERE are searched:
    within one Select Expression nothing between them looks at a row as a
    whole, while past it a LIMIT, window, or aggregate may.
    """
    if join.kind not in {"left", "right"}:
        return None
    keyword = f"{join.kind.upper()} JOIN"
    if join.kind == "left":
        exactly_one = arena.prove(ExactlyOneJoin(join=join))
        if exactly_one.value:
            message = f"every row matches exactly one row, so the {keyword} pads nothing: write JOIN"
            return Improvement(site, Rewrite.INNER_JOIN, message, exactly_one)
    relation: ir.RelationExpr = join
    while len(parents.get(relation, ())) == 1:
        parent, = parents[relation]
        match parent:
            case ir.Join(kind=kind) if kind not in {"semi", "anti"}:
                rejected, clause = _on_rejected(parent), f"the ON condition of a later {kind.upper()} JOIN"
            case ir.Filter():
                rejected, clause = _null_rejected(parent.conjuncts), "the WHERE clause"
            case _:
                return None
        for column, conjunct in rejected.items():
            if any(isinstance(source, Padding) and source.join is join for source in _null_sources(column).value):
                padded = Result(True, f"{column.name} is padded by the {keyword}", (join, column))
                evidence = Result(
                    True, f"{clause} rejects NULL in {column.name}, so every row the {keyword} pads is removed",
                    (conjunct, column), [padded],
                )
                message = f"{clause} rejects NULL in {column.name}, so no padded row survives: write JOIN"
                return Improvement(site, Rewrite.INNER_JOIN, message, evidence)
        if isinstance(parent, ir.Filter):
            return None
        relation = parent
    return None


# Redundant joins ------------------------------------------------------------------


def _redundant_join(arena: Arena, site: AttachmentSite, join: ir.Join, node: exp.Join, dialect: str) -> Improvement | None:
    if join.kind not in {"inner", "left"} or _right_side_used(join, node):
        return None
    source = node.this.sql(dialect=dialect)
    unused = f"no column of {source} is used outside its ON"
    if join.kind == "left":
        unique = arena.prove(UniqueJoin(join=join))
        if unique.value:
            message = f"at most one match per row and {unused}: delete the join"
            return Improvement(site, Rewrite.REDUNDANT_JOIN, message, unique)
        return None
    exactly_one = arena.prove(ExactlyOneJoin(join=join))
    if exactly_one.value:
        message = f"exactly one match per row and {unused}: delete the join"
        return Improvement(site, Rewrite.REDUNDANT_JOIN, message, exactly_one)
    unique = arena.prove(UniqueJoin(join=join))
    if not unique.value or _right_or_full_join_follows(node):
        # A filter moved to WHERE would act after a later RIGHT or FULL JOIN,
        # which then no longer pads the rows this join used to drop first.
        return None
    on = node.args.get("on")
    condition = f" WHERE {on.sql(dialect=dialect)}" if on is not None else ""
    message = (
        f"at most one match per row and {unused}, so it only filters: "
        f"write WHERE EXISTS (SELECT 1 FROM {source}{condition})"
    )
    return Improvement(site, Rewrite.FILTER_JOIN, message, unique)


def _right_side_used(join: ir.Join, node: exp.Join) -> bool:
    """Whether anything in the join's Select Expression but its own ON may
    read a column of its right side. Unknown counts as used."""
    select = node.parent
    name = node.this.alias_or_name.lower()
    if not isinstance(select, exp.Select) or not name or not join.right.is_schema_complete or node.args.get("using"):
        return True
    if node.args.get("method"):
        return True  # NATURAL: the condition is implicit.
    columns = {column.name.lower() for column in join.right.output_columns}
    if any(isinstance(item, exp.Star) for item in select.expressions):
        return True
    on = node.args.get("on")
    for column in select.find_all(exp.Column):
        if on is not None and _within(column, on):
            continue
        if column.table.lower() == name if column.table else column.name.lower() in columns:
            return True
    return any(
        identifier.name.lower() in columns
        for other in select.args.get("joins") or ()
        for identifier in other.args.get("using") or ()
    )


def _right_or_full_join_follows(node: exp.Join) -> bool:
    joins = list(node.parent.args.get("joins") or ()) if node.parent is not None else []
    later = joins[next(index for index, other in enumerate(joins) if other is node) + 1:]
    return any((other.side or "").upper() in {"RIGHT", "FULL"} for other in later)


def _within(node: exp.Expression, ancestor: exp.Expression) -> bool:
    current: exp.Expression | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


# DISTINCT and GROUP BY ------------------------------------------------------------


def _redundant_distinct(site: AttachmentSite, distinct: ir.Distinct) -> list[Improvement]:
    """DISTINCT over input rows no two of which agree on the selected
    columns, NULL equal to NULL: a strict key of the input."""
    selected = {
        column.scalar_expr.column: column.name
        for column in distinct.output_columns if isinstance(column.scalar_expr, ir.ColumnRef)
    }
    key = _strict_key(distinct.input, tuple(selected))
    if key is None:
        return []
    evidence = _reaches_row_identity(_closure(distinct.input, frozenset(key), frozenset()))
    names = ", ".join(selected[column] for column in key)
    message = f"no two rows agree on ({names}), NULL equal to NULL, so DISTINCT removes nothing: delete DISTINCT"
    return [Improvement(site, Rewrite.REDUNDANT_DISTINCT, message, evidence)]


def _strict_key(relation: ir.RelationExpr, columns: tuple[ir.OutputColumn, ...]) -> tuple[ir.OutputColumn, ...] | None:
    """A minimal subset of `columns` that strictly determines a row of `relation`, if any."""

    def reaches(members: tuple[ir.OutputColumn, ...]) -> bool:
        return RowIdentity(relation) in _closure(relation, frozenset(members), frozenset()).value.columns

    if not columns or not reaches(columns):
        return None
    for column in columns:
        rest = tuple(other for other in columns if other is not column)
        if rest and reaches(rest):
            return _strict_key(relation, rest)
    return columns


def _redundant_grouping(site: AttachmentSite, aggregate: ir.Aggregate) -> list[Improvement]:
    """Each grouping column the grouping columns kept so far strictly determine.

    One column always stays: grouping by nothing turns an empty input into one row.
    """
    kept = list(aggregate.grouping_outputs)
    found = []
    for grouped in aggregate.grouping_outputs:
        others = [other for other in kept if other is not grouped]
        if not others or not isinstance(grouped.scalar_expr, ir.ColumnRef):
            continue
        determinant = frozenset(other.scalar_expr.column for other in others if isinstance(other.scalar_expr, ir.ColumnRef))
        closure = _closure(aggregate.input, determinant, frozenset())
        source = grouped.scalar_expr.column
        if source not in closure.value.columns:
            continue
        kept.remove(grouped)
        names = ", ".join(other.name for other in others)
        message = (
            f"({names}) determine {grouped.name}, NULL equal to NULL: drop {grouped.name} from GROUP BY "
            f"and select any_value({grouped.name}) AS {grouped.name}"
        )
        found.append(Improvement(site, Rewrite.REDUNDANT_GROUPING, message, closure.members[source]))
    return found


# Unused CTEs ----------------------------------------------------------------------


def _unused_ctes(ast: ParsedProgram) -> list[Improvement]:
    """CTEs no query of their WITH reaches, directly or through a used CTE.

    A name is matched wherever it appears as a table, shadowed or not, so a
    CTE is only ever kept by mistake.
    """
    found = []
    for with_ in _nodes(ast, exp.With):
        query = with_.parent
        ctes = {cte.alias_or_name.lower(): cte for cte in with_.expressions}
        live = {name for name in _table_names(query, skip=with_) if name in ctes}
        pending = list(live)
        while pending:
            for name in _table_names(ctes[pending.pop()].this):
                if name in ctes and name not in live:
                    live.add(name)
                    pending.append(name)
        for name, cte in ctes.items():
            body = cte.this.find(exp.Select)
            position = marker_position(body) if body is not None else None
            if name in live or position is None:
                continue
            offset, line = position
            site = AttachmentSite(f"CTE {cte.alias_or_name}", line, offset, True)
            evidence = Result(True, f"No query of its WITH clause references CTE {cte.alias_or_name}")
            message = f"CTE {cte.alias_or_name} is never referenced, so it never runs and its markers go unchecked: delete it"
            found.append(Improvement(site, Rewrite.UNUSED_CTE, message, evidence))
    return found


def _table_names(node: exp.Expression, skip: exp.Expression | None = None) -> set[str]:
    return {table.name.lower() for table in node.find_all(exp.Table) if skip is None or not _within(table, skip)}
