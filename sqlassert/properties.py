"""Structural properties over the relational IR."""

from __future__ import annotations

from sqlassert.ir.model import Join, NodeMeta, OutputColumn


class Property(metaclass=NodeMeta, abstract=True):
    """One semantic statement, independent of how it enters reasoning."""


class NonNullColumn(Property):
    column: OutputColumn


class UniqueSet(Property):
    columns: frozenset[OutputColumn]


class CandidateKey(UniqueSet):
    """A Unique Set whose columns are all non-null."""


class UniqueJoin(Property):
    """A join that cannot multiply rows from its left input."""

    join: Join
