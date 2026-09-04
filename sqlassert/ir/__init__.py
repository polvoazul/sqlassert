"""Relational intermediate representation."""

from sqlassert.ir.model import Aggregate, Alias, AnyAggregate, Assertion, ColumnRef, Constant, Distinct, Equality, Filter, INNER, Join, NamedRelation, Node, OpaqueExpression, OpaqueRelation, OutputColumn, Program, Project, QualifyByPartition, RecursiveRelation, RelationExpr, RelationRole, ScalarExpr, SetOperation, children

__all__ = [
    "Aggregate",
    "Alias",
    "AnyAggregate",
    "Assertion",
    "ColumnRef",
    "Constant",
    "Distinct",
    "Equality",
    "Filter",
    "INNER",
    "Join",
    "NamedRelation",
    "Node",
    "OpaqueExpression",
    "OpaqueRelation",
    "OutputColumn",
    "Program",
    "Project",
    "QualifyByPartition",
    "RecursiveRelation",
    "RelationExpr",
    "RelationRole",
    "ScalarExpr",
    "SetOperation",
    "children",
]
