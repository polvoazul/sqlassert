"""Structural properties over the relational IR."""

from __future__ import annotations

from sqlassert.ir.model import Join, LiteralValue, NodeMeta, OutputColumn, SimpleCase


class Property(metaclass=NodeMeta, abstract=True):
    """One semantic statement, independent of how it enters reasoning."""


class NonNullColumn(Property):
    column: OutputColumn


class NonNullColumns(Property):
    """Every listed column is non-null: one assertion for a whole column list."""

    columns: frozenset[OutputColumn]


class UniqueSet(Property):
    columns: frozenset[OutputColumn]


class NonNullUniqueSet(UniqueSet):
    """A Unique Set whose columns are all non-null."""


class FunctionalDependency(Property):
    """Rows agreeing on every `determinant` column, all non-null, agree on
    every `dependent` column (`DETERMINES`). Lax: NULL in the determinant
    exempts a row, as it does for a Unique Set."""

    determinant: frozenset[OutputColumn]
    dependent: frozenset[OutputColumn]


class EnumeratedDomain(Property):
    """Every non-null value of `column` is one of `values`.

    NULL is outside every domain (`NOT NULL` governs it), and a domain does
    not claim that each of its values occurs.
    """

    column: OutputColumn
    values: frozenset[LiteralValue]


class ExhaustiveCase(Property):
    """Every member of `case`'s operand's Enumerated Domain has a WHEN arm
    that is exactly that literal, so no non-null operand value reaches ELSE.

    ELSE never substitutes for an arm, and a NULL operand, which matches no
    arm, is left to `NOT NULL`.
    """

    case: SimpleCase


class UniqueJoin(Property):
    """A join that cannot multiply rows from its left input."""

    join: Join


class ExactlyOneJoin(Property):
    """A join in which every row of its left input matches exactly one right row.

    The join neither multiplies nor removes left rows, and a left join
    null-extends none of them.
    """

    join: Join


class ReferentialCoverage(Property):
    """Every row whose referencing columns are all non-null has a row of the
    referenced relation equal to it on the paired referenced columns.

    Columns pair by position. A row with a null referencing component is exempt
    (`MATCH SIMPLE`). This is the part of a Foreign Key that Knowledge supplies:
    `REFERENCES` constraints, catalog foreign keys, and declarations.
    """

    columns: tuple[OutputColumn, ...]
    referenced_columns: tuple[OutputColumn, ...]


class ForeignKey(ReferentialCoverage):
    """Referential Coverage onto a Unique Set of the referenced relation."""
