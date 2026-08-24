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
class Constant:
    """A literal value in an equality.

    Its value is not represented: a constant determines the column it is
    equated to regardless of what the value is, so the property engine never
    needs to know it.
    """

    id: str
    origin_id: str


@dataclass(frozen=True)
class OpaqueExpression:
    """An expression whose semantics conversion did not model.

    Represented explicitly so that conversion never silently discards meaning.
    """

    id: str
    description: str
    origin_id: str


Expression = ColumnReference | Constant | OpaqueExpression


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


@dataclass(frozen=True)
class Filter:
    """Restricts its input's rows without renaming or dropping any column.

    Its own Relation Instance shares the input's `definition_id`: a filter
    can only remove rows, so every Unique Set the input relation already has
    still holds, and no new Relation Definition is needed to say so.
    """

    id: str
    input: "Plan"
    instance: RelationInstance
    origin_id: str


@dataclass(frozen=True)
class ProjectedColumn:
    name: str
    expression: Expression


@dataclass(frozen=True)
class Project:
    """Renames, drops, or computes columns of its input.

    Unlike Filter, this can change what a Unique Set means, so it owns a
    fresh, anonymous Relation Definition rather than reusing the input's.
    """

    id: str
    input: "Plan"
    instance: RelationInstance
    outputs: tuple[ProjectedColumn, ...]
    origin_id: str


Plan = Scan | OpaqueRelation | Join | Filter | Project


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
    """Every Relation Instance immediately visible from a plan, in source order.

    Filter and Project are leaves here, like Scan and OpaqueRelation: a
    derived table exposes only its own outer instance, keeping whatever it
    wraps out of the enclosing query's column scope.
    """
    if plan is None:
        return ()
    if isinstance(plan, (Scan, OpaqueRelation, Filter, Project)):
        return (plan.instance,)
    return instances(plan.left) + instances(plan.right)


def all_instances(plan: Plan | None) -> tuple[RelationInstance, ...]:
    """Every Relation Instance anywhere in a plan, including inside Filter and
    Project, whose own inputs are otherwise invisible to `instances`.

    Used only for fact generation: every instance needs its `instance_of`
    fact, even one that no outer scope can resolve a column against.
    """
    if plan is None:
        return ()
    if isinstance(plan, (Scan, OpaqueRelation)):
        return (plan.instance,)
    if isinstance(plan, (Filter, Project)):
        return (plan.instance,) + all_instances(plan.input)
    return all_instances(plan.left) + all_instances(plan.right)


def joins(plan: Plan | None) -> tuple[Join, ...]:
    """Every Join reachable from a plan.

    Filter and Project are leaves: this ticket's derived tables never wrap a
    Join, so nothing here needs to look inside `.input`.
    """
    if plan is None or isinstance(plan, (Scan, OpaqueRelation, Filter, Project)):
        return ()
    return joins(plan.left) + joins(plan.right) + (plan,)


def projects(plan: Plan | None) -> tuple[Project, ...]:
    """Every Project reachable from a plan, including inside Filter/Project chains."""
    if plan is None or isinstance(plan, (Scan, OpaqueRelation)):
        return ()
    if isinstance(plan, Filter):
        return projects(plan.input)
    if isinstance(plan, Project):
        return projects(plan.input) + (plan,)
    return projects(plan.left) + projects(plan.right)
