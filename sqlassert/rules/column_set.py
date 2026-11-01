"""Intermediate values used by column-set property functions."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlassert import ir
from sqlassert.python_engine import Result


@dataclass(frozen=True)
class RowIdentity:
    """Tells apart every row of `relation`, duplicates included (Paulley's ι).

    Analysis only: never in the IR, in SQL, or shown to authors.
    """

    relation: ir.RelationExpr


type Attribute = ir.OutputColumn | RowIdentity


@dataclass(eq=False)
class ColumnSet:
    relation: ir.RelationExpr
    columns: set[Attribute]


@dataclass(eq=False)
class Closure(ColumnSet):
    """A closure, with the closures it took in the relation's inputs."""

    inputs: tuple[Result[Closure], ...] = field(default=())
