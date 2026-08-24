"""The relational IR: the integration boundary between SQL analysis and
property reasoning.

Every node is an immutable Python value carrying an origin identifier. No
SQLGlot node, database connection, or Clingo symbol may appear here, and the
property engine is the only consumer that turns these values into facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

INNER = "inner"


@dataclass(frozen=True)
class ColumnReference:
    """A reference to one column of one Relation Instance."""

    id: str
    instance_id: str
    column: str
    origin_id: str


@dataclass(frozen=True)
class OpaqueExpression:
    """An expression whose semantics conversion did not model.

    Represented explicitly so that conversion never silently discards meaning.
    """

    id: str
    description: str
    origin_id: str


Expression = ColumnReference | OpaqueExpression


@dataclass(frozen=True)
class Equality:
    id: str
    left: Expression
    right: Expression
    origin_id: str


@dataclass(frozen=True)
class RelationDefinition:
    """The reusable meaning of a table, view, CTE, or subquery.

    Column and uniqueness facts live in Knowledge, not here, so that query
    structure and what is known about relations stay separately represented.
    """

    id: str
    name: str
    origin_id: str


@dataclass(frozen=True)
class RelationInstance:
    """One occurrence of a Relation Definition within a query."""

    id: str
    definition_id: str
    alias: str | None
    origin_id: str


@dataclass(frozen=True)
class Scan:
    id: str
    instance: RelationInstance


@dataclass(frozen=True)
class OpaqueRelation:
    """A relational subplan whose semantics conversion did not model.

    It still owns a Relation Instance, so nothing about the query is discarded:
    the instance simply has no properties to reason from.
    """

    id: str
    description: str
    instance: RelationInstance
    origin_id: str


@dataclass(frozen=True)
class Join:
    id: str
    kind: str
    left: "Plan"
    right: "Plan"
    equalities: tuple[Equality, ...] = ()
    origin_id: str = ""


Plan = Scan | OpaqueRelation | Join


@dataclass(frozen=True)
class UniqueJoinAssertion:
    """A requirement that a join cannot multiply its left-hand input's rows."""

    id: str
    join_id: str
    origin_id: str


@dataclass(frozen=True)
class Program:
    definitions: tuple[RelationDefinition, ...] = ()
    root: Plan | None = None
    assertions: tuple[UniqueJoinAssertion, ...] = field(default=())


def instances(plan: Plan | None) -> tuple[RelationInstance, ...]:
    """Every Relation Instance reachable from a plan, in source order."""
    if plan is None:
        return ()
    if isinstance(plan, (Scan, OpaqueRelation)):
        return (plan.instance,)
    return instances(plan.left) + instances(plan.right)


def joins(plan: Plan | None) -> tuple[Join, ...]:
    if plan is None or isinstance(plan, (Scan, OpaqueRelation)):
        return ()
    return joins(plan.left) + joins(plan.right) + (plan,)
