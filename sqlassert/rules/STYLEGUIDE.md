# Logic Rule Style Guide

The predicate conventions below apply to the archived Clingo files in `_old/`.
The Python rules use analysis functions. Each returns `Result[Value]`: `.value`
is the computed Value and the remaining fields directly represent its evidence.
`@analysis` provides declaration lookup and analysis-scoped caching for public and
internal analysis functions. Every concrete public `Property` class has one
snake-case public analysis function whose parameters match its fields. Internal
analysis functions start with `_`. `ColumnSet` is an ordinary derived value defined in
these rules. Use explicit matches for semantic dispatch, and fail on unhandled
node types. Keep IR traversal and set operations in small helpers so analysis
functions state their semantic rule directly. Use `&` / `|` or `all_of` /
`any_of` to preserve evidence; Python's `and` / `or` deliberately reject
`Result` values. Use `.value` for every result, including Boolean results
(false means unproved). `&` / `|` and `all_of` / `any_of` compose only
`Result[bool]` values and retain their evidence.

## Predicate namespaces

- `ir__...` predicates encode IR nodes and their fields.
- `pub__...` predicates are the public logic API.
- Predicates without a namespace are internal implementation details.

## Internal structural predicates

When an internal predicate expresses a property, field, ownership relation, or
membership relation of a domain term, name it with pseudo-object syntax:

```prolog
column_set__relexp(ColumnSet, RelationExpr).
column_set__columns(ColumnSet, Column).
unique_set__relexp(UniqueSet, RelationExpr).
join__rhs_output_columns(Join, Column).
```

Use the singular domain term followed by `__` and the property name. Use
`relexp` consistently for a relation-expression value.

## Semantic derivations

Do not force pseudo-object syntax on predicates that state a derived condition
or relationship rather than a property. Prefer a readable phrase when it
describes the inference itself:

```prolog
supported_unique_join(Join).
covers_unique_set(ColumnSet, UniqueSet).
known_given_left_row(Join, ScalarExpr).
```

When uncertain, use `__` for structural data and ordinary words for logic.
