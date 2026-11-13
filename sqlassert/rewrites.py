"""Rewrite suggestions: changes to the SQL justified by a proof.

Each Rewrite names a change that leaves the program's result unchanged,
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
  NULL: each group is one row, so it is a projection. A Rewrite with
  Caveat when an aggregate's one-row value has another type or precision
  (`sum(x)` is x's type, not sum's; `sum(x) + 1` may overflow as `x + 1`).
- An `EXCEPT` whose left input, or an `INTERSECT` either of whose inputs,
  has no two rows alike: `EXCEPT ALL` and `INTERSECT ALL` give the same rows
  without removing duplicates, where the dialect has them.

Proofs come from the rules' internals (null sources, strict closure) as well
as `Arena.prove`, since none of these facts is a Property. Whether a column
is used is read from the syntax: the IR keeps neither ORDER BY nor the
columns inside opaque expressions. Anything the checks cannot see whole --
a padded row leaving its own Select Expression, a join whose right side's
columns are unknown -- is left alone.

Each Rewrite carries the exact edits that make it (`edits.py`), placed from
the SQL's tokens; one whose syntax they cannot place is not offered.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Literal, assert_never

import sqlglot
from sqlglot import Expr, exp
from sqlglot.tokens import TokenType

from sqlassert.diagnostics import quoted, quoted_all
from sqlassert.edits import Edit, Source, apply_edits, unified_diff
from sqlassert import ir
from sqlassert.dialects import semantics
from sqlassert.properties import ExactlyOneJoin, ManyToOneJoin, NonNullColumn, NonNullColumns, NonNullUniqueSet
from sqlassert.rules.domain import _domain
from sqlassert.rules.equality import _comparison_type, _identity_comparison, _pins, _same_set_types, _same_type
from sqlassert.rules.exactly_one import _cross_equalities
from sqlassert.provenance import AttachmentSite
from sqlassert.python_engine import Arena, Result
from sqlassert.rules.column_set import RowIdentity
from sqlassert.rules.nullability import (
    Padding, _null_rejected, _null_sources, _on_rejected, _on_rejected_paddings, _rejected_paddings,
)
from sqlassert.rules.uniqueness import _closure, _reaches_row_identity
from sqlassert.sql_parse import ParsedProgram, grouping, marker_position, span, written_sql


class SuggestType(Enum):
    """What a suggestion is, listed in this order: a rewrite of the SQL that
    keeps its rows and every column's type and adds no error; a rewrite with
    caveat, which keeps its rows but may change a column's type or precision,
    or overflow, as its message says; a marker to paste; a hazard in its
    result."""

    REWRITE = "rewrite"
    REWRITE_WITH_CAVEAT = "rewrite with caveat"
    MARKER = "marker"
    HAZARD = "hazard"


class RewriteKind(Enum):
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
class Rewrite:
    """A result-preserving rewrite of the SQL at one site, with its proof.
    `edits` make it, on the SQL it was found in (one is offered only with
    them: where its syntax cannot be placed from the tokens, there is none);
    `message` says why, and what to do in a word. Its `suggest_type` is REWRITE_WITH_CAVEAT when a
    column's type or precision may change, which the message names;
    sqlassert has no result-type model per dialect, so such a change is
    caveated."""

    site: AttachmentSite
    kind: RewriteKind
    message: str
    edits: tuple[Edit, ...]
    evidence: Result[bool]
    suggest_type: SuggestType = SuggestType.REWRITE

    def __post_init__(self) -> None:
        if not self.edits:
            raise ValueError(f"a {self.kind.value} Rewrite without edits")

    def rewritten(self, sql: str) -> str:
        """`sql`, the SQL this Rewrite was found in, rewritten."""
        return apply_edits(sql, self.edits)

    def diff(self, sql: str, *, name: str = "original", context: int = 2) -> str:
        """The unified diff from `sql`, the SQL this Rewrite was found in, to
        its rewrite; `name` labels it."""
        return unified_diff(sql, self.edits, name=name, context=context)


def rewrites(
    ast: ParsedProgram, program: ir.Program, arena: Arena, dialect: str,
    wanted: Callable[[AttachmentSite], bool] = lambda site: True,
) -> list[Rewrite]:
    """Every Rewrite for `program` at a site `wanted` accepts, in source
    order. `arena` must be active."""
    syntax = _syntax_at_sites(ast)
    source = Source(ast.sql, ast.tokens)
    parents: dict[ir.RelationExpr, list[ir.RelationExpr]] = {}
    for relation in program.relations():
        for child in ir.children(relation):
            if isinstance(child, ir.RelationExpr):
                parents.setdefault(child, []).append(relation)
    found: list[Rewrite] = []
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
        if site.trailing and isinstance(node, exp.Select):
            found.extend(_redundant_subquery_filters(arena, site, relation, node, source, dialect))
            found.extend(_implied_where(arena, site, relation, program, node, source, dialect))
        if site.trailing and isinstance(node, exp.Select):
            one_row = _one_row_tail(site, relation, limit, node, source)
            found.extend(one_row or _redundant_sort_keys(site, relation, node, sort_keys.get(site), dialect, source))
        match relation:
            case ir.Join() if isinstance(node, exp.Join):
                same = _self_join(arena, site, relation, node, program, dialect, source)
                removed = None if same else _redundant_join(arena, site, relation, node, dialect, source)
                inner = None if removed or same else _inner_join(arena, site, relation, parents, node, source)
                found.extend(item for item in (removed, same, inner) if item is not None)
                written, _ = _on_conjuncts(node, source)
                found.extend(_implied_conjuncts(arena, site, relation, program, "ON", written, None, source, dialect))
            case ir.Distinct() if isinstance(node, exp.Select):
                found.extend(_redundant_distinct(site, relation, node, source))
            case ir.Aggregate() | ir.Project(input=ir.Aggregate()) if isinstance(node, exp.Select):
                # A Project over the Aggregate drops its hidden outputs and
                # computes the items over aggregates (`_lower_aggregate`).
                aggregate = relation if isinstance(relation, ir.Aggregate) else relation.input
                assert isinstance(aggregate, ir.Aggregate)
                projection = _grouping_is_projection(site, aggregate, node, dialect, source)
                found.extend([projection] if projection else _redundant_grouping(site, relation, aggregate, node, dialect, source))
            case _:
                pass  # Other Select Expressions have nothing to rewrite yet.
    found.extend(item for item in _unused_ctes(ast, source) if wanted(item.site))
    if semantics(dialect).set_operations_all:
        found.extend(
            item for site, operation in program.set_operations
            if wanted(site) and (item := _set_operation_all(site, operation, source)) is not None
        )
    return sorted(found, key=lambda item: item.site.offset)


def _redundant_subquery_filters(
    arena: Arena, site: AttachmentSite, relation: ir.RelationExpr, node: exp.Select, source: Source, dialect: str,
) -> list[Rewrite]:
    """The semi joins of this Select (its WHERE subqueries, issue 23, or an
    explicit SEMI JOIN) that match every row exactly once, so filter nothing.
    The edit deletes the WHERE conjunct (`_deleted_conjunct`) or the join."""
    found: list[Rewrite] = []
    while True:
        match relation:
            case ir.Project(input=input_) | ir.Aggregate(input=input_) | ir.Distinct(input=input_) | ir.QualifyByPartition(input=input_) | ir.Filter(input=input_):
                relation = input_
            case ir.Join(kind="semi" | "anti", left=input_):
                if relation.kind == "semi":
                    proof = arena.prove(ExactlyOneJoin(join=relation))
                    edits = _semi_join_edits(relation, node, source, dialect)
                    if proof.value and edits:
                        message = f"every row matches exactly one row, so {relation.origin.detail} filters nothing: delete it"
                        found.append(Rewrite(site, RewriteKind.REDUNDANT_FILTER, message, edits, proof))
                relation = input_
            case _:
                return found


def _semi_join_edits(join: ir.Join, node: exp.Select, source: Source, dialect: str) -> tuple[Edit, ...]:
    """Deleting the WHERE subquery or SEMI JOIN of `node` that `join` lowers."""
    where = node.args.get("where")
    conjuncts = _where_conjuncts(where, source) if where is not None else None
    for index, (conjunct, _, _) in enumerate(conjuncts or ()):
        if written_sql(conjunct, dialect) == join.origin.detail:
            assert conjuncts is not None and where is not None
            return _deleted_conjunct(source, conjuncts, index, source.tokens_of(span(where)))
    for other in node.args.get("joins") or ():
        tokens = source.tokens_of(span(other))
        if tokens is not None and written_sql(other, dialect) == join.origin.detail:
            return source.deletions([tokens])
    return ()


# Conjuncts of a WHERE or ON, as written ----------------------------------------------


def _where_conjuncts(where: exp.Where, source: Source) -> list[tuple[Expr, int, int]] | None:
    clause = source.tokens_of(span(where))
    return None if clause is None else _conjunct_tokens(where.this, clause[0] + 1, clause[1], source)


def _on_conjuncts(node: exp.Join, source: Source) -> tuple[list[tuple[Expr, int, int]] | None, tuple[int, int] | None]:
    """The ON conjuncts of a join, and the tokens of its condition."""
    tokens, on = source.tokens_of(span(node)), node.args.get("on")
    keyword = next(iter(source.top_level(tokens[0], tokens[1], TokenType.ON)), None) if tokens is not None else None
    if on is None or keyword is None or tokens is None:
        return None, None
    return _conjunct_tokens(on, keyword + 1, tokens[1], source), (keyword + 1, tokens[1])


def _conjunct_tokens(condition: Expr, first: int, last: int, source: Source) -> list[tuple[Expr, int, int]] | None:
    """Each conjunct of `condition`, written from token `first` to `last`,
    as the IR reads them (`ir.convert._conjuncts`: top-level ANDs, parentheses
    removed), with its first and last token, parentheses around it included.
    None when the tokens do not line up with the AST."""
    found: list[tuple[Expr, int, int]] = []

    def walk(node: Expr, first: int, last: int) -> bool:
        inner, start, end = node, first, last
        while isinstance(inner, exp.Paren):
            if not source.is_(start, TokenType.L_PAREN) or source.closing(start) != end:
                return False
            inner, start, end = inner.this, start + 1, end - 1
        if isinstance(inner, exp.And):
            ands = source.top_level(start, end, TokenType.AND)
            # AND is left-associative: the last one at this level splits it.
            return bool(ands) and walk(inner.this, start, ands[-1] - 1) and walk(inner.expression, ands[-1] + 1, end)
        found.append((inner, first, last))
        return first <= last

    return found if walk(condition, first, last) else None


def _deleted_conjunct(
    source: Source, conjuncts: list[tuple[Expr, int, int]], index: int, clause: tuple[int, int] | None,
) -> tuple[Edit, ...]:
    """Conjunct `index` deleted with the AND joining it to the others; the
    whole `clause` (a WHERE) when it is the only one, or, with no clause (an
    ON), the condition written as TRUE."""
    _, first, last = conjuncts[index]
    if len(conjuncts) == 1:
        return source.deletions([clause]) if clause is not None else (source.replace(first, last, "TRUE"),)
    if index == 0:
        following = next(token for token in range(last + 1, conjuncts[1][1]) if source.is_(token, TokenType.AND))
        return source.deletions([(first, following)])
    preceding = next(token for token in range(first - 1, conjuncts[index - 1][2], -1) if source.is_(token, TokenType.AND))
    return source.deletions([(preceding, last)])


def _syntax_at_sites(ast: ParsedProgram) -> dict[tuple[int, bool], exp.Join | exp.Select]:
    """Each Join and Select by its Attachment Site's (offset, trailing)."""
    index: dict[tuple[int, bool], exp.Join | exp.Select] = {}
    for node in _nodes(ast, exp.Join, exp.Select):
        position = marker_position(node)
        if position is not None:
            index[(position[0], isinstance(node, exp.Select))] = node
    return index


def _join_start(node: exp.Join, source: Source) -> int | None:
    """The join's first token."""
    position = marker_position(node)
    return None if position is None else source.at(position[0])


def _nodes(ast: ParsedProgram, *types: type[Expr]) -> Iterator:
    for statement in (*ast.create_statements, ast.root_select):
        if statement is not None:
            yield from statement.find_all(*types)


# Outer joins that cannot pad ------------------------------------------------------


def _inner_join(
    arena: Arena, site: AttachmentSite, join: ir.Join, parents: dict[ir.RelationExpr, list[ir.RelationExpr]],
    node: exp.Join, source: Source,
) -> Rewrite | None:
    """An outer join none of whose padded rows reaches the result. Its edit
    deletes the LEFT or RIGHT keyword, and OUTER.

    Only the joins above it in the same FROM and its own WHERE are searched:
    within one Select Expression nothing between them looks at a row as a
    whole, while past it a LIMIT, window, or aggregate may.
    """
    if join.kind not in {"left", "right"}:
        return None
    side = _join_start(node, source)
    if not source.is_(side, TokenType.LEFT, TokenType.RIGHT):
        return None
    assert side is not None
    last = side + 1 if source.is_(side + 1, TokenType.OUTER) else side
    if not source.is_(last + 1, TokenType.JOIN):
        return None
    edits = (source.delete(side, last),)
    keyword = f"{join.kind.upper()} JOIN"
    if join.kind == "left":
        exactly_one = arena.prove(ExactlyOneJoin(join=join))
        if exactly_one.value:
            message = f"every left row matches exactly one right row, so the {keyword} pads nothing: write JOIN"
            return Rewrite(site, RewriteKind.INNER_JOIN, message, edits, exactly_one)
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
                rejected, paddings, clause = {}, {}, ""
            case ir.Join(kind=kind):
                # A semi join (a WHERE EXISTS or IN) keeps only rows its ON holds on.
                described = "WHERE EXISTS or IN subquery" if kind == "semi" else f"later {kind.upper()} JOIN"
                rejected, paddings, clause = _on_rejected(parent), _on_rejected_paddings(parent), f"the ON condition of a {described}"
            case ir.Filter():
                rejected, paddings, clause = _null_rejected(parent.conjuncts), _rejected_paddings(parent.conjuncts), "the WHERE clause"
            case _:
                return None
        for column, conjunct in rejected.items():
            via = padded_by(column)
            if via is not None:
                how = f"is padded by the {keyword}" if via[0] is join else f"is NULL on every row the {keyword} pads"
                padded = Result(True, f"{quoted(column.name)} {how}", (join, column), chained[via])
                evidence = Result(
                    True, f"{clause} rejects NULL in {quoted(column.name)}, so every row the {keyword} pads is removed",
                    (conjunct, column), [padded],
                )
                message = f"{clause} rejects NULL in {quoted(column.name)}, so no padded row survives: write JOIN"
                return Rewrite(site, RewriteKind.INNER_JOIN, message, edits, evidence)
        for padding, conjunct in paddings.items():
            # Not TRUE on a row padded as a whole (`a.x > 0 OR a.y > 0`).
            via = (padding.join, padding.left)
            if via in chained:
                how = f"The {keyword}'s padding" if padding.join is join else f"A later join's padding, which every row the {keyword} pads gets"
                padded = Result(True, how, (join, padding.join), chained[via])
                evidence = Result(
                    True, f"{clause} is never TRUE on a padded row, so every row the {keyword} pads is removed",
                    (conjunct, padding.join), [padded],
                )
                message = f"{clause} is never TRUE on a row the {keyword} pads, so no padded row survives: write JOIN"
                return Rewrite(site, RewriteKind.INNER_JOIN, message, edits, evidence)
        if isinstance(parent, ir.Join) and parent.kind in {"left", "full"}:
            for column, conjunct in _null_rejected(parent.conjuncts).items():
                via = padded_by(column)
                # The left rows it cannot match keep a padded right side.
                if via is not None and column in parent.left.output_columns and (parent, False) not in chained:
                    reason = f"its ON rejects NULL in {quoted(column.name)}, so the later {parent.kind.upper()} JOIN pads every row the {keyword} pads"
                    chained[(parent, False)] = [*chained[via], Result(True, reason, (conjunct, column))]
        if isinstance(parent, ir.Filter):
            return None
        relation = parent
    return None


# Redundant ORDER BY keys -----------------------------------------------------------


def _redundant_sort_keys(
    site: AttachmentSite, relation: ir.RelationExpr, select: exp.Select, keys: tuple[ir.OutputColumn, ...] | None, dialect: str,
    source: Source,
) -> list[Rewrite]:
    """Each ORDER BY key the keys before it determine, NULL equal to NULL
    (Paulley §3, reducing the sort list): rows tied on the earlier keys agree
    on it, so it never breaks a tie. Only while the earlier keys compare by
    identity: a collation or a float ties values its equality tells apart.
    Silent when a key is an expression or a name that does not resolve to one
    column, and for DISTINCT ON, whose ORDER BY picks the rows it keeps. Its
    edit deletes the key and the comma before it."""
    from sqlassert.hazards import _ties_identical

    if not keys:
        return []
    items = select.args["order"].expressions
    clause = source.tokens_of(span(select.args["order"]))
    parts = source.split(clause[0] + 1, clause[1], TokenType.COMMA) if clause is not None else []
    if len(parts) != len(items):
        return []
    scope = relation.input if isinstance(relation, ir.Project) else relation
    found = []
    for position in range(1, len(keys)):
        if not all(_ties_identical(key) for key in keys[:position]):
            break
        closure = _closure(scope, frozenset(keys[:position]), frozenset())
        key = keys[position]
        if key not in closure.value.columns:
            continue
        written = [written_sql(item.this if isinstance(item, exp.Ordered) else item, dialect) for item in items]
        message = f"({quoted_all(written[:position])}) determine {quoted(written[position])}, NULL equal to NULL, so it never breaks a tie: drop it from ORDER BY"
        evidence = Result(True, "The earlier ORDER BY keys determine this one", (key,), [closure.members[key]])
        found.append(Rewrite(site, RewriteKind.REDUNDANT_SORT_KEY, message, source.deletions([(parts[position][0] - 1, parts[position][1])]), evidence))
    return found


# Redundant joins ------------------------------------------------------------------


def _redundant_join(arena: Arena, site: AttachmentSite, join: ir.Join, node: exp.Join, dialect: str, source: Source) -> Rewrite | None:
    """A join that keeps every left row once, none of whose columns is used
    outside its ON: its edit deletes it. One that only filters moves into a
    WHERE EXISTS."""
    if join.kind not in {"inner", "left"} or _right_side_used(join, node):
        return None
    tokens = source.tokens_of(span(node))
    if tokens is None:
        return None
    right = written_sql(node.this, dialect)
    unused = f"no column of {right} is used outside its ON"
    if join.kind == "left":
        unique = arena.prove(ManyToOneJoin(join=join))
        if unique.value:
            message = f"at most one match per row and {unused}: delete the join"
            return Rewrite(site, RewriteKind.REDUNDANT_JOIN, message, source.deletions([tokens]), unique)
        return None
    exactly_one = arena.prove(ExactlyOneJoin(join=join))
    if exactly_one.value:
        message = f"exactly one match per row and {unused}: delete the join"
        return Rewrite(site, RewriteKind.REDUNDANT_JOIN, message, source.deletions([tokens]), exactly_one)
    unique = arena.prove(ManyToOneJoin(join=join))
    if not unique.value or _right_or_full_join_follows(node):
        # A filter moved to WHERE would act after a later RIGHT or FULL JOIN,
        # which then no longer pads the rows this join used to drop first.
        return None
    edits = _filter_join_edits(node, source)
    if edits is None:
        return None
    message = f"at most one match per row and {unused}, so it only filters: move it into a WHERE EXISTS"
    return Rewrite(site, RewriteKind.FILTER_JOIN, message, edits, unique)


def _filter_join_edits(node: exp.Join, source: Source) -> tuple[Edit, ...] | None:
    """The join deleted and `EXISTS (SELECT 1 FROM <its relation> WHERE <its
    ON>)`, both as written, ANDed into its Select's WHERE (`_anded_into_where`)."""
    select = node.parent
    tokens = source.tokens_of(span(node))
    if not isinstance(select, exp.Select) or tokens is None:
        return None
    keyword = next(iter(source.top_level(tokens[0], tokens[1], TokenType.JOIN)), None)
    _, condition = _on_conjuncts(node, source)
    if keyword is None or condition is None and node.args.get("on") is not None:
        return None
    relation = (keyword + 1, condition[0] - 2 if condition is not None else tokens[1])
    where = f" WHERE {source.text(*condition)}" if condition is not None else ""
    exists = f"EXISTS (SELECT 1 FROM {source.text(*relation)}{where})"
    return _anded_into_where(select, source, exists, tokens, [relation] + ([condition] if condition is not None else []))


def _anded_into_where(
    select: exp.Select, source: Source, condition: str, deleted: tuple[int, int], moved: list[tuple[int, int]],
) -> tuple[Edit, ...] | None:
    """`condition` ANDed to the end of `select`'s WHERE (the WHERE's own
    condition parenthesized when it is an OR), or written as a new WHERE
    after its FROM, and the `deleted` join deleted, but for the text it
    `moved` into `condition`."""
    where = select.args.get("where")
    if where is not None:
        tokens = source.tokens_of(span(where))
        if tokens is None:
            return None
        if isinstance(where.this, exp.Or | exp.Xor):
            added = (source.insert_before(tokens[0] + 1, "("), source.insert_after(tokens[1], f") AND {condition}"))
        else:
            added = (source.insert_after(tokens[1], f" AND {condition}"),)
        return (source.delete(*deleted, moved), *added)
    last = _from_end(select, source)
    if last is None:
        return None
    if last == deleted[1]:
        return (source.replace(*deleted, f"WHERE {condition}", moved),)
    return (source.delete(*deleted, moved), source.insert_after(last, f" WHERE {condition}"))


def _from_end(select: exp.Select, source: Source) -> int | None:
    """The last token of `select`'s FROM clause, where a new WHERE goes: past
    its joins (a `, LATERAL` or `JOIN LATERAL` among them) and any Hive or
    Spark `LATERAL VIEW`, before the first clause after them (GROUP BY,
    HAVING, WINDOW, QUALIFY, ORDER BY, LIMIT, ...)."""
    end = marker_position(select)
    clauses = [
        position[0] for key, value in select.args.items()
        if key != "distinct" and isinstance(value, Expr) and (position := span(value)) is not None
    ]
    # A WINDOW clause is a list, which records no span: its keyword is found.
    if select.args.get("windows"):
        position = span(select)
        first = source.at(position[0]) if position is not None else None
        last = source.before(end[0]) if end is not None else None
        if first is None or last is None:
            return None
        clauses.extend(source.start(index) for index in source.top_level(first, last, TokenType.WINDOW))
    boundary = min(clauses, default=end[0] if end is not None else None)
    return source.before(boundary) if boundary is not None else None


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


def _within(node: Expr | None, ancestor: Expr) -> bool:
    current: Expr | None = node
    while current is not None:
        if current is ancestor:
            return True
        current = current.parent
    return False


# DISTINCT and GROUP BY ------------------------------------------------------------


def _redundant_distinct(site: AttachmentSite, distinct: ir.Distinct, node: exp.Select, source: Source) -> list[Rewrite]:
    """DISTINCT over input rows no two of which agree on the selected
    columns, NULL equal to NULL: a strict key of the input. Its edit deletes
    the keyword."""
    keyword = node.args.get("distinct")
    tokens = source.tokens_of(span(keyword)) if keyword is not None else None
    if tokens is None:
        return []
    selected = {
        column.scalar_expr.column: column.name
        for column in distinct.output_columns if isinstance(column.scalar_expr, ir.ColumnRef)
    }
    key = _strict_key(distinct.input, tuple(selected))
    if key is None:
        return []
    evidence = _reaches_row_identity(_closure(distinct.input, frozenset(key), frozenset()))
    names = quoted_all(selected[column] for column in key)
    message = f"no two rows agree on ({names}), NULL equal to NULL, so DISTINCT removes nothing: delete DISTINCT"
    return [Rewrite(site, RewriteKind.REDUNDANT_DISTINCT, message, source.deletions([tokens]), evidence)]


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


def _grouping_is_projection(
    site: AttachmentSite, aggregate: ir.Aggregate, node: exp.Select, dialect: str, source: Source,
) -> Rewrite | None:
    """A GROUP BY whose grouping columns no two input rows agree on, NULL
    equal to NULL (GROUP BY puts NULLs in one group): every group is one row,
    so the query is a projection, each aggregate its value over that row.
    An empty input gives no rows either way. Silent when a grouping
    expression is not a column, or an aggregate has no one-row form here
    (`_one_row`), or is read outside the select list (ORDER BY, QUALIFY, a
    window over it), or with a HAVING. A rewrite with caveat when an item's type or precision
    may change (`_one_row`). The edits delete GROUP BY and replace each
    aggregate call by its one-row value (`_one_row_edit`)."""
    expressions = grouping(node) or []
    outputs = aggregate.grouping_outputs
    if not outputs or len(expressions) != len(outputs) or not all(isinstance(column.scalar_expr, ir.ColumnRef) for column in outputs):
        return None
    if any(
        call.find_ancestor(exp.Select) is node and not any(_within(call, item) for item in node.expressions)
        for call in node.find_all(exp.AggFunc)
    ) or any(node.args.get(key) is not None for key in ("qualify", "having")) or any(item.find(exp.Window) for item in node.expressions):
        return None
    if any(item.find(exp.Anonymous, exp.AnonymousAggFunc) for item in node.expressions):
        # A call SQLGlot does not know may be an aggregate the analysis does
        # not model (DuckDB's mean, arbitrary, count_star): with GROUP BY
        # gone, it would aggregate the whole input.
        return None
    keys = frozenset(column.scalar_expr.column for column in outputs if isinstance(column.scalar_expr, ir.ColumnRef))
    evidence = _reaches_row_identity(_closure(aggregate.input, keys, frozenset()))
    if not evidence.value:
        return None
    types = {
        call.description: _comparison_type(argument.column).value
        for call in aggregate.calls
        if len(call.arguments) == 1 and isinstance(argument := call.arguments[0], ir.ColumnRef)
    }
    clause = source.tokens_of(span(node.args["group"])) if node.args.get("group") is not None else None
    if clause is None:
        return None
    edits = list(source.deletions([clause]))
    caveats: list[_Caveat] = []
    for item in node.expressions:
        name = quoted(item.alias if isinstance(item, exp.Alias) else written_sql(item, dialect))
        rewritten = _one_row(item, name, dialect, types)
        if rewritten is None:
            return None
        replacements, item_caveats = rewritten
        for replacement in replacements:
            edit = _one_row_edit(replacement, source, dialect)
            if edit is None:
                return None
            edits.append(edit)
        caveats.extend(item_caveats)
    names = quoted_all(written_sql(expression, dialect) for expression in expressions)
    message = (
        f"no two rows agree on ({names}), NULL equal to NULL, so every group is one row: "
        "drop GROUP BY and write each aggregate as its value over that row"
    )
    if not caveats:
        return Rewrite(site, RewriteKind.GROUPING_PROJECTION, message, tuple(edits), evidence)
    message += f". Caveat: {'; '.join(caveat.text for caveat in caveats)}"
    castable = len({caveat.column for caveat in caveats if caveat.castable})
    if castable:
        message += "; cast it if later arithmetic needs the wider type" if castable == 1 else "; cast them if later arithmetic needs the wider types"
    return Rewrite(site, RewriteKind.GROUPING_PROJECTION, message, tuple(edits), evidence, SuggestType.REWRITE_WITH_CAVEAT)


@dataclass(frozen=True)
class _OneRowValue:
    """An aggregate call's value over one row: `target`, the call or the
    IGNORE or RESPECT NULLS around it, is written as 1 (`count(*)`), as
    `CASE WHEN x IS NULL THEN 0 ELSE 1 END` (`count(x)`), or as its argument
    `argument` (none for `count(*)`). `whole`: the call is its select item's
    whole expression."""

    target: Expr
    call: exp.AggFunc
    form: Literal["one", "case", "argument"]
    whole: bool
    argument: Expr | None


def _one_row_edit(value: _OneRowValue, source: Source, dialect: str) -> Edit | None:
    """The call written as its one-row value: its argument's text, verbatim,
    in parentheses inside an expression unless it is a column, literal, call
    or CASE. The call is written `name(...)` or, as an ODBC escape, `{fn
    name(...)}`; its argument without DISTINCT or ALL before it, nor IGNORE
    or RESPECT NULLS or BigQuery's HAVING MAX or MIN after it. None when the
    text left does not parse back to the argument."""
    tokens = source.tokens_of(span(value.target) or span(value.call))
    if tokens is None:
        return None
    first, last = tokens
    name, end = first, last
    if source.is_(first, TokenType.L_BRACE) and source.tokens[first + 1].text.lower() == "fn":
        if not source.is_(last, TokenType.R_BRACE):
            return None
        name, end = first + 2, last - 1
    closing = source.closing(name + 1) if source.is_(name + 1, TokenType.L_PAREN) else None
    if closing is None or closing > end:
        return None
    if value.form == "one":
        return source.replace(first, last, "1")
    argument_first, argument_last = name + 2, closing - 1
    if source.is_(argument_first, TokenType.DISTINCT, TokenType.ALL):
        argument_first += 1
    if (
        argument_last - 1 > argument_first and source.tokens[argument_last].text.upper() == "NULLS"
        and source.tokens[argument_last - 1].text.upper() in {"IGNORE", "RESPECT"}
    ):
        argument_last -= 2
    having = source.top_level(argument_first, argument_last, TokenType.HAVING)
    if having:
        argument_last = having[0] - 1
    if argument_last < argument_first or value.argument is None:
        return None
    argument = source.text(argument_first, argument_last)
    try:
        parsed = sqlglot.parse_one(argument, read=dialect)
    except sqlglot.errors.ParseError:
        return None
    if parsed.sql(dialect=dialect) != value.argument.sql(dialect=dialect):
        return None
    moved = [(argument_first, argument_last)]
    if value.form == "case":
        return source.replace(first, last, f"CASE WHEN {argument} IS NULL THEN 0 ELSE 1 END", moved)
    atomic = isinstance(value.argument, exp.Column | exp.Literal | exp.Paren | exp.Func | exp.Case | exp.Null | exp.Boolean)
    return source.replace(first, last, argument if value.whole or atomic else f"({argument})", moved)


@dataclass(frozen=True)
class _Caveat:
    """What a one-row replacement may change in `column`, a quoted output
    name: its type or overflow (`castable`: a cast to the aggregate's wider
    type restores it), or avg's precision."""

    column: str
    text: str
    castable: bool


# Argument types whose sum and average over one row are the argument's value,
# and whose average, a DOUBLE in some dialects, holds it exactly (at most
# 32-bit integers and floats): a BIGINT, HUGEINT, UBIGINT or DECIMAL average
# may round, which the rewrite caveats.
_SUMMABLE = (exp.DataType.INTEGER_TYPES | exp.DataType.REAL_TYPES) - {
    exp.DataType.Type.BIT, exp.DataType.Type.MONEY, exp.DataType.Type.SMALLMONEY,
}
_FLOATS = {exp.DataType.Type.FLOAT, exp.DataType.Type.DOUBLE, exp.DataType.Type.UDOUBLE}
_AVERAGED_EXACTLY = {
    exp.DataType.Type.TINYINT, exp.DataType.Type.SMALLINT, exp.DataType.Type.MEDIUMINT, exp.DataType.Type.INT,
    exp.DataType.Type.UTINYINT, exp.DataType.Type.USMALLINT, exp.DataType.Type.UMEDIUMINT, exp.DataType.Type.UINT,
    exp.DataType.Type.FLOAT, exp.DataType.Type.DOUBLE,
}


def _one_row(item: Expr, name: str, dialect: str, types: dict[str, str | None]) -> tuple[list[_OneRowValue], list[_Caveat]] | None:
    """Each aggregate call in `item`, output as `name`, and its value over
    one row, and what each replacement may change in its type, overflow or
    precision; None when an aggregate has no such form here.

    count(*) is 1 and count(x) is 1 unless x is NULL, an integer literal's
    type rather than count's; sum(x) and avg(x) are x for a numeric x, of
    x's type unless x is a DOUBLE; min, max, any_value, and bool_and or
    bool_or of a BOOLEAN are x, of x's type. Precision and type are caveats,
    never a refusal:
    - a whole item changes type (`sum(x)` is x's type, not sum's);
    - inside an expression, the arithmetic runs in the narrower type and
      may overflow, or for a float round, where the aggregate's did not
      (`sum(x) + 1`);
    - avg of a type a DOUBLE does not hold exactly (`_AVERAGED_EXACTLY`) is
      x, the exact average, where avg's DOUBLE may round it.
    A value change is a refusal: sum of a BOOLEAN counts it, and avg of an
    integer inside an expression may divide as an integer (`avg(x) / 2` is
    x / 2, which truncates in PostgreSQL). So is an argument whose type is
    unknown. With or without DISTINCT, IGNORE or RESPECT NULLS; never with
    FILTER, nor a count of several values. None too when an aggregate sits
    in a subquery: it may be the subquery's own or, reading only outer
    columns, the outer query's."""
    if any(_within(call.find_ancestor(exp.Select), item) for call in item.find_all(exp.AggFunc)):
        return None
    caveats: list[_Caveat] = []
    values: list[_OneRowValue] = []
    for call in list(item.find_all(exp.AggFunc)):
        target: Expr = call
        while isinstance(target.parent, exp.IgnoreNulls | exp.RespectNulls):
            target = target.parent
        if isinstance(target.parent, exp.Filter | exp.WithinGroup) or any(value for key, value in call.args.items() if key not in {"this", "big_int"}):
            return None
        argument = call.this
        if isinstance(argument, exp.Distinct):
            if len(argument.expressions) != 1:
                return None
            argument = argument.expressions[0]
        if isinstance(argument, exp.HavingMax):
            argument = argument.this  # BigQuery's ANY_VALUE(x HAVING MAX y): x, over one row.
        if isinstance(argument, exp.Column) and isinstance(argument.this, exp.Star):
            return None  # count(t.*) counts rows.
        declared = types.get(call.sql(dialect=dialect))
        kind = exp.DataType.build(declared, dialect=dialect).this if declared is not None else None
        whole = target is item or (isinstance(item, exp.Alias) and target is item.this)
        narrowed_to = None
        form: Literal["one", "case", "argument"] = "argument"
        match call:
            case exp.Count() if argument is None or isinstance(argument, exp.Star):
                form = "one"
                narrowed_to = "an integer literal's type"
            case exp.Count():
                form = "case"
                narrowed_to = "an integer literal's type"
            case exp.Sum() | exp.Avg() if isinstance(argument, Expr) and not isinstance(argument, exp.Star | exp.Order) and kind in _SUMMABLE:
                if isinstance(call, exp.Avg) and not whole and kind in exp.DataType.INTEGER_TYPES:
                    return None
                written = written_sql(argument, dialect)
                if isinstance(call, exp.Avg) and kind not in _AVERAGED_EXACTLY:
                    caveats.append(_Caveat(name, (
                        f"{name} takes {written}'s exact value and type, where avg converts to DOUBLE in some dialects "
                        "and may lose precision (avg(9007199254740993) is 9007199254740992.0)"
                    ), castable=False))
                    if whole:
                        narrowed_to = ""
                if kind is not exp.DataType.Type.DOUBLE and narrowed_to is None:
                    narrowed_to = f"the type of {written}"
            case exp.LogicalAnd() | exp.LogicalOr() if isinstance(argument, Expr) and kind is exp.DataType.Type.BOOLEAN:
                pass
            case exp.Min() | exp.Max() | exp.AnyValue() if isinstance(argument, Expr) and not isinstance(argument, exp.Star | exp.Order):
                pass
            case _:
                return None
        aggregate = "count" if isinstance(call, exp.Count) else "sum" if isinstance(call, exp.Sum) else "avg"
        if narrowed_to and whole:
            caveats.append(_Caveat(name, f"{name} changes from {aggregate}'s result type to {narrowed_to}", castable=True))
        elif narrowed_to:
            risk = "lose precision" if kind in _FLOATS else "overflow"
            caveats.append(_Caveat(name, (
                f"{name} runs its arithmetic in {narrowed_to}, not {aggregate}'s result type, "
                f"and may {risk} where the aggregate's did not"
            ), castable=True))
        values.append(_OneRowValue(target, call, form, whole, argument if form != "one" else None))
    return values, caveats


def _redundant_grouping(
    site: AttachmentSite, selected: ir.RelationExpr, aggregate: ir.Aggregate, node: exp.Select, dialect: str, source: Source,
) -> list[Rewrite]:
    """Each grouping column the grouping columns kept so far strictly determine.
    `selected` is the relation whose output columns are `node`'s items: the
    Aggregate, or a Project over it. The edits delete it from GROUP BY, with
    a comma, and wrap its select item, if any, in any_value, named as before.

    One column always stays: grouping by nothing turns an empty input into one row.
    """
    expressions = grouping(node) or []
    group = node.args.get("group")
    if len(expressions) != len(aggregate.grouping_outputs) or not isinstance(group, exp.Group):
        return []
    clause = source.tokens_of(span(group))
    written_items = source.split(clause[0] + 1, clause[1], TokenType.COMMA) if clause is not None else []
    items = _select_items(node, source)
    if len(written_items) != len(group.expressions) or items is None:
        return []
    # Grouping outputs are lowered in GROUP BY order.
    written = {column: written_sql(expression, dialect) for column, expression in zip(aggregate.grouping_outputs, expressions)}
    kept = list(aggregate.grouping_outputs)
    found = []
    for grouped in aggregate.grouping_outputs:
        others = [other for other in kept if other is not grouped]
        if not others or not isinstance(grouped.scalar_expr, ir.ColumnRef):
            continue
        determinant = frozenset(other.scalar_expr.column for other in others if isinstance(other.scalar_expr, ir.ColumnRef))
        closure = _closure(aggregate.input, determinant, frozenset())
        source_column = grouped.scalar_expr.column
        expression = expressions[aggregate.grouping_outputs.index(grouped)]
        if source_column not in closure.value.columns or _read_past_select_list(node, expression):
            continue
        kept.remove(grouped)
        names = quoted_all(written[other] for other in others)
        alias = exp.to_identifier(grouped.name).sql(dialect=dialect)
        position = next((
            index for index, column in enumerate(selected.output_columns)
            if column is grouped or isinstance(column.scalar_expr, ir.ColumnRef) and column.scalar_expr.column is grouped
        ), None)
        drop = f"({names}) determine {quoted(written[grouped])}, NULL equal to NULL: drop {quoted(written[grouped])} from GROUP BY"
        index = next(index for index, expression in enumerate(group.expressions) if expression is expressions[aggregate.grouping_outputs.index(grouped)])
        first, last = written_items[index]
        edits = list(source.deletions([(first - 1, last) if index else (first, last + 1)]))
        if position is None:
            message = drop  # Not selected.
        else:
            item = node.expressions[position]
            expression = item.this if isinstance(item, exp.Alias) else item
            message = f"{drop} and select any_value({written_sql(expression, dialect)}) AS {alias}"
            start, end = items[position][1]
            edits += [
                source.insert_before(start, "any_value("),
                source.insert_after(end, ")" if isinstance(item, exp.Alias) else f") AS {alias}"),
            ]
        found.append(Rewrite(site, RewriteKind.REDUNDANT_GROUPING, message, tuple(edits), closure.members[source_column]))
    return found


def _read_past_select_list(node: exp.Select, expression: Expr) -> bool:
    """Whether a column `expression` reads may be read in DISTINCT ON, HAVING,
    QUALIFY or ORDER BY of `node`, where it must stay grouped: any column of
    the same name, but, in DISTINCT ON and ORDER BY, an unqualified one that
    names a select item, which they read instead."""
    names = {column.name.lower() for column in expression.find_all(exp.Column)}
    outputs = {item.alias_or_name.lower() for item in node.expressions}
    distinct = node.args.get("distinct")
    for key, clause in (("on", distinct.args.get("on") if distinct is not None else None), *(
        (key, node.args.get(key)) for key in ("having", "qualify", "order")
    )):
        for column in clause.find_all(exp.Column) if clause is not None else ():
            name = column.name.lower()
            if name in names and not (key in {"on", "order"} and not column.table and name in outputs):
                return True
    return False


def _select_items(node: exp.Select, source: Source) -> list[tuple[tuple[int, int], tuple[int, int]]] | None:
    """The tokens of each item of `node`'s select list, whole and without
    its alias; None where they cannot be told apart."""
    position = span(node)
    end = marker_position(node)
    start = source.at(position[0]) if position is not None else None
    last = source.before(end[0]) if end is not None else None
    # Its SELECT keyword: after the FROM of a DuckDB FROM-first Select, and
    # none in a bare `FROM t`, whose `*` is implied.
    keyword = next(iter(source.top_level(start, last, TokenType.SELECT)), None) if start is not None and last is not None else None
    if keyword is None or last is None:
        return None
    first = keyword + 1
    distinct = node.args.get("distinct")
    if distinct is not None:
        if not source.is_(first, TokenType.DISTINCT):
            return None
        first += 1
        if distinct.args.get("on") is not None:
            # DISTINCT ON (...): the list starts past its parenthesized keys.
            closing = source.closing(first + 1) if source.is_(first, TokenType.ON) and source.is_(first + 1, TokenType.L_PAREN) else None
            if closing is None:
                return None
            first = closing + 1
    elif source.is_(first, TokenType.ALL):
        first += 1
    # The list ends at FROM, or at the first clause after it.
    ends = [index for index in source.top_level(first, last, *_AFTER_SELECT_LIST)]
    parts = source.split(first, (ends[0] - 1) if ends else last, TokenType.COMMA)
    if len(parts) != len(node.expressions):
        return None
    items = []
    for (item_first, item_last), item in zip(parts, node.expressions):
        expression_last = item_last
        if isinstance(item, exp.Alias):
            identifier = item.args.get("alias")
            alias = source.at(identifier.meta["start"]) if isinstance(identifier, exp.Identifier) and "start" in identifier.meta else None
            if alias is None or not item_first < alias <= item_last:
                return None
            expression_last = alias - 2 if source.is_(alias - 1, TokenType.ALIAS) else alias - 1
        items.append(((item_first, item_last), (item_first, expression_last)))
    return items


# Tokens that end a select list.
_AFTER_SELECT_LIST = (
    TokenType.FROM, TokenType.WHERE, TokenType.GROUP_BY, TokenType.HAVING, TokenType.QUALIFY, TokenType.WINDOW,
    TokenType.ORDER_BY, TokenType.LIMIT, TokenType.OFFSET, TokenType.FETCH, TokenType.INTO,
    TokenType.UNION, TokenType.EXCEPT, TokenType.INTERSECT,
)


# LIMIT and ORDER BY on at most one row ----------------------------------------------


def _one_row_tail(
    site: AttachmentSite, relation: ir.RelationExpr, limit: ir.Limit | None, select: exp.Select, source: Source,
) -> list[Rewrite]:
    """A Select proved to return at most one row (the empty set is a strict
    key of it, Kossmann's early abort) needs no ORDER BY, and no LIMIT of at
    least one row. Not with OFFSET, which may skip that row, nor a LIMIT whose
    count is unknown or zero. Its edits delete the clauses."""
    sorted_ = select.args.get("order") is not None
    limited = select.args.get("limit") is not None and select.args.get("offset") is None and limit is not None and (limit.count or 0) >= 1
    if not sorted_ and not limited:
        return []
    clauses = [source.tokens_of(span(select.args[key])) for key, present in (("order", sorted_), ("limit", limited)) if present]
    if any(tokens is None for tokens in clauses):
        return []
    evidence = _reaches_row_identity(_closure(relation, frozenset(), frozenset()))
    if not evidence.value:
        return []
    dropped = " and ".join(clause for clause, present in (("ORDER BY", sorted_), ("LIMIT", limited)) if present)
    message = f"this Select returns at most one row, so {dropped} {'change' if sorted_ and limited else 'changes'} nothing: drop {'them' if sorted_ and limited else 'it'}"
    return [Rewrite(site, RewriteKind.ONE_ROW, message, source.deletions(tokens for tokens in clauses if tokens is not None), evidence)]


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


def _self_join(
    arena: Arena, site: AttachmentSite, join: ir.Join, node: exp.Join, program: ir.Program, dialect: str, source: Source,
) -> Rewrite | None:
    """`a JOIN t b ON a.k = b.k`, where `a` reads the same relation `t` and
    `k` is a non-null key of it: each row of `a` matches itself, so every
    `b.x` is `a.x` and the join goes. Other ON conjuncts move to WHERE (an
    inner join; not ahead of a RIGHT or FULL JOIN, which would then pad
    differently) or, for a LEFT JOIN, guard each column read from `b`:
    `CASE WHEN cond THEN a.x END`, NULL where the join padded. The relation
    is a table, or a view or CTE whose rows are the same each time it is read
    (`_same_rows`) and whose columns read through `b` are pure (`_pure`).
    The edits delete the join, qualify each `b.x` with `a`, and write the
    other conjuncts, as written but for `b`, in WHERE or each guard."""
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
    written, _ = _on_conjuncts(node, source)
    tokens = source.tokens_of(span(node))
    if written is None or tokens is None:
        return None
    columns = {column.name.lower(): column for column in relation.output_columns}
    extra = [(conjunct, first, last) for conjunct, first, last in written if not _key_equality(conjunct, mine, theirs, names)]
    if extra and join.kind == "inner" and _right_or_full_join_follows(node):
        return None
    if extra and join.kind == "left" and join.predicate is not None and ir.determinants(join.predicate) is None:
        return None  # A volatile condition repeated per column would draw again.
    reader = exp.to_identifier(mine).sql(dialect=dialect)
    parts = [_requalified(source, conjunct, first, last, theirs, reader, mine, dialect) for conjunct, first, last in extra]
    if any(part is None for part in parts):
        return None
    guard = " AND ".join(part for part in parts if part is not None) or None
    edits: list[Edit] = []
    for column in select.find_all(exp.Column):
        if _within(column, node):
            continue
        if column.find_ancestor(exp.Select) is not select:
            if column.table.lower() == theirs.lower():
                return None  # Read in a nested query, which this rewrite does not reach.
            continue
        if not column.table:
            if column.name.lower() in columns:
                return None  # Unqualified: it may be read through either reference.
            continue
        if column.table.lower() != theirs.lower():
            continue
        read = columns.get(column.name.lower())
        qualifier, name = _identifier_token(column.args.get("table"), source), _identifier_token(column.this, source)
        if read is None or not _pure(read, program) or qualifier is None or name is None:
            return None
        if guard is not None and join.kind == "left":
            written_name = source.text(name, name)
            value = f"CASE WHEN {guard} THEN {reader}.{written_name} END"
            edits.append(source.replace(qualifier, name, value + (f" AS {written_name}" if column.parent is select else "")))
        else:
            edits.append(source.replace(qualifier, qualifier, reader))
    moved = [(first, last) for _, first, last in extra]
    if guard is not None and join.kind == "inner":
        anded = _anded_into_where(select, source, guard, tokens, moved)
        if anded is None:
            return None
        edits.extend(anded)
    else:
        # A LEFT JOIN's guard may be copied into no column: its comments stay.
        edits.append(source.delete(*tokens))
    message = (
        f"each {mine} row matches itself on the key ({quoted_all(sorted(column.name for column in keys.values()))}), "
        f"so {theirs} is {mine}: delete the join and read {mine}'s columns"
    )
    return Rewrite(site, RewriteKind.SELF_JOIN, message, tuple(edits), key)


def _identifier_token(identifier: Expr | None, source: Source) -> int | None:
    return source.at(identifier.meta["start"]) if isinstance(identifier, exp.Identifier) and "start" in identifier.meta else None


def _requalified(source: Source, conjunct: Expr, first: int, last: int, old: str, new: str, new_name: str, dialect: str) -> str | None:
    """The conjunct, written from token `first` to `last`, reading `new`
    (written as `new`, named `new_name`) where it read `old`; `new.x =
    old.x` becomes `new.x IS NOT NULL`, all it still checks once both are
    one row."""
    if isinstance(conjunct, exp.EQ) and _key_equality(conjunct, new_name, old, {conjunct.this.name.lower()}):
        return written_sql(exp.Is(this=exp.column(conjunct.this.name, table=new_name), expression=exp.null(), negate=True), dialect)
    qualifiers = [
        _identifier_token(column.args.get("table"), source)
        for column in conjunct.find_all(exp.Column) if column.table.lower() == old.lower()
    ]
    text, cursor = "", source.start(first)
    for index in sorted(index if index is not None else -1 for index in qualifiers):
        if index < 0:
            return None
        text += source.sql[cursor:source.start(index)] + new
        cursor = source.end(index)
    return text + source.sql[cursor:source.end(last)]


def _key_equality(conjunct: Expr, mine: str, theirs: str, names: set[str]) -> bool:
    """`mine.k = theirs.k` (either way round) for a key column `k`."""
    if not isinstance(conjunct, exp.EQ) or not isinstance(conjunct.this, exp.Column) or not isinstance(conjunct.expression, exp.Column):
        return False
    first, second = conjunct.this, conjunct.expression
    tables = {first.table.lower(), second.table.lower()}
    return first.name.lower() == second.name.lower() and first.name.lower() in names and tables == {mine.lower(), theirs.lower()}


def _same_rows(relation: ir.RelationExpr, program: ir.Program) -> bool:
    """Whether reading `relation` twice gives the same rows: a table, or a body
    with no LIMIT, QUALIFY or opaque part, no volatile condition, and no
    volatile column among those that decide which rows there are (DISTINCT,
    a set operation's, GROUP BY's). Other columns are checked where read."""
    relation = relation.closed
    match relation:
        case ir.NamedRelation(role=role, body=body):
            return role is ir.RelationRole.TABLE or (body is not None and _same_rows(body, program))
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


def _implied_where(
    arena: Arena, site: AttachmentSite, relation: ir.RelationExpr, program: ir.Program, node: exp.Select, source: Source, dialect: str,
) -> list[Rewrite]:
    """The WHERE of the Select whose relation is `relation`."""
    while True:
        match relation:
            case ir.Project(input=input_) | ir.Aggregate(input=input_) | ir.Distinct(input=input_) | ir.QualifyByPartition(input=input_) | ir.Join(kind="semi" | "anti", left=input_):
                relation = input_
            case ir.Filter():
                where = node.args.get("where")
                if where is None:
                    return []
                clause = source.tokens_of(span(where))
                return _implied_conjuncts(arena, site, relation, program, "WHERE", _where_conjuncts(where, source), clause, source, dialect)
            case _:
                return []


def _implied_conjuncts(
    arena: Arena, site: AttachmentSite, relation: ir.Filter | ir.Join, program: ir.Program, clause: str,
    written_conjuncts: list[tuple[Expr, int, int]] | None, clause_tokens: tuple[int, int] | None, source: Source, dialect: str,
) -> list[Rewrite]:
    """Each conjunct TRUE on every row whose columns it reads are non-null,
    given the other conjuncts and what is known of the input: an equality
    two other equalities chain (`a = b AND b = c` give `a = c`), or both of
    whose sides are pinned to one value; a self-join's copies of one column
    (`_self_join`'s rows are the same row); `x = v` or `x IN (...)` whose
    listed values hold every value x can take (its Enumerated Domain);
    `x IS NOT NULL` where x is never NULL. It is
    deleted when those columns are non-null, else it only removes rows where
    one is NULL: write `x IS NOT NULL` instead. The edit deletes it
    (`_deleted_conjunct`: of an ON, its only conjunct becomes TRUE) or
    writes the IS NOT NULL in its place. `written_conjuncts` are the
    clause's conjuncts as written (`_conjunct_tokens`), `clause_tokens` the
    WHERE's tokens, None for an ON."""
    if isinstance(relation, ir.Join) and relation.kind not in {"inner", "left", "semi"}:
        return []
    found = []
    conjuncts = relation.conjuncts
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
        index = next((
            index for index, (node, _, _) in enumerate(written_conjuncts or ()) if written_sql(node, dialect) == written
        ), None)
        if index is None or written_conjuncts is None:
            continue
        if open_:
            written_columns = _written_columns(conjunct)
            check = " AND ".join(f"{written_columns.get(column, column.name)} IS NOT NULL" for column in open_)
            message = f"{written} is TRUE whenever its columns are non-null ({reason}), so it only removes NULLs: write {check} in {clause}"
            _, first, last = written_conjuncts[index]
            edits: tuple[Edit, ...] = (source.replace(first, last, check),)
        else:
            message = f"{written} is always TRUE here ({reason}): delete it from {clause}"
            edits = _deleted_conjunct(source, written_conjuncts, index, clause_tokens)
        found.append(Rewrite(site, RewriteKind.IMPLIED_CONDITION, message, edits, Result(True, reason, (conjunct,))))
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
        case ir.IsNotNull(operand=ir.ColumnRef(column=column)):
            return (f"{quoted(column.name)} is never NULL", False) if arena.prove(NonNullColumn(column=column)).value else None
        case ir.Equality(left=ir.ColumnRef(column=first), right=ir.ColumnRef(column=second)):
            if _chained(first, second, others):
                return "the other equalities chain its sides", False
            pinned = _pinned(first, others), _pinned(second, others)
            comparable = _same_type(first, second) and _identity_comparison(first).value and _identity_comparison(second).value
            if comparable and pinned[0] is not None and pinned[0] == pinned[1]:
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
    return f"{quoted(column.name)} is always one of the listed values", False


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
            case ir.Equality(left=ir.ColumnRef(column=pinned), right=ir.Constant(value=value) as literal) | ir.Equality(left=ir.Constant(value=value) as literal, right=ir.ColumnRef(column=pinned)) if pinned is column and value is not None and _pins(column, literal):
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


def _set_operation_all(site: AttachmentSite, operation: ir.SetOperation, source: Source) -> Rewrite | None:
    """`A EXCEPT B` keeps each distinct row of A that B lacks; `EXCEPT ALL`
    keeps it as often as A has it more than B. When A has no two rows alike,
    NULL equal to NULL as a set operation compares them, both keep each such
    row once. `INTERSECT ALL` keeps a row as often as the input with fewer of
    it, so a duplicate-free input on either side suffices. Its edit writes
    ALL after the operator, or in place of DISTINCT."""
    if not operation.distinct or operation.operator == "union" or not _same_set_types(operation, operation.output_columns):
        return None
    # The site follows the left input's last Select; the operator comes next.
    operator = source.after(site.offset)
    while operator is not None and source.is_(operator, TokenType.R_PAREN):
        operator += 1
    if operator is None or not source.is_(operator, TokenType.EXCEPT, TokenType.INTERSECT):
        return None
    if source.is_(operator + 1, TokenType.DISTINCT):
        edits = (source.replace(operator + 1, operator + 1, "ALL"),)
    else:
        edits = (source.insert_after(operator, " ALL"),)
    keyword = operation.operator.upper()
    sides = [("left", operation.left)] + ([("right", operation.right)] if operation.operator == "intersect" else [])
    for side, relation in sides:
        evidence = _reaches_row_identity(_closure(relation, frozenset(relation.output_columns), frozenset()))
        if evidence.value:
            message = f"no two rows of the {side} input are alike, NULL equal to NULL, so {keyword} removes no duplicates: write {keyword} ALL"
            return Rewrite(site, RewriteKind.SET_OPERATION_ALL, message, edits, evidence)
    return None


# Unused CTEs ----------------------------------------------------------------------


def _unused_ctes(ast: ParsedProgram, source: Source) -> list[Rewrite]:
    """CTEs no query of their WITH reaches, directly or through a used CTE.
    Each one's edit deletes it (`_cte_tokens`); each is a Rewrite of its own,
    so a dead CTE another dead one reads is deleted without it.

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
            tokens = _cte_tokens(source, with_, cte)
            if name in live or position is None or tokens is None:
                continue
            offset, line = position
            site = AttachmentSite(f"CTE {cte.alias_or_name}", line, offset, True)
            evidence = Result(True, f"No query of its WITH clause references CTE {cte.alias_or_name}")
            message = f"CTE {cte.alias_or_name} is never referenced, so it never runs and its markers go unchecked: delete it"
            found.append(Rewrite(site, RewriteKind.UNUSED_CTE, message, source.deletions([tokens]), evidence))
    return found


def _cte_tokens(source: Source, with_: exp.With, cte: exp.CTE) -> tuple[int, int] | None:
    """The tokens deleting `cte` deletes: `name AS (...)` and the comma
    after it (before it, for the last), or the whole WITH for its only CTE."""
    alias = cte.args.get("alias")
    identifier = alias.this if isinstance(alias, exp.TableAlias) else None
    name = source.at(identifier.meta["start"]) if isinstance(identifier, exp.Identifier) and "start" in identifier.meta else None
    if name is None:
        return None
    keyword = next(iter(source.top_level(name, len(source.tokens) - 1, TokenType.ALIAS)), None)
    opening = None if keyword is None else next(
        (index for index in range(keyword + 1, len(source.tokens)) if source.is_(index, TokenType.L_PAREN)), None
    )
    closing = source.closing(opening) if opening is not None else None
    if closing is None:
        return None
    ctes = list(with_.expressions)
    if len(ctes) == 1:
        first = name - 1 if source.is_(name - 1, TokenType.RECURSIVE) else name
        return (first - 1, closing) if source.is_(first - 1, TokenType.WITH) else None
    if ctes[0] is cte:
        return (name, closing + 1) if source.is_(closing + 1, TokenType.COMMA) else None
    return (name - 1, closing) if source.is_(name - 1, TokenType.COMMA) else None


def _table_names(node: Expr, skip: Expr | None = None) -> set[str]:
    return {table.name.lower() for table in node.find_all(exp.Table) if skip is None or not _within(table, skip)}
