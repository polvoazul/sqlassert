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

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, assert_never

from sqlglot import Expr, exp

from sqlassert import ir
from sqlassert.dialects import semantics
from sqlassert.properties import ExactlyOneJoin, ManyToOneJoin, NonNullColumn, NonNullColumns, NonNullUniqueSet
from sqlassert.rules.domain import _domain
from sqlassert.rules.equality import _identity_comparison, _same_type
from sqlassert.rules.exactly_one import _cross_equalities
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
    ONE_ROW = "LIMIT or ORDER BY on one row"
    SELF_JOIN = "Self-join on a key"
    IMPLIED_CONDITION = "Implied condition"
    UNUSED_CTE = "Unused CTE"


@dataclass(frozen=True)
class Improvement:
    """A result-preserving rewrite of the SQL at one site, with its proof."""

    site: AttachmentSite
    rewrite: Rewrite
    message: str
    evidence: Result[bool]
    level: ClassVar[Level] = Level.HIGH


def improvements(
    ast: ParsedProgram, program: ir.Program, arena: Arena, dialect: str,
    wanted: Callable[[AttachmentSite], bool] = lambda site: True,
) -> list[Improvement]:
    """Every Improvement for `program` at a site `wanted` accepts, in source
    order. `arena` must be active."""
    syntax = _syntax_at_sites(ast)
    parents: dict[ir.RelationExpr, list[ir.RelationExpr]] = {}
    for relation in program.relations():
        for child in ir.children(relation):
            if isinstance(child, ir.RelationExpr):
                parents.setdefault(child, []).append(relation)
    found: list[Improvement] = []
    sort_keys = dict(program.sort_keys)
    for site, relation in program.attachment_sites:
        if isinstance(relation, ir.SimpleCase) or not wanted(site):
            continue  # A CASE has nothing to rewrite yet.
        if relation not in parents and relation is not program.root:
            # Unreachable: lowered inside a Select that is opaque as a whole.
            # (Every reachable relation but the root is some relation's input.)
            continue
        node = syntax.get((site.offset, site.trailing))
        limit = relation if isinstance(relation, ir.Limit) else None
        if isinstance(relation, ir.Limit):
            relation = relation.input
        if site.trailing:
            found.extend(_redundant_subquery_filters(arena, site, relation))
            found.extend(_implied_where(arena, site, relation, program))
        if site.trailing and isinstance(node, exp.Select):
            one_row = _one_row_tail(site, relation, limit, node)
            found.extend(one_row or _redundant_sort_keys(site, relation, node, sort_keys.get(site), dialect))
        match relation:
            case ir.Join() if isinstance(node, exp.Join):
                removed = _redundant_join(arena, site, relation, node, dialect)
                same = None if removed else _self_join(arena, site, relation, node, program, dialect)
                inner = None if removed or same else _inner_join(arena, site, relation, parents)
                found.extend(item for item in (removed, same, inner) if item is not None)
                found.extend(_implied_conjuncts(arena, site, relation, relation.conjuncts, program, "ON"))
            case ir.Distinct():
                found.extend(_redundant_distinct(site, relation))
            case ir.Aggregate() if isinstance(node, exp.Select):
                projection = _grouping_is_projection(site, relation, node, dialect)
                found.extend([projection] if projection else _redundant_grouping(site, relation, node, dialect))
            case _:
                pass  # Other Select Expressions have nothing to rewrite yet.
    found.extend(item for item in _unused_ctes(ast) if wanted(item.site))
    if semantics(dialect).set_operations_all:
        found.extend(
            item for site, operation in program.set_operations
            if wanted(site) and (item := _set_operation_all(site, operation)) is not None
        )
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


# LIMIT and ORDER BY on at most one row ----------------------------------------------


def _one_row_tail(site: AttachmentSite, relation: ir.RelationExpr, limit: ir.Limit | None, select: exp.Select) -> list[Improvement]:
    """A Select proved to return at most one row (the empty set is a strict
    key of it, Kossmann's early abort) needs no ORDER BY, and no LIMIT of at
    least one row. Not with OFFSET, which may skip that row, nor a LIMIT whose
    count is unknown or zero."""
    sorted_ = select.args.get("order") is not None
    limited = select.args.get("limit") is not None and select.args.get("offset") is None and limit is not None and (limit.count or 0) >= 1
    if not sorted_ and not limited:
        return []
    evidence = _reaches_row_identity(_closure(relation, frozenset(), frozenset()))
    if not evidence.value:
        return []
    dropped = " and ".join(clause for clause, present in (("ORDER BY", sorted_), ("LIMIT", limited)) if present)
    message = f"this Select returns at most one row, so {dropped} {'change' if sorted_ and limited else 'changes'} nothing: drop {'them' if sorted_ and limited else 'it'}"
    return [Improvement(site, Rewrite.ONE_ROW, message, evidence)]


# Self-joins on a key -----------------------------------------------------------------


@dataclass(frozen=True)
class _Copy:
    """A column read from one reference (`alias`) to a relation, and the relation's own column."""

    alias: ir.Alias
    column: ir.OutputColumn


def _copy(column: ir.OutputColumn, program: ir.Program) -> _Copy | None:
    """Where `column` comes from, followed through pass-through columns of
    joins and filters down to the alias that reads its relation."""
    while True:
        relation = column.get_relation(program)
        match relation, column.scalar_expr:
            case ir.Alias(), ir.ColumnRef(column=source):
                return _Copy(relation, source)
            case ir.Join() | ir.Filter(), ir.ColumnRef(column=source):
                column = source
            case _:
                return None


def _self_join(arena: Arena, site: AttachmentSite, join: ir.Join, node: exp.Join, program: ir.Program, dialect: str) -> Improvement | None:
    """`a JOIN t b ON a.k = b.k`, where `a` reads the same relation `t` and
    `k` is a non-null key of it: each row of `a` matches itself, so every
    `b.x` is `a.x` and the join goes. Other ON conjuncts move to WHERE (an
    inner join; not ahead of a RIGHT or FULL JOIN, which would then pad
    differently) or, for a LEFT JOIN, guard each column read from `b`:
    `CASE WHEN cond THEN a.x END`, NULL where the join padded. The relation
    is a table, or a view or CTE whose rows are the same each time it is read
    (`_same_rows`) and whose columns read through `b` are pure (`_pure`)."""
    if join.kind not in {"inner", "left"} or not isinstance(join.right, ir.Alias) or node.args.get("using") or node.args.get("method"):
        return None
    select = node.parent
    if not isinstance(select, exp.Select) or any(isinstance(item, exp.Star) for item in select.expressions):
        return None
    pairs, _ = _cross_equalities(join)
    keys: dict[ir.OutputColumn, ir.OutputColumn] = {}  # The relation's key column, by the left column equal to it.
    alias: ir.Alias | None = None
    for left, right in pairs:
        mine, theirs = _copy(left, program), _copy(right, program)
        if mine is None or theirs is None or theirs.alias is not join.right or mine.column is not theirs.column:
            continue
        if alias is not None and mine.alias is not alias:
            return None
        alias = mine.alias
        keys[left] = mine.column
    if alias is None or alias.source is not join.right.source:
        return None
    relation = join.right.source
    keys = {left: column for left, column in keys.items() if arena.prove(NonNullColumn(column=column)).value}
    key = arena.prove(NonNullUniqueSet(columns=frozenset(keys.values())))
    if not key.value or not _same_rows(relation, program):
        return None
    if join.kind == "inner":
        present = arena.prove(NonNullColumns(columns=frozenset(keys)))
        if not present.value:
            return None  # A padded or NULL key matches nothing, so the join drops its row.
        key = key & present
    mine, theirs = alias.name, node.this.alias_or_name
    names = {column.name.lower() for column in keys.values()}
    on = node.args.get("on")
    conjuncts = _syntax_conjuncts(on) if on is not None else []
    extra = [conjunct for conjunct in conjuncts if not _key_equality(conjunct, mine, theirs, names)]
    if extra and join.kind == "inner" and _right_or_full_join_follows(node):
        return None
    if extra and join.kind == "left" and ir.determinants(join.predicate) is None:
        return None  # A volatile condition repeated per column would draw again.
    rewritten = select.copy()
    copied = next(other for other, original in zip(rewritten.args.get("joins") or (), select.args.get("joins") or ()) if original is node)
    columns = {column.name.lower(): column for column in relation.output_columns}
    guard = exp.and_(*(_requalified(conjunct, theirs, mine, names) for conjunct in extra)) if extra else None
    for column in list(rewritten.find_all(exp.Column)):
        if _within(column, copied):
            continue
        if column.find_ancestor(exp.Select) is not rewritten:
            if column.table.lower() == theirs.lower():
                return None  # Read in a nested query, which this rewrite does not reach.
            continue
        if not column.table:
            if column.name.lower() in columns:
                return None  # Unqualified: it may be read through either reference.
            continue
        if column.table.lower() != theirs.lower():
            continue
        source = columns.get(column.name.lower())
        if source is None or not _pure(source, program):
            return None
        value: exp.Expression = exp.column(column.name, table=mine)
        if guard is not None and join.kind == "left":
            value = exp.Case(ifs=[exp.If(this=guard.copy(), true=value)])
            if column.parent is rewritten:
                value = exp.alias_(value, column.name)
        column.replace(value)
    copied.pop()
    if guard is not None and join.kind == "inner":
        rewritten.where(guard, copy=False)
    message = (
        f"each {mine} row matches itself on the key ({', '.join(sorted(column.name for column in keys.values()))}), "
        f"so {theirs} is {mine}: delete the join and read {mine}'s columns: {rewritten.sql(dialect=dialect)}"
    )
    return Improvement(site, Rewrite.SELF_JOIN, message, key)


def _syntax_conjuncts(condition: exp.Expression) -> list[exp.Expression]:
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        return [*_syntax_conjuncts(condition.this), *_syntax_conjuncts(condition.expression)]
    return [condition]


def _key_equality(conjunct: exp.Expression, mine: str, theirs: str, names: set[str]) -> bool:
    """`mine.k = theirs.k` (either way round) for a key column `k`."""
    if not isinstance(conjunct, exp.EQ) or not isinstance(conjunct.this, exp.Column) or not isinstance(conjunct.expression, exp.Column):
        return False
    first, second = conjunct.this, conjunct.expression
    tables = {first.table.lower(), second.table.lower()}
    return first.name.lower() == second.name.lower() and first.name.lower() in names and tables == {mine.lower(), theirs.lower()}


def _requalified(expression: exp.Expression, old: str, new: str, names: set[str]) -> exp.Expression:
    """`expression` reading `new` where it read `old`; `new.x = old.x` becomes
    `new.x IS NOT NULL`, all it still checks once both are one row."""
    if isinstance(expression, exp.EQ) and _key_equality(expression, new, old, {expression.this.name.lower()}):
        return exp.Not(this=exp.Is(this=exp.column(expression.this.name, table=new), expression=exp.null()))
    copied = expression.copy()
    for column in copied.find_all(exp.Column):
        if column.table.lower() == old.lower():
            column.set("table", exp.to_identifier(new))
    return copied


def _same_rows(relation: ir.RelationExpr, program: ir.Program) -> bool:
    """Whether reading `relation` twice gives the same rows: a table, or a body
    with no LIMIT, QUALIFY or opaque part, no volatile condition, and no
    volatile column among those that decide which rows there are (DISTINCT,
    a set operation's, GROUP BY's). Other columns are checked where read."""
    relation = relation.closed
    match relation:
        case ir.NamedRelation(role=ir.RelationRole.TABLE):
            return True
        case ir.NamedRelation(body=None):
            return False
        case ir.NamedRelation(body=body) if body is not None:
            return _same_rows(body, program)
        case ir.Limit() | ir.QualifyByPartition() | ir.OpaqueRelation() | ir.RecursiveRelation():
            return False
        case ir.Filter(input=source, predicate=predicate):
            return (predicate is None or _pure_expression(predicate, program)) and _same_rows(source, program)
        case ir.Join(left=left, right=right, predicate=predicate):
            return (predicate is None or _pure_expression(predicate, program)) and _same_rows(left, program) and _same_rows(right, program)
        case ir.Distinct(input=source) | ir.Aggregate(input=source):
            deciding = relation.grouping_outputs if isinstance(relation, ir.Aggregate) else relation.output_columns
            return all(_pure(column, program) for column in deciding) and _same_rows(source, program)
        case ir.SetOperation(left=left, right=right):
            return (not relation.distinct or all(_pure(column, program) for column in (*left.output_columns, *right.output_columns))) and _same_rows(left, program) and _same_rows(right, program)
        case ir.Project(input=source) | ir.Alias(source=source):
            return _same_rows(source, program)
        case _:
            assert_never(relation)


def _pure(column: ir.OutputColumn, program: ir.Program) -> bool:
    """Whether `column` has the same value each time its row is read: a table
    column, or a pure function of such columns."""
    relation = column.get_relation(program)
    if isinstance(relation, ir.NamedRelation) and relation.role is ir.RelationRole.TABLE:
        return True
    return _pure_expression(column.scalar_expr, program)


def _pure_expression(expression: ir.ScalarExpr, program: ir.Program) -> bool:
    inputs = ir.determinants(expression)
    return inputs is not None and all(_pure(column, program) for column in inputs)


# Conditions the others imply ---------------------------------------------------------


def _implied_where(arena: Arena, site: AttachmentSite, relation: ir.RelationExpr, program: ir.Program) -> list[Improvement]:
    """The WHERE of the Select whose relation is `relation`."""
    while True:
        match relation:
            case ir.Project(input=source) | ir.Aggregate(input=source) | ir.Distinct(input=source) | ir.QualifyByPartition(input=source) | ir.Join(kind="semi" | "anti", left=source):
                relation = source
            case ir.Filter():
                return _implied_conjuncts(arena, site, relation, relation.conjuncts, program, "WHERE")
            case _:
                return []


def _implied_conjuncts(
    arena: Arena, site: AttachmentSite, relation: ir.Filter | ir.Join, conjuncts: tuple[ir.ScalarExpr, ...], program: ir.Program, clause: str,
) -> list[Improvement]:
    """Each conjunct TRUE on every row whose columns it reads are non-null,
    given the other conjuncts and what is known of the input: an equality
    two other equalities chain (`a = b AND b = c` give `a = c`), or both of
    whose sides are pinned to one value; a self-join's copies of one column
    (`_self_join`'s rows are the same row); `x = v` or `x IN (...)` whose
    listed values hold every value x can take (its Enumerated Domain). It is
    deleted when those columns are non-null, else it only removes rows where
    one is NULL: write `x IS NOT NULL` instead."""
    if isinstance(relation, ir.Join) and relation.kind not in {"inner", "left", "semi"}:
        return []
    found = []
    kept = list(conjuncts)
    for conjunct in conjuncts:
        others = [other for other in kept if other is not conjunct]
        implied = _implication(arena, relation, conjunct, others, program)
        if implied is None:
            continue
        reason, one_value = implied
        kept.remove(conjunct)  # One of several that imply each other stays.
        columns = ir.strict_inputs(conjunct)[:1] if one_value else ir.strict_inputs(conjunct)
        rejected = {column for other in others for column in ir.strict_inputs(other)}
        open_ = [column for column in columns if column not in rejected and not arena.prove(NonNullColumn(column=column)).value]
        written = conjunct.origin.detail
        if open_:
            written_columns = _written_columns(conjunct)
            check = " AND ".join(f"{written_columns.get(column, column.name)} IS NOT NULL" for column in open_)
            message = f"{written} is TRUE whenever its columns are non-null ({reason}), so it only removes NULLs: write {check} in {clause}"
        else:
            message = f"{written} is always TRUE here ({reason}): delete it from {clause}"
        found.append(Improvement(site, Rewrite.IMPLIED_CONDITION, message, Result(True, reason, (conjunct,))))
    return found


def _written_columns(conjunct: ir.ScalarExpr) -> dict[ir.OutputColumn, str]:
    """Each column `conjunct` reads, as the SQL wrote it."""
    found: dict[ir.OutputColumn, str] = {}
    pending: list[ir.Node] = [conjunct]
    while pending:
        node = pending.pop()
        if isinstance(node, ir.ColumnRef):
            found.setdefault(node.column, node.origin.detail)
        elif isinstance(node, ir.ScalarExpr):
            pending.extend(ir.children(node))
    return found


def _implication(
    arena: Arena, relation: ir.Filter | ir.Join, conjunct: ir.ScalarExpr, others: list[ir.ScalarExpr], program: ir.Program,
) -> tuple[str, bool] | None:
    """Why `conjunct` holds wherever its columns are non-null, and whether
    its columns are NULL together (one IS NOT NULL then checks both)."""
    match conjunct:
        case ir.Equality(left=ir.ColumnRef(column=first), right=ir.ColumnRef(column=second)):
            if _chained(first, second, others):
                return "the other equalities chain its sides", False
            pinned = _pinned(first, others), _pinned(second, others)
            if pinned[0] is not None and pinned[0] == pinned[1]:
                return f"both sides are {pinned[0].text}", False
            if isinstance(relation, ir.Join) and _same_row_column(first, second, others, relation, program, arena):
                return "both sides read one row's column through a self-join on its key", True
            return None
        case ir.Equality(left=ir.ColumnRef(column=column), right=ir.Constant(value=ir.LiteralValue() as value)) | ir.Equality(left=ir.Constant(value=ir.LiteralValue() as value), right=ir.ColumnRef(column=column)):
            listed = frozenset({value})
        case ir.InList(operand=ir.ColumnRef(column=column), values=values) if all(isinstance(item, ir.Constant) and item.value is not None for item in values):
            listed = frozenset(item.value for item in values if isinstance(item, ir.Constant) and item.value is not None)
        case _:
            return None
    domain = _domain(column).value
    if domain is None or not domain or not domain <= listed:
        return None
    return f"{column.name} is always one of the listed values", False


def _chained(first: ir.OutputColumn, second: ir.OutputColumn, others: list[ir.ScalarExpr]) -> bool:
    """Whether column-to-column equalities among `others`, over columns of
    one comparison type compared by identity, connect `first` to `second`."""
    edges = []
    for other in others:
        match other:
            case ir.Equality(left=ir.ColumnRef(column=left), right=ir.ColumnRef(column=right)) if (
                _same_type(left, right) and _identity_comparison(left).value and _identity_comparison(right).value
            ):
                edges.append((left, right))
            case _:
                pass
    reached, pending = {first}, [first]
    while pending:
        current = pending.pop()
        for left, right in edges:
            for here, there in ((left, right), (right, left)):
                if here is current and there not in reached:
                    reached.add(there)
                    pending.append(there)
    return second in reached and first is not second


def _pinned(column: ir.OutputColumn, others: list[ir.ScalarExpr]) -> ir.LiteralValue | None:
    """The one value another conjunct, or the column's domain, holds it to."""
    for other in others:
        match other:
            case ir.Equality(left=ir.ColumnRef(column=pinned), right=ir.Constant(value=value)) | ir.Equality(left=ir.Constant(value=value), right=ir.ColumnRef(column=pinned)) if pinned is column and value is not None:
                return value
            case _:
                pass
    domain = _domain(column).value
    return next(iter(domain)) if domain is not None and len(domain) == 1 else None


def _same_row_column(
    first: ir.OutputColumn, second: ir.OutputColumn, others: list[ir.ScalarExpr], join: ir.Join, program: ir.Program, arena: Arena,
) -> bool:
    """Whether `first` and `second` are one column of the one row a
    self-join on a non-null key of its relation, equated by `others`, pairs
    each row with."""
    mine, theirs = _copy(first, program), _copy(second, program)
    if mine is None or theirs is None or mine.column is not theirs.column or mine.alias is theirs.alias:
        return False
    if mine.alias.source is not theirs.alias.source or not _same_rows(mine.alias.source, program) or not _pure(mine.column, program):
        return False
    pairs = [
        (other.left.column, other.right.column) for other in others
        if isinstance(other, ir.Equality) and isinstance(other.left, ir.ColumnRef) and isinstance(other.right, ir.ColumnRef)
    ]
    keys = set()
    for left, right in pairs:
        a, b = _copy(left, program), _copy(right, program)
        if a is not None and b is not None and a.column is b.column and {a.alias, b.alias} == {mine.alias, theirs.alias}:
            keys.add(a.column)
    return bool(keys) and arena.prove(NonNullUniqueSet(columns=frozenset(keys))).value


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
