"""Knowledge: typed semantic facts about relation definitions and columns.

Knowledge is independent of the analyzed query. It holds no SQL text, no
SQLGlot AST, and no database connection, so it can be supplied by a caller or
derived from Create Statements without changing the reasoning path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from sqlassert.provenance import Origin


@dataclass(frozen=True)
class ColumnKnowledge:
    name: str
    nullable: bool = True


@dataclass(frozen=True)
class UniqueSetKnowledge:
    """Columns whose fully non-null value combinations cannot repeat."""

    columns: tuple[str, ...]


@dataclass(frozen=True)
class RelationKnowledge:
    """Facts about one Relation Definition, named as it is declared.

    The name is a whole qualified name: `users` and `b.users` are different
    relations, and neither inherits the other's Unique Sets.
    """

    name: str
    columns: tuple[ColumnKnowledge, ...] = ()
    unique_sets: tuple[UniqueSetKnowledge, ...] = ()
    origin: Origin | None = None


@dataclass(frozen=True)
class Knowledge:
    relations: tuple[RelationKnowledge, ...] = ()

    def relation(self, name: str) -> RelationKnowledge | None:
        return next((relation for relation in self.relations if _same(relation.name, name)), None)

    def merge(self, other: Knowledge | None) -> Knowledge:
        """Combine two sets of facts; `other` extends what is already known."""
        if other is None:
            return self

        merged = list(self.relations)
        for relation in other.relations:
            existing = next((index for index, known in enumerate(merged) if _same(known.name, relation.name)), None)
            if existing is None:
                merged.append(relation)
            else:
                merged[existing] = _combine(merged[existing], relation)
        return Knowledge(tuple(merged))


def _combine(known: RelationKnowledge, extra: RelationKnowledge) -> RelationKnowledge:
    names = {column.name.lower() for column in known.columns}
    columns = list(known.columns) + [column for column in extra.columns if column.name.lower() not in names]

    unique_sets = list(known.unique_sets)
    for unique_set in extra.unique_sets:
        if unique_set not in unique_sets:
            unique_sets.append(unique_set)

    return replace(known, columns=tuple(columns), unique_sets=tuple(unique_sets))


def _same(left: str, right: str) -> bool:
    return left.lower() == right.lower()
