"""Framework-independent relational intermediate representation.

The IR is an immutable semantic object graph. Nodes refer directly to other
nodes and carry no identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import TYPE_CHECKING, dataclass_transform

from sqlassert.provenance import AttachmentSite, Origin

if TYPE_CHECKING:
    from sqlassert.properties import Property

INNER = "inner"


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


class OpaqueExpression(ScalarExpr):
    """An expression whose value is not modelled.

    `deterministic` ones are a function of exactly the input columns in
    `inputs` (Paulley §3.1.4): rows agreeing on them agree on it. It is NULL
    whenever one of `strict_inputs` is (`n + 1`, `n > m`, `NOT p`), so a
    WHERE or ON conjunct rejects them. Nothing else about the value, its
    nullability or its domain is known."""

    description: str
    deterministic: bool = False
    inputs: tuple[OutputColumn, ...] = ()
    strict_inputs: tuple[OutputColumn, ...] = ()


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

    type: str
    text: str

    def sql(self) -> str:
        match self.type:
            case "string":
                return "'" + self.text.replace("'", "''") + "'"
            case "numeric" | "boolean":
                return self.text
            case _:
                raise ValueError(f"Unhandled literal type: {self.type}")


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
    """An arbitrary value selected from the input rows of an aggregate group."""

    input: ScalarExpr


class ColumnRef(ScalarExpr):
    """A direct reference to an upstream relation expression's output."""

    column: OutputColumn


class Equality(ScalarExpr):
    """`left = right`. As a conjunct it holds only on rows where both sides are non-null."""

    left: ScalarExpr
    right: ScalarExpr


class InList(ScalarExpr):
    """`operand IN (values...)`, recognized as a WHERE or ON conjunct."""

    operand: ScalarExpr
    values: tuple[ScalarExpr, ...]


class IsNull(ScalarExpr):
    """`operand IS NULL`."""

    operand: ScalarExpr


class IsNotNull(ScalarExpr):
    """`operand IS NOT NULL`."""

    operand: ScalarExpr


class And(ScalarExpr):
    """`p AND q ...` in a WHERE or ON condition, flattened: TRUE when every part is."""

    parts: tuple[ScalarExpr, ...]


class Or(ScalarExpr):
    """`p OR q ...` in a WHERE or ON condition, flattened: TRUE when some part is.

    Its parts are lowered like a WHERE condition. An AND or OR inside a value,
    such as `(a OR b) = c`, stays an opaque expression.
    """

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


class OutputColumn(Node):
    """One named output of one relational stage and the expression producing it.

    `binary_text` marks a table column declared as text compared by identity:
    VARCHAR or TEXT, with no COLLATE, in a dialect whose default collation is
    binary. Equal to such a column, distinct strings are distinct values.

    `identity_comparison` marks a table column whose equal values are
    identical: an integer, decimal, boolean, date, time, timestamp or UUID
    type, or binary text. Not a float (`-0.0 = 0.0`), an interval, or text
    under a collation.
    """

    name: str
    scalar_expr: ScalarExpr
    binary_text: bool = False
    # Declared SQL type where equality compares values without casts or collation.
    comparison_type: str | None = None
    identity_comparison: bool = False

    def get_relation(self, program: Program) -> RelationExpr:
        """The one relation in ``program`` whose output includes this column."""
        relation = program.column_to_parent_relation_index.get(self)
        assert relation is not None, f"output column {self.name} has no parent relation in this program"
        return relation


class RelationExpr(Node, abstract=True):
    """A relation-producing expression with an explicit output schema."""

    output_columns: tuple[OutputColumn, ...]
    is_schema_complete: bool = False


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

    A condition is `And` and `Or` over the recognized `Equality`, `InList`,
    `IsNull`, and `IsNotNull`; anything else is an opaque scalar expression, so
    the predicate is the whole condition.
    """

    kind: str
    left: RelationExpr
    right: RelationExpr
    predicate: ScalarExpr | None = None

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


class Aggregate(RelationExpr):
    input: RelationExpr
    grouping_outputs: tuple[OutputColumn, ...]


class Distinct(RelationExpr):
    input: RelationExpr


class SetOperation(RelationExpr):
    """UNION, INTERSECT, or EXCEPT. Output columns refer to the left input;
    `arms` pairs each with the right input's column at its position."""

    operator: str
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
        relations = []
        for node in self.nodes():
            match node:
                case RelationExpr():
                    relations.append(node)
                case ScalarExpr() | OutputColumn() | Assertion():
                    pass
                case _:
                    raise TypeError(f"Unhandled IR node: {type(node).__name__}")
        return tuple(relations)

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
