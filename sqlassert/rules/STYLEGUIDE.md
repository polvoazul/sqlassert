# Rule Style Guide

Rules are analysis functions. Each returns `Result[Value]`: `.value`
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
