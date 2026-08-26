"""Framework-independent relational intermediate representation.

The IR is an immutable semantic object graph. Nodes refer directly to other
nodes; identifiers are assigned only when the graph is encoded for Clingo.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import dataclass_transform

from sqlassert.provenance import Origin

INNER = "inner"


@dataclass_transform(eq_default=False, frozen_default=True, kw_only_default=True)
class NodeMeta(type):
    """Turn every Node subclass into the same kind of semantic value."""

    def __new__(metaclass, name, bases, namespace):
        node_type = super().__new__(metaclass, name, bases, namespace)
        return dataclass(frozen=True, eq=False, kw_only=True)(node_type)


class Node(metaclass=NodeMeta):
    """An immutable semantic node with object-identity equality."""

    origin: Origin


class ScalarExpr(Node):
    """A scalar expression produced or consumed by a relational operation."""


class OpaqueExpression(ScalarExpr):
    description: str


class Constant(ScalarExpr):
    """A literal whose concrete value is irrelevant to current proofs."""


class ColumnRef(ScalarExpr):
    """A direct reference to an upstream relation expression's output."""

    column: OutputColumn


class Equality(ScalarExpr):
    left: ScalarExpr
    right: ScalarExpr


class OutputColumn(Node):
    """One named output of one relational stage and the expression producing it."""

    name: str
    expression: ScalarExpr


class RelationExpr(Node):
    """A relation-producing expression with an explicit output schema."""

    outputs: tuple[OutputColumn, ...]
    schema_complete: bool = False


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
    """A requirement over a node in the relation graph."""


class UniqueJoinAssertion(Assertion):
    subject: Join


class UniqueSetAssertion(Assertion):
    subject: RelationExpr
    columns: tuple[OutputColumn, ...]
    candidate_key: bool


@dataclass(frozen=True)
class Program:
    declarations: tuple[NamedRelation, ...] = ()
    root: RelationExpr | None = None
    assertions: tuple[Assertion, ...] = ()


def joins(relation: RelationExpr | None) -> tuple[Join, ...]:
    return tuple(node for node in relation_nodes(relation) if isinstance(node, Join))


def relation_nodes(relation: RelationExpr | None) -> tuple[RelationExpr, ...]:
    """Relation expressions reachable from ``relation``, once each by identity."""
    found: list[RelationExpr] = []
    seen: set[int] = set()

    def visit(node: RelationExpr | None) -> None:
        if node is None or id(node) in seen:
            return
        seen.add(id(node))
        found.append(node)
        if isinstance(node, NamedRelation):
            visit(node.body)
        elif isinstance(node, Alias):
            visit(node.source)
        elif isinstance(node, (Join, SetOperation)):
            visit(node.left)
            visit(node.right)
        elif isinstance(node, (Filter, Project, Aggregate, Distinct, QualifyByPartition)):
            visit(node.input)

    visit(relation)
    return tuple(found)
