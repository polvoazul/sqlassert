"""Relational intermediate representation."""

from sqlassert.ir.model import Aggregate, Alias, Assertion, ColumnRef, Constant, Distinct, Equality, Filter, INNER, Join, NamedRelation, Node, OpaqueExpression, OpaqueRelation, OutputColumn, Program, Project, QualifyByPartition, RecursiveRelation, RelationExpr, RelationRole, ScalarExpr, SetOperation, UniqueJoinAssertion, UniqueSetAssertion, children

__all__ = [
    "Aggregate",
    "Alias",
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
    "UniqueJoinAssertion",
    "UniqueSetAssertion",
    "children",
]
