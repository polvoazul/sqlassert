"""Framework-independent relational intermediate representation.

The IR is an immutable semantic object graph. Nodes refer directly to other
nodes and carry no identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import TYPE_CHECKING, Literal, assert_never, cast, dataclass_transform

from sqlassert.provenance import AttachmentSite, Origin

if TYPE_CHECKING:
    from sqlassert.properties import Property

type JoinKind = Literal["inner", "cross", "left", "right", "full", "semi", "anti"]
type SetOperator = Literal["union", "intersect", "except"]


@dataclass_transform(eq_default=False, frozen_default=True, kw_only_default=True)
class NodeMeta(type):
    """Turn every Node subclass into the same kind of semantic value."""

    _subclasses: list

    def __new__(metaclass, name, bases, namespace, *, abstract: bool = False):
        node_type = super().__new__(metaclass, name, bases, namespace)
        node_type = dataclass(frozen=True, eq=False, kw_only=True)(node_type)
        node_type.__ir_abstract__ = abstract
        node_type._subclasses = []
        if not abstract:
            for base in node_type.__mro__[1:]:
                match base:
                    case NodeMeta():
                        base._subclasses.append(node_type)
        return node_type

    def __call__(cls, *args, **kwargs):
        if getattr(cls, "__ir_abstract__", False):
            raise TypeError(f"cannot construct abstract IR node {cls.__name__}")
        return super().__call__(*args, **kwargs)


class Node(metaclass=NodeMeta, abstract=True):
    """An immutable semantic node with object-identity equality."""

    origin: Origin


class ScalarExpr(Node, abstract=True):
    """A scalar expression produced or consumed by a relational operation."""

    @property
    def closed(self) -> AnyScalar:
        """This node as one of the concrete ScalarExprs, to match exhaustively."""
        return cast(AnyScalar, self)


class OpaqueExpression(ScalarExpr):
    """An expression nothing is known about: not even which columns its value
    depends on. A user-defined or volatile function, an aggregate or window,
    a subquery, or a column that does not resolve."""

    description: str


@dataclass(frozen=True)
class Builtin:
    """A built-in operator or function whose behaviour is modelled: a pure
    function of its arguments' values, one flag of each kind per parameter.

    A `strict` parameter makes the value NULL whenever its argument is NULL.
    The value is non-null whenever every `total` parameter's argument is
    non-null: all False means never NULL, and `total` None means it may be NULL
    even then (`n % 0` in DuckDB). Unless `total` is None, a strict parameter
    is total.

    An `injective` one is one-to-one in its single argument that is not an
    integer or decimal literal, when that argument is an integer or decimal:
    `a + 1`, `10 - a`, `-a`. Only where exact arithmetic raises an error on
    overflow instead of wrapping, saturating, or turning to a float.

    A `rejecting` parameter makes the value never TRUE (FALSE or NULL)
    whenever its argument is NULL, though perhaps not NULL: `x > ANY (...)`
    is FALSE over no rows. A strict parameter is one too, so only one that
    is not strict needs the flag; `()` when none does.

    With `true_only_if_argument` the value is TRUE only where its one
    argument is TRUE (`p IS TRUE`), so as a condition it rejects whatever
    that argument would.

    With `skips_null` the value is non-null whenever any argument is
    (`greatest` where it ignores NULLs), and `total` is None.
    """

    name: str
    strict: tuple[bool, ...]
    total: tuple[bool, ...] | None
    injective: bool
    rejecting: tuple[bool, ...] = field(default=(), kw_only=True)
    skips_null: bool = field(default=False, kw_only=True)
    true_only_if_argument: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        assert self.total is None or len(self.total) == len(self.strict), f"{self.name}: one total flag per parameter"
        assert not self.rejecting or len(self.rejecting) == len(self.strict), f"{self.name}: one rejecting flag per parameter"
        assert not self.skips_null or self.total is None, f"{self.name}: skipping NULLs, it is not total in each argument"
        assert not self.true_only_if_argument or len(self.strict) == 1, f"{self.name}: TRUE only if its one argument is"
        assert self.total is None or all(total for strict, total in zip(self.strict, self.total) if strict), (
            f"{self.name}: a strict parameter is total"
        )

    def accepts(self, count: int) -> bool:
        return count == len(self.strict)

    def strict_flags(self, count: int) -> tuple[bool, ...]:
        return self.strict

    def total_flags(self, count: int) -> tuple[bool, ...] | None:
        return self.total


@dataclass(frozen=True)
class BuiltinVariadic(Builtin):
    """A Builtin whose last parameter repeats any number of times (none
    included), each repetition with the `repeated_` flags:
    `concat_ws(sep, value, ...)` is strict and total in `sep` alone."""

    repeated_strict: bool
    repeated_total: bool

    def __post_init__(self) -> None:
        super().__post_init__()
        assert not self.injective, f"{self.name}: a variadic Builtin is not injective"
        assert self.total is None or self.repeated_total or not self.repeated_strict, f"{self.name}: a strict parameter is total"

    def accepts(self, count: int) -> bool:
        return count >= len(self.strict)

    def strict_flags(self, count: int) -> tuple[bool, ...]:
        return self.strict + (self.repeated_strict,) * (count - len(self.strict))

    def total_flags(self, count: int) -> tuple[bool, ...] | None:
        return None if self.total is None else self.total + (self.repeated_total,) * (count - len(self.total))


class Call(ScalarExpr):
    """A Builtin applied to arguments: `n + 1`, `n > 0`, `CAST(n AS BIGINT)`, `NOT p`."""

    function: Builtin
    arguments: tuple[ScalarExpr, ...]
    description: str

    def __post_init__(self) -> None:
        assert self.function.accepts(len(self.arguments)), f"{self.function.name} takes no {len(self.arguments)} arguments"

    @property
    def strict_arguments(self) -> tuple[ScalarExpr, ...]:
        """The arguments whose NULL makes the value NULL."""
        flags = self.function.strict_flags(len(self.arguments))
        return tuple(argument for argument, strict in zip(self.arguments, flags) if strict)

    @property
    def rejecting_arguments(self) -> tuple[ScalarExpr, ...]:
        """The arguments whose NULL makes the value never TRUE: the strict ones
        and the `rejecting` ones."""
        flags = self.function.strict_flags(len(self.arguments))
        rejecting = self.function.rejecting or (False,) * len(flags)
        return tuple(argument for argument, strict, rejects in zip(self.arguments, flags, rejecting) if strict or rejects)

    @property
    def total_arguments(self) -> tuple[ScalarExpr, ...] | None:
        """The arguments whose being non-null makes the value non-null (none:
        never NULL), or None when the value may be NULL regardless."""
        flags = self.function.total_flags(len(self.arguments))
        return None if flags is None else tuple(argument for argument, total in zip(self.arguments, flags) if total)

    @property
    def injective_argument(self) -> ScalarExpr | None:
        """The argument the value is one-to-one in, when it is an integer or
        decimal: the single one that is not an integer or decimal literal."""
        if not self.function.injective:
            return None
        others = [argument for argument in self.arguments if not _exact_literal(argument)]
        return others[0] if len(others) == 1 else None


class OpaquePureFunction(ScalarExpr):
    """A built-in function or operator whose behaviour is not modelled: a pure
    function of its arguments, and nothing else is known about it. Searched
    CASE, and `concat`, `||` or `greatest` where the dialect's is not modelled."""

    arguments: tuple[ScalarExpr, ...]
    description: str


class Count(ScalarExpr):
    """`count(*)` or `count(...)`: never NULL, even over no rows; its value is
    otherwise opaque."""

    description: str


@dataclass(frozen=True)
class LiteralValue:
    """A non-null SQL literal's value: its SQL type and canonical text.

    `type` is "string", "numeric", or "boolean". Two values are the same only
    when both match, so numeric 1 never collides with string '1'. A string's
    text is its contents; a boolean's is TRUE or FALSE. A numeric literal's
    text is its spelling, which carries its SQL type: an integer is written
    without sign or leading zeros (`+007` is `7`), while a decimal keeps its
    scale (`1.0` is not `1`) and an exponent form stays a double (`1e0` is
    not `1`). Those differ in how a column is compared with them, so only
    identical literals are the same value; whether distinct ones can equal one
    value is a separate, conservative question (`rules/domain.py`).
    """

    type: Literal["string", "numeric", "boolean"]
    text: str

    def sql(self) -> str:
        match self.type:
            case "string":
                return "'" + self.text.replace("'", "''") + "'"
            case "numeric" | "boolean":
                return self.text
            case _:
                assert_never(self.type)


class Constant(ScalarExpr):
    """A non-null literal; `value` is None while its type is not modelled."""

    value: LiteralValue | None = None


class NullLiteral(ScalarExpr):
    """The NULL literal, kept apart from Constant so it is never taken as non-null."""


class Coalesce(ScalarExpr):
    """The first non-null argument: `coalesce`, `ifnull`, and `nvl`."""

    arguments: tuple[ScalarExpr, ...]


class SimpleCase(ScalarExpr):
    """`CASE operand WHEN whens[i] THEN thens[i] ... ELSE default END`.

    Each arm compares `operand = whens[i]`; a NULL operand matches none. A
    missing ELSE is lowered as a NULL literal `default`.
    """

    operand: ScalarExpr
    whens: tuple[ScalarExpr, ...]
    thens: tuple[ScalarExpr, ...]
    default: ScalarExpr


class AnyAggregate(ScalarExpr):
    """An arbitrary value selected from the input rows of an aggregate group:
    a column outside every aggregate that the grouping determines (constant
    across the group), or `min` and `max`, which take one of its non-null values.

    `empty` marks a group that may have no rows, where the value is NULL: an
    aggregate grouped by nothing has one row even over no input rows, and one
    with FILTER may see none of its group's rows."""

    input: ScalarExpr
    empty: bool = False


class Accumulation(ScalarExpr):
    """An aggregate of a group's values of `input` that skips NULLs: `sum`
    and `avg` where arithmetic raises rather than returning NULL. NULL only
    when no value is non-null, or when the group is `empty` (see
    AnyAggregate); its value is otherwise opaque."""

    input: ScalarExpr
    empty: bool = False


class Elements(ScalarExpr):
    """Every element of a composite `operand` (an ARRAY's elements, a ROW's
    fields, a MAP's values, nested ones included) as one value, non-null when
    each element is. Not a value SQL computes: a comparison of composites
    takes one per composite operand as a non-strict argument, since
    `ARRAY[1, NULL] <> ARRAY[1, NULL]` and `ROW(1, NULL) = ROW(1, NULL)` may be NULL."""

    operand: ScalarExpr


class ColumnRef(ScalarExpr):
    """A direct reference to an upstream relation expression's output."""

    column: OutputColumn


class Equality(ScalarExpr):
    """`left = right`. As a conjunct it holds only on rows where both sides are non-null.

    `total` marks one that is non-null whenever both sides are: where a failed
    implicit cast raises (`DialectSemantics.casts_raise`) and neither side is
    a composite, which may hold a NULL element (`ROW(1, NULL) = ROW(1, NULL)`
    is NULL)."""

    left: ScalarExpr
    right: ScalarExpr
    total: bool = False


class NullSafeEquality(ScalarExpr):
    """`left IS NOT DISTINCT FROM right`: never NULL, TRUE when both sides
    are equal or both NULL.

    `rejects` marks one where a non-null side stays non-null as it is
    compared, so a TRUE comparison makes the other side non-null too: where a
    failed implicit cast raises (in Hive `n <=> 'x'` is
    `n IS NULL`) and neither side is a composite."""

    left: ScalarExpr
    right: ScalarExpr
    rejects: bool = False


class InList(ScalarExpr):
    """`operand IN (values...)` over a list of values, not a subquery."""

    operand: ScalarExpr
    values: tuple[ScalarExpr, ...]


class IsNull(ScalarExpr):
    """`operand IS NULL`."""

    operand: ScalarExpr


class IsNotNull(ScalarExpr):
    """`operand IS NOT NULL`."""

    operand: ScalarExpr


class And(ScalarExpr):
    """`p AND q ...`, flattened: TRUE when every part is."""

    parts: tuple[ScalarExpr, ...]


class Or(ScalarExpr):
    """`p OR q ...`, flattened: TRUE when some part is."""

    parts: tuple[ScalarExpr, ...]


def conjuncts(predicate: ScalarExpr | None) -> tuple[ScalarExpr, ...]:
    """The parts of `predicate`'s top-level AND; none for no predicate."""
    match predicate:
        case None:
            return ()
        case And(parts=parts):
            return parts
        case _:
            return (predicate,)


def determinants(expression: ScalarExpr) -> tuple[OutputColumn, ...] | None:
    """The columns `expression` is a pure function of, in order of first use,
    or None when it is not one of any: its value may depend on other rows, on
    state, or on nothing known (Paulley §3.1.4)."""
    expression = expression.closed
    match expression:
        case ColumnRef(column=column):
            return (column,)
        case OpaqueExpression() | Count() | AnyAggregate() | Accumulation():
            return None
        case (
            Constant() | NullLiteral() | Call() | OpaquePureFunction() | Coalesce() | SimpleCase() | Elements() | Equality()
            | NullSafeEquality() | InList() | IsNull() | IsNotNull() | And() | Or()
        ):
            found: dict[OutputColumn, None] = {}
            for child in children(expression):
                assert isinstance(child, ScalarExpr)
                inputs = determinants(child)
                if inputs is None:
                    return None
                found.update(dict.fromkeys(inputs))
            return tuple(found)
        case _:
            assert_never(expression)


def injective_input(expression: ScalarExpr) -> OutputColumn | None:
    """The one column `expression` is a one-to-one function of, given that the
    column is an integer or decimal: the column itself, or an injective
    Builtin of such an expression and integer or decimal literals."""
    expression = expression.closed
    match expression:
        case ColumnRef(column=column):
            return column
        case Call(injective_argument=ScalarExpr() as argument):
            return injective_input(argument)
        case (
            Call() | Constant() | NullLiteral() | Coalesce() | SimpleCase() | OpaquePureFunction() | OpaqueExpression()
            | Count() | AnyAggregate() | Accumulation() | Elements() | Equality() | NullSafeEquality() | InList() | IsNull() | IsNotNull() | And() | Or()
        ):
            return None
        case _:
            assert_never(expression)


def _exact_literal(expression: ScalarExpr) -> bool:
    """An integer or decimal literal: not an exponent form, which is a double."""
    match expression:
        case Constant(value=LiteralValue(type="numeric", text=text)):
            return "e" not in text
        case _:
            return False


def total(expression: ScalarExpr) -> bool:
    """Whether `expression` is non-null whenever every column it reads is: a
    column, a literal, `count(...)`, the IS family, a Builtin total in its
    arguments over total ones, a total equality of total sides, or a coalesce
    or a Builtin that skips NULLs with one total argument. NULL, CASE (a
    missed WHEN), IN, AND, OR (not inferred), and unmodelled functions may be
    NULL on non-null columns."""
    expression = expression.closed
    match expression:
        case ColumnRef() | Constant() | Count() | IsNull() | IsNotNull() | NullSafeEquality():
            return True
        case Call(function=Builtin(skips_null=True), arguments=arguments):
            return any(total(argument) for argument in arguments)
        case Call(total_arguments=tuple() as arguments):
            return all(total(argument) for argument in arguments)
        case Equality(total=True, left=left, right=right):
            return total(left) and total(right)
        case Coalesce(arguments=arguments):
            return any(total(argument) for argument in arguments)
        case (
            Call() | NullLiteral() | SimpleCase() | OpaquePureFunction() | OpaqueExpression() | AnyAggregate() | Accumulation() | Elements()
            | Equality() | InList() | And() | Or()
        ):
            return False
        case _:
            assert_never(expression)


def strict_inputs(expression: ScalarExpr) -> tuple[OutputColumn, ...]:
    """The columns whose NULL makes `expression` NULL, in order of first use.

    A Builtin passes on its strict arguments'; `=` both sides'; `IN (...)` its
    operand's (`1 IN (1, NULL)` is TRUE); AND and OR only a column every part
    is strict in (`NULL OR TRUE` is TRUE). Coalesce, CASE, the IS family
    (IS NOT DISTINCT FROM included) and
    unmodelled functions may turn NULL into a value; a literal, `count(...)`,
    an opaque expression or an aggregate's value has no column of its own."""
    expression = expression.closed
    match expression:
        case ColumnRef(column=column):
            return (column,)
        case Call(strict_arguments=arguments):
            return tuple(dict.fromkeys(column for argument in arguments for column in strict_inputs(argument)))
        case Equality(left=left, right=right):
            return tuple(dict.fromkeys((*strict_inputs(left), *strict_inputs(right))))
        case InList(operand=operand):
            return strict_inputs(operand)
        case And(parts=(first, *others)) | Or(parts=(first, *others)):
            rest = [strict_inputs(other) for other in others]
            return tuple(column for column in strict_inputs(first) if all(column in columns for columns in rest))
        case (
            And() | Or() | Constant() | NullLiteral() | Coalesce() | SimpleCase() | OpaquePureFunction() | OpaqueExpression()
            | Count() | AnyAggregate() | Accumulation() | Elements() | IsNull() | IsNotNull() | NullSafeEquality()
        ):
            return ()
        case _:
            assert_never(expression)


# Closed unions of every concrete ScalarExpr and RelationExpr, for an
# exhaustive `match` ending in `assert_never`. Python has no sealed classes, so
# a type checker treats a base class as open and cannot prove a match covers
# it. tests/test_ir_unions.py checks these stay in sync with the subclasses,
# which makes `ScalarExpr.closed` and `RelationExpr.closed` sound.
type AnyScalar = (
    ColumnRef | Constant | NullLiteral | Coalesce | SimpleCase | Call | OpaquePureFunction | OpaqueExpression
    | Count | AnyAggregate | Accumulation | Elements | Equality | NullSafeEquality | InList | IsNull | IsNotNull | And | Or
)


class OutputColumn(Node):
    """One named output of one relational stage and the expression producing it.

    `binary_text` marks a table column declared as text compared by identity:
    VARCHAR or TEXT, with no COLLATE, in a dialect whose default collation is
    binary (`DialectSemantics.text_is_binary`). Equal to such a column, distinct strings are distinct values.

    `identity_comparison` marks a table column whose equal values are
    identical: an integer, decimal, boolean, date, time, timestamp or UUID
    type, or binary text. Not a float (`-0.0 = 0.0`), an interval, or text
    under a collation.

    `exact_numeric` marks a table column declared as an integer or decimal.
    """

    name: str
    scalar_expr: ScalarExpr
    binary_text: bool = False
    # Declared SQL type where equality compares values without casts or collation.
    comparison_type: str | None = None
    identity_comparison: bool = False
    exact_numeric: bool = False

    def get_relation(self, program: Program) -> RelationExpr:
        """The one relation in ``program`` whose output includes this column."""
        relation = program.column_to_parent_relation_index.get(self)
        assert relation is not None, f"output column {self.name} has no parent relation in this program"
        return relation


class RelationExpr(Node, abstract=True):
    """A relation-producing expression with an explicit output schema."""

    output_columns: tuple[OutputColumn, ...]
    is_schema_complete: bool = False

    @property
    def closed(self) -> AnyRelation:
        """This node as one of the concrete RelationExprs, to match exhaustively."""
        return cast(AnyRelation, self)


class RelationRole(Enum):
    TABLE = "table"
    VIEW = "view"
    CTE = "cte"


class NamedRelation(RelationExpr):
    """A table, view, or CTE declaration shared by every reference to it."""

    name: str
    role: RelationRole
    body: RelationExpr | None = None

    @property
    def report_name(self) -> str:
        return f"CTE_{self.name}" if self.role is RelationRole.CTE else self.name


class Alias(RelationExpr):
    """One occurrence of a relation expression in a query scope."""

    source: RelationExpr
    name: str


class Join(RelationExpr):
    """A join and its ON (or USING) condition; None for a cross join.

    A condition is `And` and `Or` over the recognized `Equality`,
    `NullSafeEquality`, `InList`, `IsNull`, and `IsNotNull`; anything else is an opaque scalar expression, so
    the predicate is the whole condition.
    """

    kind: JoinKind
    left: RelationExpr
    right: RelationExpr
    predicate: ScalarExpr | None = None
    # A left join a scalar subquery lowers to: its right input is the
    # subquery's body, and each left row matches at most one right row, or
    # the query fails (more than one row is a runtime error).
    scalar_subquery: bool = False

    @property
    def conjuncts(self) -> tuple[ScalarExpr, ...]:
        return conjuncts(self.predicate)


class Filter(RelationExpr):
    """A WHERE clause, its condition lowered as in `Join`."""

    input: RelationExpr
    predicate: ScalarExpr | None = None

    @property
    def conjuncts(self) -> tuple[ScalarExpr, ...]:
        return conjuncts(self.predicate)


class Project(RelationExpr):
    input: RelationExpr


class Limit(RelationExpr):
    """A row subset selected by LIMIT/FETCH or OFFSET; count is a known hard bound, if any."""

    input: RelationExpr
    count: int | None = None


class AggregateCall(Node):
    """One aggregate function call in a grouped Select's list: its SQL, and its
    arguments lowered against the Aggregate's input (none for `count(*)`).
    Only hazards read it; an aggregate's value is opaque to every proof."""

    description: str
    arguments: tuple[ScalarExpr, ...]


class Aggregate(RelationExpr):
    input: RelationExpr
    grouping_outputs: tuple[OutputColumn, ...]
    calls: tuple[AggregateCall, ...] = ()


class Distinct(RelationExpr):
    input: RelationExpr


class SetOperation(RelationExpr):
    """UNION, INTERSECT, or EXCEPT. Output columns refer to the left input;
    `arms` pairs each with the right input's column at its position."""

    operator: SetOperator
    left: RelationExpr
    right: RelationExpr
    distinct: bool = True
    by_name: bool = False

    def arms(self, column: OutputColumn) -> tuple[OutputColumn, OutputColumn] | None:
        """The left and right input columns `column` is computed from, or None
        when the inputs pair by name (`UNION BY NAME`) or differ in width (an
        unanalyzed input whose columns are unknown)."""
        if self.by_name or len(self.left.output_columns) != len(self.right.output_columns):
            return None
        position = self.output_columns.index(column)
        return self.left.output_columns[position], self.right.output_columns[position]


class QualifyByPartition(RelationExpr):
    """One row per partition: ``QUALIFY row_number() OVER (PARTITION BY ...) = 1`` or ``DISTINCT ON (...)``."""

    input: RelationExpr
    partition_outputs: tuple[OutputColumn, ...]
    ordering: tuple[ScalarExpr, ...] = ()


class OpaqueRelation(RelationExpr):
    description: str


class RecursiveRelation(OpaqueRelation):
    """An explicit unsupported recursive reference, represented without a cycle."""

    relation_name: str


type AnyRelation = (
    NamedRelation | Alias | Join | Filter | Project | Limit | Aggregate | Distinct | SetOperation
    | QualifyByPartition | OpaqueRelation | RecursiveRelation
)


class Assertion(Node):
    """A request to prove one Property. For now its just a wrapper around a Property, but it could be extended with metadata in the future"""

    property: Property


@dataclass(frozen=True)
class Program:
    named_relations: tuple[NamedRelation, ...] = () # First pass collects all named relations so that we can reference them
    root: RelationExpr | None = None # The root of the query tree, which is the entry point for the engine to start reasoning
    assertions: tuple[Assertion, ...] = () # The properties that the engine should attempt to prove
    declarations: tuple[Property, ...] = () # The properties that the engine should assume to be true
    # Every JOIN, Select Expression, and simple CASE a marker can attach to, with
    # the Join, the relation the Select produces, or the lowered CASE.
    attachment_sites: tuple[tuple[AttachmentSite, RelationExpr | SimpleCase], ...] = ()
    # Each scalar subquery lowered as a join, with where its Select is written.
    scalar_subqueries: tuple[tuple[AttachmentSite, Join], ...] = ()
    # Each Select Expression's ORDER BY keys, at its trailing site, as columns
    # of the stage it sorts: the input of a Project, else the Select's own
    # relation. Empty with no ORDER BY; None when a key is an expression or a
    # name that does not resolve to one column, and for DISTINCT ON, whose
    # ORDER BY picks the rows it keeps.
    sort_keys: tuple[tuple[AttachmentSite, tuple[OutputColumn, ...] | None], ...] = ()
    # Each INTERSECT and EXCEPT, sited where its keyword follows its left
    # input's last Select (no marker attaches there).
    set_operations: tuple[tuple[AttachmentSite, SetOperation], ...] = ()
    # Each node standing for a construct sqlassert does not support yet (an
    # opaque relation or value), with what it is: an assertion whose proof
    # reaches one says so when it stays UNKNOWN.
    unsupported: tuple[tuple[Node, str], ...] = ()
    column_to_parent_relation_index: dict[OutputColumn, RelationExpr] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Every stage mints its own output columns, so each column has exactly one parent relation.
        index: dict[OutputColumn, RelationExpr] = {}
        for relation in self.relations():
            for column in relation.output_columns:
                parent = index.setdefault(column, relation)
                if parent is not relation:
                    raise ValueError(
                        f"output column {column.name} belongs to both {type(parent).__name__} and {type(relation).__name__}"
                    )
        object.__setattr__(self, "column_to_parent_relation_index", index)
        self._check_relations_read_once()

    def _check_relations_read_once(self) -> None:
        """Apart from Named Relations, the relations form a tree: a subquery
        or other relation is the input of one relation, read once, and a
        Named Relation is read only through an Alias, one per occurrence.
        Analyses rely on it: two columns reaching one column through the
        same Aliases reach it on the same row (`nullability._row_lineage`)."""
        readers: dict[RelationExpr, RelationExpr] = {}
        for relation in self.relations():
            for child in children(relation):
                match child:
                    case NamedRelation() if not isinstance(relation, Alias):
                        raise ValueError(f"named relation {child.name} is read by a {type(relation).__name__}, not an Alias")
                    case NamedRelation():
                        pass  # Each occurrence has its own Alias.
                    case RelationExpr() if child in readers:
                        raise ValueError(
                            f"{type(child).__name__} is read twice, by {type(readers[child]).__name__} and {type(relation).__name__}"
                        )
                    case RelationExpr():
                        readers[child] = relation
                    case _:
                        pass  # Scalar expressions and columns hold no relation.

    def nodes(self) -> tuple[Node, ...]:
        """Every node reachable from the program, each once."""
        nodes: dict[Node, None] = {}

        def visit(node: Node) -> None:
            if node in nodes:
                return
            nodes[node] = None
            for child in children(node):
                visit(child)

        for node in (*self.named_relations, *self.assertions):
            visit(node)
        if self.root is not None:
            visit(self.root)
        return tuple(nodes)

    def relations(self) -> tuple[RelationExpr, ...]:
        """Every relation reachable from the program, each once."""
        return tuple(node for node in self.nodes() if isinstance(node, RelationExpr))

    def relation_of(self, columns: frozenset[OutputColumn]) -> RelationExpr | None:
        """The parent relation shared by every column, or None when they span several or none."""
        parents = {self.column_to_parent_relation_index.get(column) for column in columns}
        if len(parents) != 1 or None in parents:
            return None
        parent, = parents
        return parent


def children(node: Node) -> tuple[Node, ...]:
    """Direct semantic references held by ``node``, in dataclass field order."""
    found: list[Node] = []
    for node_field in fields(node):
        value = getattr(node, node_field.name)
        if isinstance(value, Node):
            found.append(value)
        elif isinstance(value, tuple):
            found.extend(item for item in value if isinstance(item, Node))
    return tuple(found)
