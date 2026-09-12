# Logic Rule Style Guide

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
