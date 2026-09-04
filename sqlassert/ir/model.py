"""Framework-independent relational intermediate representation.

The IR is an immutable semantic object graph. Nodes refer directly to other
nodes; identifiers are assigned only when the graph is encoded for Clingo.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum
from typing import TYPE_CHECKING, dataclass_transform

from sqlassert.provenance import Origin

if TYPE_CHECKING:
    from sqlassert.properties import Property

INNER = "inner"


@dataclass_transform(eq_default=False, frozen_default=True, kw_only_default=True)
class NodeMeta(type):
    """Turn every Node subclass into the same kind of semantic value."""

    def __new__(metaclass, name, bases, namespace, *, abstract: bool = False):
        node_type = super().__new__(metaclass, name, bases, namespace)
        node_type = dataclass(frozen=True, eq=False, kw_only=True)(node_type)
        node_type.__ir_abstract__ = abstract
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


class Constant(ScalarExpr):
    """A literal whose concrete value is irrelevant to current proofs."""


class AnyAggregate(ScalarExpr):
    """An arbitrary value selected from the input rows of an aggregate group."""

    input: ScalarExpr


class ColumnRef(ScalarExpr):
    """A direct reference to an upstream relation expression's output."""

    column: OutputColumn


class Equality(ScalarExpr):
    left: ScalarExpr
    right: ScalarExpr


class OutputColumn(Node):
    """One named output of one relational stage and the expression producing it."""

    name: str
    scalar_expr: ScalarExpr


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
    kind: str
    left: RelationExpr
    right: RelationExpr
    equalities: tuple[Equality, ...] = ()


class Filter(RelationExpr):
    input: RelationExpr


class Project(RelationExpr):
    input: RelationExpr


class Aggregate(RelationExpr):
    input: RelationExpr
    grouping_outputs: tuple[OutputColumn, ...]


class Distinct(RelationExpr):
    input: RelationExpr


class SetOperation(RelationExpr):
    operator: str
    left: RelationExpr
    right: RelationExpr


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
