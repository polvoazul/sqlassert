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

    `name` and `report_name` serve two different, deliberately separate
    purposes. `name` is what Knowledge is looked up by: it is empty for
    every derived relation (a Project, Aggregate, Distinct, or an anonymous
    subquery), because a derived relation's own properties must never be
    confused with a declared relation's that happens to share its name.
    `report_name` is purely a caller-facing label for `report.facts` -- a
    CTE's own declared name, say -- and is never consulted for Knowledge.
    """

    id: str
    name: str
    origin_id: str
    report_name: str | None = None


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
class SetOperation:
    """UNION, INTERSECT, or EXCEPT: each side is lowered independently, the
    same way the two sides of a Join are, so a marked join in either arm is
    reachable for analysis.

    `operator` is carried for provenance only. All three share one DISTINCT
    vs. ALL toggle (`UNION`/`INTERSECT`/`EXCEPT` dedup their combined output;
    the `ALL` form of each keeps duplicates instead), and the operation's own
    row-set semantics -- that the non-ALL form's combined output is unique
    over every output column, the same way a bare `SELECT DISTINCT` earns a
    Unique Set over its output columns -- is deliberately not modeled here:
    that is a proof in its own right, not a precondition for reaching the
    joins nested in either arm.

    TODO: earning that Unique Set needs three things together, none done yet:
    this node its own `RelationInstance`/`RelationDefinition` and an output
    column list (as `Distinct` has); the Unique Set derived from the leftmost
    arm's output names when `distinct` is set; and `CteScope`/
    `_lower_nested_source` taught to lower a `SetOperation` body instead of
    only a plain `exp.Select`, since otherwise nothing could reference the
    result to ever observe the proof.
    """

    id: str
    operator: str
    left: "Plan"
    right: "Plan"
    origin_id: str


@dataclass(frozen=True)
class Filter:
    """Restricts its input's rows without renaming or dropping any column.

    Owns a fresh Relation Definition like every other derived table, so it
    can carry its own name for `report.facts` (a CTE's, for instance) --
    unlike Project, its Unique Sets are never conditional on which columns
    survived, since a Filter can only remove rows: every Unique Set of its
    input relation propagates to it whole and unchanged (see
    `rules/propagation.lp`).
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


@dataclass(frozen=True)
class GroupingKey:
    """One `GROUP BY` expression, named by the output column an outer query
    sees it as -- the only thing a Unique Set built from it needs, since a
    later join can only ever refer to a Grouping Key by that output name.

    Only a Grouping Key contributes to the Unique Set an Aggregate earns: an
    Aggregate Expression such as a sum or count is deliberately not
    represented here, so it can never be mistaken for one.
    """

    name: str


@dataclass(frozen=True)
class Aggregate:
    """An ordinary `GROUP BY`: unique by its complete set of Grouping Keys,
    regardless of what its input relation's own Unique Sets were.

    Owns a fresh, anonymous Relation Definition for the same reason Project
    does: grouping changes what a Unique Set means.
    """

    id: str
    input: "Plan"
    instance: RelationInstance
    grouping_keys: tuple[GroupingKey, ...]
    origin_id: str


@dataclass(frozen=True)
class Distinct:
    """`SELECT DISTINCT`: unique by its complete set of output expressions,
    regardless of what its input relation's own Unique Sets were.
    """

    id: str
    input: "Plan"
    instance: RelationInstance
    outputs: tuple[ProjectedColumn, ...]
    origin_id: str


@dataclass(frozen=True)
class PartitionKey:
    """One `PARTITION BY` expression of a QualifyByPartition, named by the
    output column an outer query sees it as -- the only thing a Unique Set
    built from it needs, since a later join can only ever refer to a
    Partition Key by that output name. Keeps its own lowered expression, the
    same way ProjectedColumn does, so its provenance survives for future
    best-effort reporting even though the MVP does not yet render it.
    """

    name: str
    expression: Expression


@dataclass(frozen=True)
class QualifyByPartition:
    """A recognized `ROW_NUMBER() OVER (PARTITION BY ...) = 1` qualification:
    unique by its complete Partition Key, because that predicate retains
    exactly one row per partition regardless of how ORDER BY orders rows
    within it.

    Only this narrow shape earns a Unique Set. `ordering` keeps the ORDER BY
    expressions' own provenance for future best-effort reporting -- it never
    determines uniqueness, since ORDER BY decides which row survives, not how
    many do. Any other rank function, retention predicate, or general window
    semantics is left as an OpaqueRelation instead, exactly as an unsupported
    Aggregate is.
    """

    id: str
    input: "Plan"
    instance: RelationInstance
    partition_keys: tuple[PartitionKey, ...]
    ordering: tuple[Expression, ...]
    origin_id: str


Plan = Scan | OpaqueRelation | Join | SetOperation | Filter | Project | Aggregate | Distinct | QualifyByPartition


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


_DERIVED_TABLE = (Scan, OpaqueRelation, Filter, Project, Aggregate, Distinct, QualifyByPartition)


def instances(plan: Plan | None) -> tuple[RelationInstance, ...]:
    """Every Relation Instance immediately visible from a plan, in source order.

    Filter, Project, Aggregate, and Distinct are leaves here, like Scan and
    OpaqueRelation: a derived table exposes only its own outer instance,
    keeping whatever it wraps out of the enclosing query's column scope.
    """
    if plan is None:
        return ()
    if isinstance(plan, _DERIVED_TABLE):
        return (plan.instance,)
    return instances(plan.left) + instances(plan.right)


def all_instances(plan: Plan | None) -> tuple[RelationInstance, ...]:
    """Every Relation Instance anywhere in a plan, including inside a derived
    table, whose own input is otherwise invisible to `instances`.

    Used only for fact generation: every instance needs its `instance_of`
    fact, even one that no outer scope can resolve a column against.
    """
    if plan is None:
        return ()
    if isinstance(plan, (Scan, OpaqueRelation)):
        return (plan.instance,)
    if isinstance(plan, (Filter, Project, Aggregate, Distinct, QualifyByPartition)):
        return (plan.instance,) + all_instances(plan.input)
    return all_instances(plan.left) + all_instances(plan.right)


def joins(plan: Plan | None) -> tuple[Join, ...]:
    """Every Join reachable from a plan.

    Every derived table is a leaf: this slice's derived tables never wrap a
    Join, so nothing here needs to look inside `.input`. A SetOperation is not
    itself a Join -- it shares the two-children shape only so both of its arms
    are walked the same way a Join's sides are.
    """
    if plan is None or isinstance(plan, _DERIVED_TABLE):
        return ()
    found = joins(plan.left) + joins(plan.right)
    return (*found, plan) if isinstance(plan, Join) else found


def _collect(plan: Plan | None, node_type: type) -> tuple:
    """Every `node_type` node reachable from a plan, including inside chains
    of other derived tables -- one Aggregate nested inside a Distinct inside
    a Filter is still found, and vice versa.

    Shared by `projects`, `aggregates`, and `distincts`, which otherwise
    differ only in which single Plan subtype they collect.
    """
    if plan is None or isinstance(plan, (Scan, OpaqueRelation)):
        return ()
    if isinstance(plan, (Filter, Project, Aggregate, Distinct, QualifyByPartition)):
        found = _collect(plan.input, node_type)
        return (*found, plan) if isinstance(plan, node_type) else found
    return _collect(plan.left, node_type) + _collect(plan.right, node_type)


def filters(plan: Plan | None) -> tuple[Filter, ...]:
    return _collect(plan, Filter)


def projects(plan: Plan | None) -> tuple[Project, ...]:
    return _collect(plan, Project)


def aggregates(plan: Plan | None) -> tuple[Aggregate, ...]:
    return _collect(plan, Aggregate)


def distincts(plan: Plan | None) -> tuple[Distinct, ...]:
    return _collect(plan, Distinct)


def qualify_by_partitions(plan: Plan | None) -> tuple[QualifyByPartition, ...]:
    return _collect(plan, QualifyByPartition)
