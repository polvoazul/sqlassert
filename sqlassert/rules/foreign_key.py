"""Foreign Keys: Referential Coverage onto a Unique Set of the referenced relation.

Knowledge -- `REFERENCES` constraints, catalog foreign keys, and
`DECLARE FOREIGN KEY` -- supplies coverage only; the referenced Unique Set is
proved separately, so a declaration never vouches for its target. Coverage is
direct: it holds for the columns it was stated on and for an Alias of their
relation, which re-exposes the same rows. Under `MATCH SIMPLE` a row with a null
referencing component is exempt, so nullability never weakens a Foreign Key.
"""

from sqlassert import ir
from sqlassert.properties import ReferentialCoverage
from sqlassert.python_engine import Result, Stratum, analysis, any_of, current_arena, unknown
from sqlassert.rules.uniqueness import unique_set


@analysis(Stratum.FOREIGN_KEY)
def foreign_key(columns: tuple[ir.OutputColumn, ...], referenced_columns: tuple[ir.OutputColumn, ...]) -> Result[bool]:
    shape = _pairing(columns, referenced_columns)
    if not shape.value:
        return shape
    return referential_coverage(columns, referenced_columns) & _referenced_unique_set(referenced_columns)


@analysis(Stratum.FOREIGN_KEY)
def referential_coverage(columns: tuple[ir.OutputColumn, ...], referenced_columns: tuple[ir.OutputColumn, ...]) -> Result[bool]:
    shape = _pairing(columns, referenced_columns)
    if not shape.value:
        return shape
    # Pairs, not lists, are the relationship: (a, b) → (x, y) is (b, a) → (y, x).
    pairs = set(zip(columns, referenced_columns))
    known = [coverage for coverage in current_arena().knowledge if isinstance(coverage, ReferentialCoverage)]
    restated = [
        current_arena().accepted[_key(coverage)] for coverage in known
        if set(zip(coverage.columns, coverage.referenced_columns)) == pairs
    ]
    if restated:
        return Result(True, "Known coverage pairs the same columns in another order", columns, [any_of(e) for e in restated])

    target = current_arena().program.relation_of(frozenset(referenced_columns))
    if isinstance(target, ir.Alias):
        # Covering an alias's rows is covering its source's rows: they are the same rows.
        alias = Result(True, "A referenced alias re-exposes its source's rows unchanged", (target,))
        return alias & referential_coverage(columns, _alias_sources(target, referenced_columns))

    relation = current_arena().program.relation_of(frozenset(columns))
    match relation:
        case ir.Alias():
            sources = _alias_sources(relation, columns)
            alias = Result(True, "An alias re-exposes its source's rows unchanged", (relation,))
            return alias & referential_coverage(sources, referenced_columns)
        case ir.NamedRelation(body=None):
            elsewhere = [
                _describe(coverage.referenced_columns) for coverage in known
                if set(coverage.columns) == set(columns)
            ]
            hint = f"; these columns reference {', '.join(elsewhere)}" if elsewhere else ""
            return unknown(
                f"No known Foreign Key from ({_names(columns)}) to {_describe(referenced_columns)}{hint}", *columns
            )
        case (
            ir.NamedRelation() | ir.Join() | ir.Filter() | ir.Project() | ir.Aggregate() | ir.Distinct()
            | ir.SetOperation() | ir.QualifyByPartition() | ir.OpaqueRelation()
        ):
            return unknown("Foreign Keys are not propagated through this operation", relation, *columns)
        case _:
            raise TypeError(f"Unhandled relation: {type(relation).__name__}")


def _referenced_unique_set(referenced_columns: tuple[ir.OutputColumn, ...]) -> Result[bool]:
    relation = current_arena().program.relation_of(frozenset(referenced_columns))
    match relation:
        case ir.NamedRelation(role=ir.RelationRole.TABLE, is_schema_complete=False):
            return unknown(
                f"Referenced relation {relation.name} has no complete declared schema, so no Unique Set is known on it",
                relation,
            )
        case _:
            unique = unique_set(frozenset(referenced_columns))
            reason = f"The referenced columns {_describe(referenced_columns)} must form a Unique Set"
            return Result(unique.value, reason, referenced_columns, [unique], kind="all")


def _pairing(columns: tuple[ir.OutputColumn, ...], referenced_columns: tuple[ir.OutputColumn, ...]) -> Result[bool]:
    program = current_arena().program
    if not columns or len(columns) != len(referenced_columns):
        return unknown("A Foreign Key pairs equally many referencing and referenced columns", *columns, *referenced_columns)
    if program.relation_of(frozenset(columns)) is None:
        return unknown("Referencing columns must belong to one relation", *columns)
    if program.relation_of(frozenset(referenced_columns)) is None:
        return unknown("Referenced columns must belong to one relation", *referenced_columns)
    return Result(True, "Columns pair by position", (*columns, *referenced_columns))


def _alias_sources(alias: ir.Alias, columns: tuple[ir.OutputColumn, ...]) -> tuple[ir.OutputColumn, ...]:
    sources = []
    for column in columns:
        match column.scalar_expr:
            case ir.ColumnRef(column=source):
                sources.append(source)
            case _:
                raise TypeError(f"Alias {alias.name} column {column.name} is not a pass-through")
    return tuple(sources)


def _key(coverage: ReferentialCoverage) -> tuple:
    return ("referential_coverage", (("columns", coverage.columns), ("referenced_columns", coverage.referenced_columns)))


def _names(columns: tuple[ir.OutputColumn, ...]) -> str:
    return ", ".join(column.name for column in columns)


def _describe(referenced_columns: tuple[ir.OutputColumn, ...]) -> str:
    relation = current_arena().program.relation_of(frozenset(referenced_columns))
    name = relation.name if isinstance(relation, ir.NamedRelation) else "relation"
    return f"{name}({_names(referenced_columns)})"
