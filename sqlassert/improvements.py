"""High-level suggestions: native SQL improvements justified by a proof.

Each Improvement names a rewrite that leaves the program's result unchanged,
as a multiset, and the evidence that it does:

- An outer join that never keeps a padded row is an inner join: it matches
  exactly one row, or a null-rejecting conjunct later in the same FROM and
  WHERE removes every row it pads (padding promotion), directly or through a
  later LEFT or FULL JOIN that then pads the same rows.
- A join none of whose columns is used outside its own ON is redundant when
  it keeps every left row once (a unique LEFT JOIN, an Exactly One JOIN), and
  only filters when it is a unique inner join (a `WHERE EXISTS`).
- A `DISTINCT` over rows that already differ, NULL equal to NULL, on the
  selected columns; a `GROUP BY` column the remaining grouping columns
  strictly determine.
- A CTE nothing references.
- A `WHERE EXISTS` or `IN` subquery (or a SEMI JOIN) every row passes: its
  semi join is proved Exactly One, so it keeps every row once.
- A `GROUP BY` whose grouping columns are a key of its input, NULL equal to
  NULL: each group is one row, so it is a projection.
- An `EXCEPT` whose left input, or an `INTERSECT` either of whose inputs,
  has no two rows alike: `EXCEPT ALL` and `INTERSECT ALL` give the same rows
  without removing duplicates, where the dialect has them.

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

from sqlglot import Expr, exp

from sqlassert import ir
from sqlassert.dialects import semantics
from sqlassert.properties import ExactlyOneJoin, ManyToOneJoin
from sqlassert.provenance import AttachmentSite
from sqlassert.python_engine import Arena, Result
from sqlassert.rules.column_set import RowIdentity
from sqlassert.rules.nullability import Padding, _null_rejected, _null_sources, _on_rejected
from sqlassert.rules.uniqueness import _closure, _reaches_row_identity
from sqlassert.sql_parse import ParsedProgram, grouping, marker_position


class Level(Enum):
    """How directly a suggestion improves the SQL; HIGH ranks first."""

    HIGH = "high"
    MID = "mid"
    LOW = "low"


class Rewrite(Enum):
    INNER_JOIN = "INNER JOIN"
    REDUNDANT_JOIN = "Redundant Join"
    FILTER_JOIN = "Filter-Only Join"
    REDUNDANT_DISTINCT = "Redundant DISTINCT"
    REDUNDANT_GROUPING = "Redundant GROUP BY"
    GROUPING_PROJECTION = "GROUP BY of one row per group"
    REDUNDANT_SORT_KEY = "Redundant ORDER BY key"
    REDUNDANT_FILTER = "Redundant EXISTS"
    SET_OPERATION_ALL = "EXCEPT ALL or INTERSECT ALL"
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
    sort_keys = dict(program.sort_keys)
    for site, relation in program.attachment_sites:
        if isinstance(relation, ir.SimpleCase):
            continue  # A CASE has nothing to rewrite yet.
        if relation not in parents and relation is not program.root:
            # Unreachable: lowered inside a Select that is opaque as a whole.
            # (Every reachable relation but the root is some relation's input.)
            continue
        node = syntax.get((site.offset, site.trailing))
        if isinstance(relation, ir.Limit):
            relation = relation.input
        if site.trailing:
            found.extend(_redundant_subquery_filters(arena, site, relation))
        if site.trailing and isinstance(node, exp.Select):
            found.extend(_redundant_sort_keys(site, relation, node, sort_keys.get(site), dialect))
        match relation:
            case ir.Join() if isinstance(node, exp.Join):
                removed = _redundant_join(arena, site, relation, node, dialect)
                inner = None if removed else _inner_join(arena, site, relation, parents)
                found.extend(item for item in (removed, inner) if item is not None)
            case ir.Distinct():
                found.extend(_redundant_distinct(site, relation))
            case ir.Aggregate() if isinstance(node, exp.Select):
                projection = _grouping_is_projection(site, relation, node, dialect)
                found.extend([projection] if projection else _redundant_grouping(site, relation, node, dialect))
            case _:
                pass  # Other Select Expressions have nothing to rewrite yet.
    found.extend(_unused_ctes(ast))
    if semantics(dialect).set_operations_all:
        found.extend(item for site, operation in program.set_operations if (item := _set_operation_all(site, operation)) is not None)
    return sorted(found, key=lambda item: item.site.offset)


def _redundant_subquery_filters(arena: Arena, site: AttachmentSite, relation: ir.RelationExpr) -> list[Improvement]:
    """The semi joins of this Select (its WHERE subqueries, issue 23, or an
    explicit SEMI JOIN) that match every row exactly once, so filter nothing."""
    found: list[Improvement] = []
    while True:
        match relation:
            case ir.Project(input=source) | ir.Aggregate(input=source) | ir.Distinct(input=source) | ir.QualifyByPartition(input=source) | ir.Filter(input=source):
                relation = source
            case ir.Join(kind="semi" | "anti", left=source):
                if relation.kind == "semi":
                    proof = arena.prove(ExactlyOneJoin(join=relation))
                    if proof.value:
                        message = f"every row matches exactly one row, so {relation.origin.detail} filters nothing: delete it"
                        found.append(Improvement(site, Rewrite.REDUNDANT_FILTER, message, proof))
                relation = source
            case _:
                return found


def _syntax_at_sites(ast: ParsedProgram) -> dict[tuple[int, bool], exp.Join | exp.Select]:
    """Each Join and Select by its Attachment Site's (offset, trailing)."""
    index: dict[tuple[int, bool], exp.Join | exp.Select] = {}
    for node in _nodes(ast, exp.Join, exp.Select):
        position = marker_position(node)
        if position is not None:
            index[(position[0], isinstance(node, exp.Select))] = node
    return index


def _nodes(ast: ParsedProgram, *types: type[Expr]) -> Iterator:
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
            message = f"every left row matches exactly one right row, so the {keyword} pads nothing: write JOIN"
            return Improvement(site, Rewrite.INNER_JOIN, message, exactly_one)
    # Joins that pad every row this one pads, each with why (issue 33): a
    # later LEFT or FULL JOIN whose ON rejects NULL in a column one of them
    # pads finds no match for those rows, so it pads them too.
    # Keyed by join and the side it pads: a FULL JOIN pads each side on different rows.
    chained: dict[tuple[ir.Join, bool], list[Result[bool]]] = {(join, join.kind == "right"): []}

    def padded_by(column: ir.OutputColumn) -> tuple[ir.Join, bool] | None:
        return next((
            (source.join, source.left) for source in _null_sources(column).value
            if isinstance(source, Padding) and (source.join, source.left) in chained
        ), None)

    relation: ir.RelationExpr = join
    while len(parents.get(relation, ())) == 1:
        parent, = parents[relation]
        match parent:
            case ir.Join(kind="anti"):
                rejected, clause = {}, ""
            case ir.Join(kind=kind):
                # A semi join (a WHERE EXISTS or IN) keeps only rows its ON holds on.
                described = "WHERE EXISTS or IN subquery" if kind == "semi" else f"later {kind.upper()} JOIN"
                rejected, clause = _on_rejected(parent), f"the ON condition of a {described}"
            case ir.Filter():
                rejected, clause = _null_rejected(parent.conjuncts), "the WHERE clause"
            case _:
                return None
        for column, conjunct in rejected.items():
            via = padded_by(column)
            if via is not None:
                how = f"is padded by the {keyword}" if via[0] is join else f"is NULL on every row the {keyword} pads"
                padded = Result(True, f"{column.name} {how}", (join, column), chained[via])
                evidence = Result(
                    True, f"{clause} rejects NULL in {column.name}, so every row the {keyword} pads is removed",
                    (conjunct, column), [padded],
                )
                message = f"{clause} rejects NULL in {column.name}, so no padded row survives: write JOIN"
                return Improvement(site, Rewrite.INNER_JOIN, message, evidence)
        if isinstance(parent, ir.Join) and parent.kind in {"left", "full"}:
            for column, conjunct in _null_rejected(parent.conjuncts).items():
                via = padded_by(column)
                # The left rows it cannot match keep a padded right side.
                if via is not None and column in parent.left.output_columns and (parent, False) not in chained:
                    reason = f"its ON rejects NULL in {column.name}, so the later {parent.kind.upper()} JOIN pads every row the {keyword} pads"
                    chained[(parent, False)] = [*chained[via], Result(True, reason, (conjunct, column))]
        if isinstance(parent, ir.Filter):
            return None
        relation = parent
    return None


# Redundant ORDER BY keys -----------------------------------------------------------


def _redundant_sort_keys(
    site: AttachmentSite, relation: ir.RelationExpr, select: exp.Select, keys: tuple[ir.OutputColumn, ...] | None, dialect: str
) -> list[Improvement]:
    """Each ORDER BY key the keys before it determine, NULL equal to NULL
    (Paulley §3, reducing the sort list): rows tied on the earlier keys agree
    on it, so it never breaks a tie. Only while the earlier keys compare by
    identity: a collation or a float ties values its equality tells apart.
    Silent when a key is an expression or a name that does not resolve to one
    column, and for DISTINCT ON, whose ORDER BY picks the rows it keeps."""
    from sqlassert.smells import _ties_identical

    if not keys:
        return []
    items = select.args["order"].expressions
    scope = relation.input if isinstance(relation, ir.Project) else relation
    found = []
    for position in range(1, len(keys)):
        if not all(_ties_identical(key) for key in keys[:position]):
            break
        closure = _closure(scope, frozenset(keys[:position]), frozenset())
        key = keys[position]
        if key not in closure.value.columns:
            continue
        written = [(item.this if isinstance(item, exp.Ordered) else item).sql(dialect=dialect) for item in items]
        message = f"({', '.join(written[:position])}) determine {written[position]}, NULL equal to NULL, so it never breaks a tie: drop it from ORDER BY"
        evidence = Result(True, "The earlier ORDER BY keys determine this one", (key,), [closure.members[key]])
        found.append(Improvement(site, Rewrite.REDUNDANT_SORT_KEY, message, evidence))
    return found


# Redundant joins ------------------------------------------------------------------


def _redundant_join(arena: Arena, site: AttachmentSite, join: ir.Join, node: exp.Join, dialect: str) -> Improvement | None:
    if join.kind not in {"inner", "left"} or _right_side_used(join, node):
        return None
    source = node.this.sql(dialect=dialect)
    unused = f"no column of {source} is used outside its ON"
    if join.kind == "left":
        unique = arena.prove(ManyToOneJoin(join=join))
        if unique.value:
            message = f"at most one match per row and {unused}: delete the join"
            return Improvement(site, Rewrite.REDUNDANT_JOIN, message, unique)
        return None
    exactly_one = arena.prove(ExactlyOneJoin(join=join))
    if exactly_one.value:
        message = f"exactly one match per row and {unused}: delete the join"
        return Improvement(site, Rewrite.REDUNDANT_JOIN, message, exactly_one)
    unique = arena.prove(ManyToOneJoin(join=join))
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


def _within(node: Expr, ancestor: Expr) -> bool:
    current: Expr | None = node
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


def _grouping_is_projection(site: AttachmentSite, aggregate: ir.Aggregate, node: exp.Select, dialect: str) -> Improvement | None:
    """A GROUP BY whose grouping columns no two input rows agree on, NULL
    equal to NULL (GROUP BY puts NULLs in one group): every group is one row,
    so the query is a projection, each aggregate its value over that row.
    An empty input gives no rows either way. Silent when a grouping
    expression is not a column, or an aggregate has no one-row form here
    (`_one_row`), or is read outside the select list (ORDER BY, QUALIFY, a
    window over it)."""
    expressions = grouping(node) or []
    outputs = aggregate.grouping_outputs
    if not outputs or len(expressions) != len(outputs) or not all(isinstance(column.scalar_expr, ir.ColumnRef) for column in outputs):
        return None
    if any(
        call.find_ancestor(exp.Select) is node and not any(_within(call, item) for item in node.expressions)
        for call in node.find_all(exp.AggFunc)
    ) or node.args.get("qualify") is not None or any(item.find(exp.Window) for item in node.expressions):
        return None
    keys = frozenset(column.scalar_expr.column for column in outputs if isinstance(column.scalar_expr, ir.ColumnRef))
    evidence = _reaches_row_identity(_closure(aggregate.input, keys, frozenset()))
    if not evidence.value:
        return None
    items = []
    for item in node.expressions:
        rewritten = _one_row(item.copy(), dialect)
        if rewritten is None:
            return None
        items.append(rewritten)
    names = ", ".join(expression.sql(dialect=dialect) for expression in expressions)
    message = (
        f"no two rows agree on ({names}), NULL equal to NULL, so every group is one row: "
        f"drop GROUP BY and write SELECT {', '.join(items)}"
    )
    return Improvement(site, Rewrite.GROUPING_PROJECTION, message, evidence)


def _one_row(item: Expr, dialect: str) -> str | None:
    """`item` with each aggregate in it replaced by its value over one row,
    or None when an aggregate has no such form here: count(*) is 1, count(x)
    is 1 unless x is NULL, sum, avg, min, max, any_value, bool_and and bool_or
    are x. With or without DISTINCT, IGNORE or RESPECT NULLS; never with
    FILTER, nor a count of several values."""
    for call in list(item.find_all(exp.AggFunc)):
        target: Expr = call
        while isinstance(target.parent, exp.IgnoreNulls | exp.RespectNulls):
            target = target.parent
        if isinstance(target.parent, exp.Filter) or any(value for key, value in call.args.items() if key not in {"this", "big_int"}):
            return None
        argument = call.this
        if isinstance(argument, exp.Distinct):
            if len(argument.expressions) != 1:
                return None
            argument = argument.expressions[0]
        match call:
            case exp.Count() if argument is None or isinstance(argument, exp.Star):
                value: Expr = exp.Literal.number(1)
            case exp.Count():
                value = exp.Case(ifs=[exp.If(this=exp.Is(this=argument.copy(), expression=exp.null()), true=exp.Literal.number(0))], default=exp.Literal.number(1))
            case exp.Sum() | exp.Avg() | exp.Min() | exp.Max() | exp.AnyValue() | exp.LogicalAnd() | exp.LogicalOr() if isinstance(argument, Expr) and not isinstance(argument, exp.Star | exp.Order):
                value = argument.copy()
            case _:
                return None
        if target is item:
            item = value
        else:
            target.replace(value)
    return item.sql(dialect=dialect)


def _redundant_grouping(site: AttachmentSite, aggregate: ir.Aggregate, node: exp.Select, dialect: str) -> list[Improvement]:
    """Each grouping column the grouping columns kept so far strictly determine.

    One column always stays: grouping by nothing turns an empty input into one row.
    """
    expressions = grouping(node) or []
    if len(expressions) != len(aggregate.grouping_outputs):
        return []
    # Grouping outputs are lowered in GROUP BY order.
    written = {column: expression.sql(dialect=dialect) for column, expression in zip(aggregate.grouping_outputs, expressions)}
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
        names = ", ".join(written[other] for other in others)
        alias = exp.to_identifier(grouped.name).sql(dialect=dialect)
        selected = node.expressions[aggregate.output_columns.index(grouped)]
        expression = selected.this if isinstance(selected, exp.Alias) else selected
        message = (
            f"({names}) determine {written[grouped]}, NULL equal to NULL: drop {written[grouped]} from GROUP BY "
            f"and select any_value({expression.sql(dialect=dialect)}) AS {alias}"
        )
        found.append(Improvement(site, Rewrite.REDUNDANT_GROUPING, message, closure.members[source]))
    return found


# EXCEPT and INTERSECT over inputs without duplicates --------------------------------


def _set_operation_all(site: AttachmentSite, operation: ir.SetOperation) -> Improvement | None:
    """`A EXCEPT B` keeps each distinct row of A that B lacks; `EXCEPT ALL`
    keeps it as often as A has it more than B. When A has no two rows alike,
    NULL equal to NULL as a set operation compares them, both keep each such
    row once. `INTERSECT ALL` keeps a row as often as the input with fewer of
    it, so a duplicate-free input on either side suffices."""
    if not operation.distinct or operation.operator == "union":
        return None
    keyword = operation.operator.upper()
    sides = [("left", operation.left)] + ([("right", operation.right)] if operation.operator == "intersect" else [])
    for side, relation in sides:
        evidence = _reaches_row_identity(_closure(relation, frozenset(relation.output_columns), frozenset()))
        if evidence.value:
            message = f"no two rows of the {side} input are alike, NULL equal to NULL, so {keyword} removes no duplicates: write {keyword} ALL"
            return Improvement(site, Rewrite.SET_OPERATION_ALL, message, evidence)
    return None


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


def _table_names(node: Expr, skip: Expr | None = None) -> set[str]:
    return {table.name.lower() for table in node.find_all(exp.Table) if skip is None or not _within(table, skip)}
