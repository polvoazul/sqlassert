"""IR-linked facts supplied to and derived for the property engine."""

from __future__ import annotations

from sqlassert.ir.model import NodeMeta, OutputColumn, RelationExpr


class Knowledge(metaclass=NodeMeta, abstract=True):
    """One public fact over the relational IR."""


class NonNullColumn(Knowledge):
    relation: RelationExpr
    column: OutputColumn


class UniqueSet(Knowledge):
    relation: RelationExpr


class UniqueSetColumn(Knowledge):
    unique_set: UniqueSet
    position: int
    column: OutputColumn
