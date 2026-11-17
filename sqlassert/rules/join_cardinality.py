"""Join cardinality: whether a join can repeat rows of its left input, its right input, or both.

A row of one input appears once per matching row of the other, or once padded,
whatever the join kind. Matching rows satisfy ON, so they agree on every
column an ON equality binds, all non-null: to a constant or to a pure function
of the first row's columns (a column, `-b.x`, `b.x + 1`, `CAST(b.ts AS DATE)`),
which is one value for that row, or, for the one column an exact injective
function of it is of (`-a.x`, `a.x + 1`), to such a value. Neither operand
needs to be injective to bind the other's column; the comparison must lose
nothing (`exact_comparison`). IS NOT DISTINCT FROM binds them too, but matches NULL to
NULL, so those columns are non-null only where proved, or where the other side
is a literal or an expression of non-null columns that is never NULL for them
(`ir.total`), which no NULL equals. When those columns reach the
matched input's Row Identity, a row matches at most one row and is never
repeated. A cross join is an ON with no
conjuncts: the other input must have at most one row. A semi or anti join
keeps or drops left rows whole and outputs no right row; its left rows are
never repeated (`many_to_one_join`), and no cardinality marker applies to it.
A table function or UNNEST that reads the left input is evaluated once per
left row, so none of its rows is repeated (`Join.lateral`, `one_to_many_join`).
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Literal

from sqlassert import ir
from sqlassert.python_engine import Result, Stratum, all_of, any_of, analysis, cache, current_arena
from sqlassert.rules.column_set import Attribute, ColumnSet
from sqlassert.rules.nullability import non_null_column
from sqlassert.rules.uniqueness import _closure, _key_gaps, _reaches_row_identity
from sqlassert.rules.value_typing import _comparison_type, _identity_comparison, exact_comparison, exact_numeric

type Side = Literal["left", "right"]


@analysis(Stratum.UNIQUE_JOIN)
def many_to_one_join(join: ir.Join) -> Result[bool]:
    if join.scalar_subquery:
        return Result(True, "A scalar subquery returns at most one row for each outer row, or the query fails", (join,))
    return _at_most_one_match(join, "right")


@analysis(Stratum.UNIQUE_JOIN)
def one_to_many_join(join: ir.Join) -> Result[bool]:
    if join.lateral:
        return Result(True, "A table function reading the left input is evaluated once per left row, so each of its rows has one left row", (join,))
    return _at_most_one_match(join, "left")


@analysis(Stratum.UNIQUE_JOIN)
def one_to_one_join(join: ir.Join) -> Result[bool]:
    return all_of((many_to_one_join(join), one_to_many_join(join)))


@analysis(Stratum.UNIQUE_JOIN)
def unique_join(join: ir.Join) -> Result[bool]:
    # Not many to many: the proved alternative names the input that is unique.
    return any_of((many_to_one_join(join), one_to_many_join(join)))


def _at_most_one_match(join: ir.Join, matched: Side) -> Result[bool]:
    """Each row of the other input matches at most one row of `matched`."""
    relation = join.right if matched == "right" else join.left
    cols = _bound_columns(join, matched)
    bound = frozenset(cols.value.columns)
    reached = _reaches_row_identity(_closure(relation, bound, frozenset(cols.value.definite)))
    reason = "Right columns bound for each left row" if matched == "right" else "Left columns bound for each right row"
    return all_of((Result(True, reason, (join,), list(cols.members.values())), reached))


@dataclass(eq=False)
class BoundColumns(ColumnSet):
    """`columns` bound for each row of the other input; `definite` those of
    them non-null on every matched row: bound by `=`, not only by IS NOT
    DISTINCT FROM, which also matches NULL to NULL."""

    definite: set[Attribute] = field(default_factory=set)
    # The ON equalities that bound a column.
    equalities: set[ir.ScalarExpr] = field(default_factory=set)


@analysis(Stratum.UNIQUE_JOIN)
def _bound_columns(join: ir.Join, matched: Side) -> Result[BoundColumns]:
    """The columns of `matched` an ON equality (or IS NOT DISTINCT FROM)
    binds for each row of the other input.

    One operand is a constant or a pure function of that row's columns: a
    column, `-b.x`, `b.x + 1`, `CAST(b.ts AS DATE)`; for one row it is one
    value. The other fixes a column of `matched`: the column itself, or the one
    column an injective function of it (`-a.x`, `a.x + 1`) is of, since that
    function takes that value at one column value only. Neither needs the
    other to be injective, but the comparison must lose nothing (`exact_comparison`).
    """
    relation, other = (join.right, join.left) if matched == "right" else (join.left, join.right)
    members = {}
    definite = set()
    equalities = set()
    for equality in join.conjuncts:
        if not isinstance(equality, ir.Equality | ir.NullSafeEquality):
            continue
        for bound, value in ((equality.left, equality.right), (equality.right, equality.left)):
            if bound is value:
                continue
            column = _fixed_column(bound, relation)
            inputs = ir.determinants(value)
            if column is None or inputs is None or not all(read in other.output_columns for read in inputs):
                continue
            # An equality that casts pins nothing: '01' and '1' both equal 1.
            if not exact_comparison(bound, value).value:
                continue

            evidence = Result(True, "Equality fixes the matched column for each row of the other input", (equality, column, value))
            members[column] = members[column] | evidence if column in members else evidence
            equalities.add(equality)
            # IS NOT DISTINCT FROM a value that is never NULL is `=`.
            if isinstance(equality, ir.Equality) or (ir.total(value) and all(non_null_column(read).value for read in inputs)):
                definite.add(column)
                if not isinstance(equality, ir.Equality):
                    members[column] = all_of((members[column], *(non_null_column(read) for read in inputs)))

    return Result(BoundColumns(relation, set(members), definite, equalities), "Bound columns", members=members)


def _fixed_column(expression: ir.ScalarExpr, relation: ir.RelationExpr) -> ir.OutputColumn | None:
    """The column of `relation` that `expression` taking one value fixes: the
    column itself, or the one an exact, injective function of it is of
    (`ir.injective_input`; the value of `-x` or `x + 1` has one `x`)."""
    column = ir.injective_input(expression)
    if column is None or column not in relation.output_columns:
        return None
    return column if isinstance(expression.closed, ir.ColumnRef) or exact_numeric(column).value else None


# Why a join has no proved Join Cardinality --------------------------------

type UnprovedReason = Literal["unsupported", "unconstrained", "unresolved-key", "untyped-key", "expression-key", "no-unique-set"]
type TypeLoss = Literal["values", "computed", "opaque-relation", "set-operation", "table", "type-mismatch"]
type NoUniqueSet = Literal["cascade", "partial-key", "undeclared-key"]
type _Operand = Literal["left", "right", "constant", "opaque", "both"]


@dataclass(frozen=True)
class UnprovedJoin:
    """Why neither `many_to_one_join` nor `one_to_many_join` is proved for a
    join: the first reason that applies, in the order of `UnprovedReason`.

    - `unsupported`: an input is a source sqlassert does not model (LATERAL,
      or a table function or UNNEST whose columns the dialect does not fix),
      or the join is not reachable from the program: it sits in a SELECT that
      lowered opaque.
    - `unconstrained`: no ON equality relates one input to the other or to a
      constant (a cross join, a range join). No Unique Set can make it a
      Unique Join; whether rows repeat depends on the data, not on a key.
    - `unresolved-key`: an operand of such an equality lowered opaque: a
      column that does not resolve in its input, or an expression sqlassert
      does not model.
    - `untyped-key`: a column = column equality binds nothing, since the two
      columns do not compare without a lossy coercion; `detail` names where
      the type is lost (`TypeLoss`).
    - `expression-key`: every such equality compares an expression, and none
      binds a column (`_bound_columns`): the expression is not deterministic,
      or has no type that compares without a lossy coercion (`exact_comparison`), or the
      matched side is not an injective function of one exact column. `a.k =
      b.k * 2` is one, `a.k = b.k + 1` is not.
    - `no-unique-set`: the equalities bind columns that reach the Row
      Identity of neither input. `detail` says who can act, the first that
      applies (`NoUniqueSet`):
      `cascade`: something below an input is itself unproved or unsupported
      (an upstream join with no proved Join Cardinality, through which no
      Unique Set passes, or an unsupported construct), so fixing that may fix
      this join; `construct` names the unsupported one, if any.
      `partial-key`: the equalities bind a strict subset of a known Unique
      Set of an input and every equality written binds a column; the rest of
      ON is a range or inequality, so the join is as constrained as written.
      `undeclared-key`: the bound side has no known Unique Set covering the
      bound columns, a key the project could declare.

    `construct` names the construct sqlassert does not support, when one
    is behind the reason: the nearest one at or under the input, or the
    relation, the reason points at; for a join in a SELECT that lowered
    opaque, the one that made it so (`IrParser._name_orphan_joins`).

    A diagnostic about a missing proof: it never changes what is proved."""

    reason: UnprovedReason
    detail: TypeLoss | NoUniqueSet | None = None
    construct: str | None = None


def unproved_join(join: ir.Join) -> UnprovedJoin | None:
    """Why `join` has no proved Join Cardinality; None when it has one or is
    a semi or anti join, which has none. Runs inside an analysis session."""
    if join.kind in {"semi", "anti"}:
        return None
    if join not in _reachable():
        return UnprovedJoin("unsupported", construct=_unsupported_under(join))
    if many_to_one_join(join).value or one_to_many_join(join).value:
        return None
    for relation in (join.left, join.right):
        source = relation
        while isinstance(source, ir.Alias):
            source = source.source
        if isinstance(source, ir.OpaqueRelation) and (construct := _unsupported_under(source)) is not None:
            return UnprovedJoin("unsupported", construct=construct)
    pairs = []
    related = []
    for equality in join.conjuncts:
        if not isinstance(equality, ir.Equality | ir.NullSafeEquality):
            continue
        sides = (_operand(join, equality.left), _operand(join, equality.right))
        if "both" not in sides and (sides[0] != sides[1] or sides[0] == "opaque"):
            pairs.append((equality.left.closed, equality.right.closed, sides))
            related.append(equality)
    if not pairs:
        return UnprovedJoin("unconstrained")
    if any("opaque" in sides for _, _, sides in pairs):
        return UnprovedJoin("unresolved-key", construct=_unsupported_under(join.left) or _unsupported_under(join.right))
    for first, second, _ in pairs:
        if isinstance(first, ir.ColumnRef) and isinstance(second, ir.ColumnRef) and not exact_comparison(first, second).value:
            for column in (first.column, second.column):
                if (loss := _type_loss(column)) is not None:
                    return UnprovedJoin("untyped-key", loss[0], _unsupported_under(loss[1]) if loss[0] == "opaque-relation" else None)
            return UnprovedJoin("untyped-key", "type-mismatch")
    if all(not isinstance(first, ir.ColumnRef | ir.Constant) or not isinstance(second, ir.ColumnRef | ir.Constant) for first, second, _ in pairs) and not any(
        _bound_columns(join, matched).value.columns for matched in ("left", "right")
    ):
        return UnprovedJoin("expression-key")
    return _no_unique_set(join, related)


def _no_unique_set(join: ir.Join, related: list[ir.ScalarExpr]) -> UnprovedJoin:
    """Who can act on a join whose bound columns reach no Row Identity, from
    the IR and analyses it already ran: one walk of each input's tree, and
    the closures `_at_most_one_match` took (`related`: its ON equalities
    that relate the inputs or an input to a constant)."""
    unproved, constructs = _cascade(join)
    if unproved or any(constructs):
        return UnprovedJoin("no-unique-set", "cascade", next(filter(None, constructs), None))
    for matched in ("right", "left"):
        cols = _bound_columns(join, matched)
        if not all(equality in cols.value.equalities for equality in related):
            continue  # An equality binds nothing here: not as constrained as written.
        relation = join.right if matched == "right" else join.left
        closure = _closure(relation, frozenset(cols.value.columns), frozenset(cols.value.definite))
        if any(gap.reached for gap in _key_gaps(closure)):
            return UnprovedJoin("no-unique-set", "partial-key")
    return UnprovedJoin("no-unique-set", "undeclared-key")


def _cascade(join: ir.Join) -> tuple[bool, tuple[str | None, str | None]]:
    """Whether a join below an input of `join` has no proved Join Cardinality
    (a semi or anti join has none to prove), and the unsupported construct
    nearest each input, in one walk of the two trees."""
    unsupported = {id(node): construct for node, construct in current_arena().program.unsupported}
    unproved = False
    constructs = []
    for relation in (join.left, join.right):
        nearest = None
        for node in _tree(relation):
            nearest = nearest or unsupported.get(id(node))
            if isinstance(node, ir.Join) and node.kind not in {"semi", "anti"}:
                unproved |= not (many_to_one_join(node).value or one_to_many_join(node).value)
        constructs.append(nearest)
    return unproved, (constructs[0], constructs[1])


def _tree(relation: ir.RelationExpr) -> Iterator[ir.RelationExpr]:
    """`relation` and every relation it reads, nearest first, each once."""
    pending, seen = [relation], set()
    while pending:
        node = pending.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        pending.extend(child for child in ir.children(node) if isinstance(child, ir.RelationExpr))


@cache
def _reachable() -> frozenset[ir.Node]:
    return frozenset(current_arena().program.nodes())


def _unsupported_under(relation: ir.RelationExpr) -> str | None:
    """The construct sqlassert does not support nearest to `relation`: its
    own, else the first found reading down through its inputs."""
    unsupported = {id(node): construct for node, construct in current_arena().program.unsupported}
    return next((unsupported[id(node)] for node in _tree(relation) if id(node) in unsupported), None) if unsupported else None


def _operand(join: ir.Join, expression: ir.ScalarExpr) -> _Operand:
    """Which input an equality operand reads: `opaque` when nothing is known
    of it, `constant` when it reads neither (an outer column is fixed for
    the join too)."""
    sides = set()
    pending = [expression]
    while pending:
        match node := pending.pop():
            case ir.OpaqueExpression():
                return "opaque"
            case ir.ColumnRef(column=column):
                if column in join.left.output_columns:
                    sides.add("left")
                elif column in join.right.output_columns:
                    sides.add("right")
            case _:
                pending.extend(child for child in ir.children(node) if isinstance(child, ir.ScalarExpr))
    match sorted(sides):
        case []:
            return "constant"
        case ["left"]:
            return "left"
        case ["right"]:
            return "right"
        case _:
            return "both"


def _type_loss(column: ir.OutputColumn) -> tuple[TypeLoss, ir.RelationExpr] | None:
    """Where `column` loses the type equality needs to bind it, following
    plain column references (and set operation arms, and a recursive
    reference's base case) down to the first column that is not one: `table` for a table column with no declared
    type (no CREATE, or none in the catalog), else the kind of relation or
    expression that computes it without a type compared by identity. None
    when that column keeps its type. With the relation that column is of."""
    relation = column.get_relation(current_arena().program)
    if isinstance(relation, ir.SetOperation):
        arms = relation.arms(column)
        if arms is None:
            return "set-operation", relation
        return _type_loss(arms[0]) or _type_loss(arms[1])
    if isinstance(relation, ir.RecursiveRelation) and relation.typed_by and (base := relation.typed_by[relation.output_columns.index(column)]):
        return _type_loss(base)
    match column.scalar_expr:
        case ir.ColumnRef(column=source) | ir.AnyAggregate(input=ir.ColumnRef(column=source)):
            return _type_loss(source)
    typed = _comparison_type(column).value is not None
    if isinstance(relation, ir.NamedRelation) and relation.role is ir.RelationRole.TABLE:
        # A declared type that does not compare by identity is the type, not a loss.
        return None if typed else ("table", relation)
    if typed and _identity_comparison(column).value:
        return None
    if isinstance(relation, ir.ValuesList):
        return "values", relation
    if isinstance(relation, ir.OpaqueRelation) or isinstance(column.scalar_expr, ir.OpaqueExpression):
        return "opaque-relation", relation
    return "computed", relation
