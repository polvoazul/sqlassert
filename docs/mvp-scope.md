# Clingo Engine MVP Scope

The MVP proves Unique Join Assertions from a SQL Program and optional external Knowledge. Unsupported semantics produce `UNKNOWN` or an explicit unsupported-program diagnostic; they are never silently approximated.

## Included

- `CREATE TABLE` and `CREATE VIEW` statements followed by at most one Root Select.
- Two-pass declaration and definition conversion, including forward references and cycle detection.
- CTEs, FROM subqueries, and recursively expanded view definitions.
- `Scan`, `Project`, `Filter`, `Join`, `Aggregate`, `Distinct`, `Window`, and `QualifyByPartition` relational operations.
- Grouping keys and aggregate expressions.
- `INNER` and `LEFT` Unique Join Assertions.
- `ON` predicates composed from simple equalities joined by `AND`, plus `USING`.
- Candidate Keys and nullable Unique Sets supplied by DDL or explicit Knowledge.
- Uniqueness propagated through grouping, `DISTINCT`, projection, filtering, and recognized `row_number() = 1` partition qualification.
- `PROVED` and `UNKNOWN` outcomes.
- One deterministic Clingo stable model and callback-based reporting.

## Limitations and Future Features

- DuckDB schema and view autodiscovery.
- `CREATE INDEX` and unique-index knowledge.
- `RIGHT`, `FULL`, `SEMI`, `ANTI`, and `CROSS` join analysis.
- Non-equality, null-safe, and `OR` join predicates.
- Scalar and correlated subqueries.
- Set operations such as `UNION`, `INTERSECT`, and `EXCEPT`.
- Grouping sets, `ROLLUP`, and `CUBE`.
- General window-function property reasoning beyond recognized partition qualification.
- A `DISPROVED` outcome backed by genuine refutation evidence.
- A reporting abstraction independent of Clingo, if direct use of the Clingo model becomes painful.
- Polished multi-level reporting for nested view-expansion provenance.

## Null Semantics

A Unique Set guarantees that fully non-null value combinations do not repeat. A Candidate Key is a Unique Set whose columns are all non-null. Nullable Unique Sets are sufficient for ordinary equality-join uniqueness because null values do not match; null-safe equality is outside the MVP.
