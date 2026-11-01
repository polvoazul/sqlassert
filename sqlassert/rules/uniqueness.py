"""Establish, preserve, and cover unique sets directly on the semantic IR."""

from sqlassert import ir
from sqlassert.properties import ExactlyOneJoin, NonNullColumns, ReferentialCoverage, UniqueJoin, UniqueSet
from sqlassert.python_engine import Result, Stratum, all_of, analysis, any_of, current_arena, unknown
from sqlassert.rules.column_set import ColumnSet, _column_set


@analysis(Stratum.UNIQUE_SET)
def unique_set(columns: frozenset[ir.OutputColumn]) -> Result[bool]:
    relation = current_arena().program.relation_of(columns)
    if relation is None:
        return unknown("A unique set must have columns belonging to one relation", *columns)
    return _covers_unique_set(_column_set(relation, columns))


@analysis(Stratum.UNIQUE_SET)
def non_null_unique_set(columns: frozenset[ir.OutputColumn]) -> Result[bool]:
    from sqlassert.rules.nullability import non_null_columns

    return unique_set(columns) & non_null_columns(columns)


@analysis(Stratum.UNIQUE_SET)
def _unique_sets(relation: ir.RelationExpr) -> Result[dict[frozenset[ir.OutputColumn], Result[bool]]]:
    keys = {}
    arena = current_arena()
    for (name, arguments), evidence in arena.accepted.items():
        if name == "unique_set":
            columns = dict(arguments)["columns"]
            if columns and columns <= set(relation.output_columns):
                _add(keys, columns, any_of(evidence))

    match relation:
        case ir.Aggregate(grouping_outputs=columns) | ir.QualifyByPartition(partition_outputs=columns):
            _add(keys, frozenset(columns), Result(True, "Operation establishes a unique set", (relation,)))
        case ir.Distinct(output_columns=columns):
            _add(keys, frozenset(columns), Result(True, "DISTINCT establishes a unique set", (relation,)))
        case ir.Alias() | ir.NamedRelation() | ir.Filter() | ir.Project() | ir.Join():
            pass
        case ir.SetOperation() | ir.OpaqueRelation():
            pass
        case _:
            raise TypeError(f"Unhandled relation: {type(relation).__name__}")

    preservation = _preserving_input(relation)
    source = preservation.value
    if source is not None:
        for columns, evidence in _unique_sets(source).value.items():
            mapping = _pass_through_columns(relation, columns)
            if mapping.value is not None:
                proof = Result(True, "Unique set survives this operation", (relation,), [evidence, preservation, mapping])
                _add(keys, mapping.value, proof)

    # Only requested supersets become new keys, matching column sets of interest.
    for assertion in arena.program.assertions:
        match assertion.property:
            case UniqueSet(columns=columns):
                if columns and columns <= set(relation.output_columns):
                    known = Result(keys.copy(), "Unique sets established before this request")
                    coverage = _coverage(_column_set(relation, columns), known)
                    if coverage.value:
                        _add(keys, columns, coverage)
            case UniqueJoin() | ExactlyOneJoin() | NonNullColumns() | ReferentialCoverage():
                pass
            case _:
                raise TypeError(f"Unhandled assertion property: {type(assertion.property).__name__}")
    return Result(keys, "Known unique sets", (relation,), list(keys.values()))


@analysis(Stratum.UNIQUE_SET)
def _preserving_input(relation: ir.RelationExpr) -> Result[ir.RelationExpr | None]:
    match relation:
        case ir.Alias(source=source):
            return Result(source, "Alias preserves source unique sets", (relation,))
        case ir.NamedRelation(body=source):
            return Result(source, "Named relation uses its body", (relation,))
        case ir.Filter(input=source) | ir.Project(input=source) | ir.QualifyByPartition(input=source):
            return Result(source, "Operation preserves input unique sets", (relation,))
        case ir.Join(left=source):
            from sqlassert.rules.unique_join import unique_join

            proof = unique_join(relation)
            return Result(source if proof.value else None, "Unique join preserves left unique sets", (relation,), [proof])
        case ir.Aggregate() | ir.Distinct() | ir.SetOperation() | ir.OpaqueRelation():
            return Result(None, "Operation does not preserve input unique sets", (relation,))
        case _:
            raise TypeError(f"Unhandled relation: {type(relation).__name__}")


@analysis(Stratum.UNIQUE_SET)
def _pass_through_columns(relation: ir.RelationExpr, columns: frozenset[ir.OutputColumn]):
    targets = []
    for column in columns:
        matches = []
        for target in relation.output_columns:
            match target.scalar_expr:
                case ir.ColumnRef(column=source_column):
                    if source_column is column:
                        matches.append(target)
                case ir.Constant() | ir.NullLiteral() | ir.Coalesce() | ir.OpaqueExpression() | ir.AnyAggregate() | ir.Equality():
                    pass
                case _:
                    raise TypeError(f"Unhandled scalar expression: {type(target.scalar_expr).__name__}")
        if len(matches) != 1:
            return Result(None, "Key column must pass through exactly once", (relation, column))
        targets.append(matches[0])
    return Result(frozenset(targets), "Each key column passes through exactly once", (relation, *targets))


@analysis(Stratum.UNIQUE_SET)
def _covers_unique_set(cols: Result[ColumnSet]) -> Result[bool]:
    return _coverage(cols, _unique_sets(cols.value.relation))


@analysis(Stratum.UNIQUE_SET)
def _coverage(cols: Result[ColumnSet], keys: Result[dict[frozenset[ir.OutputColumn], Result[bool]]]) -> Result[bool]:
    alternatives = []
    for key, evidence in keys.value.items():
        missing = key - cols.value.columns
        if missing:
            coverage = unknown("Unique-set columns are not covered", *missing)
        else:
            coverage = all_of(cols.members[column] for column in cols.value.relation.output_columns if column in key)
        alternatives.append(evidence & coverage)

    if not alternatives:
        return unknown("No known unique set on this relation", cols.value.relation)
    return any_of(alternatives)


def _add(keys, columns, evidence):
    # The LP membership representation establishes only nonempty unique sets.
    if columns:
        keys[columns] = keys[columns] | evidence if columns in keys else evidence
