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


class OutputColumn(Node):
    """One named output of one relational stage and the expression producing it.

    `binary_text` marks a table column declared as text compared by identity:
    VARCHAR or TEXT, with no COLLATE, in a dialect whose default collation is
    binary. Equal to such a column, distinct strings are distinct values.
    """

    name: str
    scalar_expr: ScalarExpr
    binary_text: bool = False

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
    """A join and the conjuncts of its ON (or USING) condition.

    Recognized conjuncts are `Equality`, `InList`, `IsNull`, and `IsNotNull`; any other
    conjunct is kept as an opaque scalar expression, so the tuple is the whole
    condition.
    """

    kind: str
    left: RelationExpr
    right: RelationExpr
    conjuncts: tuple[ScalarExpr, ...] = ()


class Filter(RelationExpr):
    """A WHERE clause and its conjuncts, recognized as in `Join`."""

    input: RelationExpr
    conjuncts: tuple[ScalarExpr, ...] = ()


class Project(RelationExpr):
    input: RelationExpr


class Aggregate(RelationExpr):
    input: RelationExpr
    grouping_outputs: tuple[OutputColumn, ...]


class Distinct(RelationExpr):
    input: RelationExpr


class SetOperation(RelationExpr):
    """UNION, INTERSECT, or EXCEPT. Output columns refer to the left input."""

    operator: str
    left: RelationExpr
    right: RelationExpr
    distinct: bool = True


class QualifyByPartition(RelationExpr):
    """The supported ``row_number() = 1`` qualification special case."""

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
    # Every JOIN and Select Expression a marker can attach to, with the Join or the relation the Select produces.
    attachment_sites: tuple[tuple[AttachmentSite, RelationExpr], ...] = ()
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

    def relations(self) -> tuple[RelationExpr, ...]:
        """Every relation reachable from the program, each once."""
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
        relations = []
        for node in nodes:
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
